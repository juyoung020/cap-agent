//! What the person sees: colours, one line per step, the model's text as it arrives, and the
//! question before something that needs a yes.

use cap_core::agent::{Event, Sink};
use cap_core::gate::Verdict;
use cap_core::perm::Approver;
use cap_core::tools::Status as ToolStatus;
use std::io::{IsTerminal, Write};
use std::sync::{Mutex, PoisonError};

/// Where the lines go.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Out {
    Stdout,
    Stderr,
}

impl Out {
    pub fn is_terminal(self) -> bool {
        match self {
            Out::Stdout => std::io::stdout().is_terminal(),
            Out::Stderr => std::io::stderr().is_terminal(),
        }
    }

    /// Write and show at once; a closed pipe ends the output quietly.
    pub fn print(self, text: &str) {
        let _ = match self {
            Out::Stdout => {
                let mut out = std::io::stdout().lock();
                out.write_all(text.as_bytes()).and_then(|()| out.flush())
            }
            Out::Stderr => {
                let mut out = std::io::stderr().lock();
                out.write_all(text.as_bytes()).and_then(|()| out.flush())
            }
        };
    }

    pub fn line(self, text: &str) {
        self.print(&format!("{text}\n"));
    }
}

/// Ask the Windows console to read colour codes. Other systems read them as they are.
#[cfg(windows)]
pub(crate) fn colour_codes_work(out: Out) -> bool {
    use windows_sys::Win32::System::Console::{
        ENABLE_VIRTUAL_TERMINAL_PROCESSING, GetConsoleMode, GetStdHandle, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE, SetConsoleMode,
    };
    let id = match out {
        Out::Stdout => STD_OUTPUT_HANDLE,
        Out::Stderr => STD_ERROR_HANDLE,
    };
    // SAFETY: the handle comes from the system for this process, and `mode` outlives both calls.
    unsafe {
        let handle = GetStdHandle(id);
        let mut mode = 0;
        GetConsoleMode(handle, &mut mode) != 0
            && (mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING != 0 || SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING) != 0)
    }
}

#[cfg(not(windows))]
pub(crate) fn colour_codes_work(_: Out) -> bool {
    true
}

/// Colours, or plain text where they would be noise (a file, a pipe, `NO_COLOR`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Paint(pub bool);

impl Paint {
    pub fn for_out(out: Out) -> Paint {
        let wanted = std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty());
        Paint(wanted && out.is_terminal() && colour_codes_work(out))
    }

    fn wrap(self, code: &str, text: &str) -> String {
        if self.0 && !text.is_empty() { format!("\x1b[{code}m{text}\x1b[0m") } else { text.to_string() }
    }

    pub fn dim(self, text: &str) -> String {
        self.wrap("2", text)
    }

    pub fn bold(self, text: &str) -> String {
        self.wrap("1", text)
    }

    pub fn red(self, text: &str) -> String {
        self.wrap("31", text)
    }

    pub fn green(self, text: &str) -> String {
        self.wrap("32", text)
    }

    pub fn yellow(self, text: &str) -> String {
        self.wrap("33", text)
    }

    pub fn cyan(self, text: &str) -> String {
        self.wrap("36", text)
    }
}

/// The first line with something on it, cut to `max` characters.
pub fn first_line(text: &str, max: usize) -> String {
    let line = text.lines().find(|l| !l.trim().is_empty()).unwrap_or("").trim();
    if line.chars().count() > max { line.chars().take(max).collect::<String>() + "…" } else { line.to_string() }
}

#[derive(Default)]
struct State {
    /// Pieces of the reply in progress have been shown.
    streamed: bool,
    /// The cursor stands after shown text, not at the start of a line.
    mid_line: bool,
    /// The last complete answer that was shown in full.
    answer: String,
}

/// A line of its own starts on a fresh line, also after text that ended without one.
fn fresh(state: &mut State, line: String) -> String {
    let lead = if std::mem::take(&mut state.mid_line) { "\n" } else { "" };
    state.streamed = false;
    format!("{lead}{line}\n")
}

/// Progress on the terminal.
pub struct Screen {
    out: Out,
    paint: Paint,
    /// Show the model's text in full, as it arrives. Otherwise one line per step.
    live: bool,
    state: Mutex<State>,
}

impl Screen {
    /// One short line per event, for a run that prints its answer at the end.
    pub fn brief(out: Out) -> Screen {
        Screen { out, paint: Paint::for_out(out), live: false, state: Mutex::default() }
    }

    /// The model's words as they arrive, for a conversation.
    pub fn live(out: Out) -> Screen {
        Screen { out, paint: Paint::for_out(out), live: true, state: Mutex::default() }
    }

    #[cfg(test)]
    fn plain(live: bool) -> Screen {
        Screen { out: Out::Stderr, paint: Paint(false), live, state: Mutex::default() }
    }

    /// Forget what the last turn showed.
    pub fn begin_turn(&self) {
        *self.state.lock().unwrap_or_else(PoisonError::into_inner) = State::default();
    }

    /// The answer the person has already read in full, if any.
    pub fn shown_answer(&self) -> String {
        self.state.lock().unwrap_or_else(PoisonError::into_inner).answer.clone()
    }

    /// What one event adds to the screen.
    fn render(&self, event: &Event<'_>) -> String {
        let p = self.paint;
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        match event {
            Event::Step(_) | Event::Context(_) => String::new(),
            Event::Delta(piece) => {
                if !self.live || piece.is_empty() {
                    return String::new();
                }
                state.streamed = true;
                state.mid_line = !piece.ends_with('\n');
                (*piece).to_string()
            }
            Event::Text(text) | Event::Answer(text) => {
                let answer = matches!(event, Event::Answer(_));
                if !self.live {
                    return if answer || text.trim().is_empty() { String::new() } else { format!("  {}\n", first_line(text, 160)) };
                }
                if answer {
                    state.answer = (*text).to_string();
                }
                if std::mem::take(&mut state.streamed) {
                    return if std::mem::take(&mut state.mid_line) { "\n".to_string() } else { String::new() };
                }
                if text.trim().is_empty() { String::new() } else { format!("{}\n", text.trim_end()) }
            }
            Event::Call { name, args } => fresh(&mut state, format!("{} {}", p.cyan(&format!("> {name}")), p.dim(&first_line(args, 140)))),
            Event::Result { status, text, .. } => {
                let mark = match status {
                    ToolStatus::Ok => p.dim("[ok]"),
                    ToolStatus::Failed => p.red("[failed]"),
                    ToolStatus::Denied => p.yellow("[refused]"),
                };
                fresh(&mut state, format!("  {mark} {}", p.dim(&first_line(text, 140))))
            }
            Event::Gate(verdict) => match verdict {
                Verdict::Pass => fresh(&mut state, p.green("= tests pass")),
                Verdict::Fail { tail, .. } => {
                    fresh(&mut state, p.red(&format!("= tests fail: {}", first_line(tail.lines().last().unwrap_or(""), 120))))
                }
                Verdict::NoTests => String::new(),
            },
            Event::Nudge(text) => fresh(&mut state, p.yellow(&format!("! {}", first_line(text, 140)))),
            Event::Compacted { stubbed, dropped } => {
                fresh(&mut state, p.dim(&format!("~ context trimmed ({stubbed} shortened, {dropped} dropped)")))
            }
        }
    }
}

impl Sink for Screen {
    fn event(&self, event: &Event<'_>) {
        let text = self.render(event);
        if !text.is_empty() {
            self.out.print(&text);
        }
    }
}

/// Asks on the terminal. With nobody at the terminal the answer is no.
pub struct Ask {
    pub out: Out,
}

impl Approver for Ask {
    fn approve(&self, action: &str, why: &str) -> bool {
        let stdin = std::io::stdin();
        if !stdin.is_terminal() || !self.out.is_terminal() {
            return false;
        }
        let p = Paint::for_out(self.out);
        self.out.print(&format!("{} {action}\n  ({why}) allow? [y/N] ", p.yellow("?")));
        let mut line = String::new();
        stdin.read_line(&mut line).is_ok() && matches!(line.trim().to_lowercase().as_str(), "y" | "yes")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shown(screen: &Screen, events: &[Event<'_>]) -> String {
        events.iter().map(|e| screen.render(e)).collect()
    }

    #[test]
    fn a_run_shows_one_short_line_per_step() {
        let screen = Screen::plain(false);
        let fail = Verdict::Fail { tail: "first\nassert 1 == 2".into(), signature: 1 };
        let out = shown(
            &screen,
            &[
                Event::Step(1),
                Event::Delta("I will "),
                Event::Text("\nI will read the file.\nThen fix it."),
                Event::Call { name: "read_file", args: "{\"path\":\"a.py\"}" },
                Event::Result { name: "read_file", status: ToolStatus::Ok, text: "1| x = 1\n2| y = 2" },
                Event::Result { name: "edit", status: ToolStatus::Denied, text: "outside the project" },
                Event::Gate(&fail),
                Event::Gate(&Verdict::NoTests),
                Event::Gate(&Verdict::Pass),
                Event::Nudge("call done"),
                Event::Compacted { stubbed: 2, dropped: 1 },
                Event::Answer("All fixed."),
            ],
        );
        assert_eq!(
            out,
            "  I will read the file.\n> read_file {\"path\":\"a.py\"}\n  [ok] 1| x = 1\n  [refused] outside the project\n\
             = tests fail: assert 1 == 2\n= tests pass\n! call done\n~ context trimmed (2 shortened, 1 dropped)\n"
        );
        assert_eq!(screen.shown_answer(), "");
    }

    #[test]
    fn a_conversation_shows_the_text_once_as_it_arrives() {
        let screen = Screen::plain(true);
        let out = shown(
            &screen,
            &[
                Event::Delta("Let me "),
                Event::Delta("look."),
                Event::Text("Let me look."),
                Event::Call { name: "list_files", args: "{}" },
                Event::Result { name: "list_files", status: ToolStatus::Ok, text: "a.py" },
                Event::Delta("Two files.\n"),
                Event::Answer("Two files.\n"),
            ],
        );
        assert_eq!(out, "Let me look.\n> list_files {}\n  [ok] a.py\nTwo files.\n");
        assert_eq!(screen.shown_answer(), "Two files.\n");
        screen.begin_turn();
        assert_eq!(screen.shown_answer(), "");
    }

    #[test]
    fn text_that_did_not_arrive_in_pieces_is_shown_whole() {
        let screen = Screen::plain(true);
        let out = shown(&screen, &[Event::Text("Reading."), Event::Call { name: "read_file", args: "{}" }, Event::Answer("Done.\n\n")]);
        assert_eq!(out, "Reading.\n> read_file {}\nDone.\n");
        // A step line after text that stopped mid-line starts on its own line.
        let cut = shown(&Screen::plain(true), &[Event::Delta("half"), Event::Call { name: "done", args: "{}" }]);
        assert_eq!(cut, "half\n> done {}\n");
    }

    #[test]
    fn colours_are_left_out_of_plain_output() {
        assert_eq!(Paint(false).red("x"), "x");
        assert_eq!(Paint(true).red("x"), "\x1b[31mx\x1b[0m");
        assert_eq!(Paint(true).dim(""), "");
        assert_eq!(first_line("\n\n  hello world  \nmore", 5), "hello…");
    }
}
