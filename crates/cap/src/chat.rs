//! The conversation: a prompt, the agent's turn, and a few commands that start with a slash.

use crate::term::{Ask, Out, Paint, Screen};
use crate::{Interrupt, Ready, config, mcp_text, painted_headline, skills_text};
use cap_core::agent::{Agent, Config, Report, Session, Status};
use cap_core::model::{Message, Model};
use cap_core::perm::Mode;
use std::io::IsTerminal;

/// What each command does.
pub(crate) const COMMANDS: &str = "\
/help          this list
/status        the model, the context in use, the mode, the tests and the isolation
/mode NAME     read (look only), edit (change files, run tests), auto (run commands unasked)
/model NAME    talk to another model of the same server
/clear         start over with an empty conversation
/skills        the skills the agent can use here
/mcp           the outside tool servers and their tools
/quit          end (so does /exit, and the end of the input)";

/// What else the line prompt understands.
const KEYS: &str = "A line that ends with \\ goes on in the next line. Ctrl-C stops the turn that is running.";

/// A line that starts with a slash.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    Help,
    Mode(Option<String>),
    Model(Option<String>),
    Clear,
    Status,
    Skills,
    Mcp,
    Quit,
    Unknown(String),
}

/// Read a slash command; `None` for a line that is a task.
pub fn parse(line: &str) -> Option<Command> {
    let rest = line.trim().strip_prefix('/')?;
    let (name, arg) = rest.split_once(char::is_whitespace).unwrap_or((rest, ""));
    let arg = Some(arg.trim().to_string()).filter(|a| !a.is_empty());
    Some(match name.to_lowercase().as_str() {
        "help" | "?" => Command::Help,
        "mode" => Command::Mode(arg),
        "model" => Command::Model(arg),
        "clear" | "new" => Command::Clear,
        "status" => Command::Status,
        "skills" => Command::Skills,
        "mcp" => Command::Mcp,
        "quit" | "exit" => Command::Quit,
        _ => Command::Unknown(name.to_string()),
    })
}

/// Add a typed line to the task. `true` when the task is complete.
fn take_line(task: &mut String, line: &str) -> bool {
    match line.strip_suffix('\\') {
        Some(more) => {
            task.push_str(more);
            task.push('\n');
            false
        }
        None => {
            task.push_str(line);
            true
        }
    }
}

fn read_line() -> Option<String> {
    let mut line = String::new();
    match std::io::stdin().read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(line.trim_end_matches(['\r', '\n']).to_string()),
    }
}

/// The next thing the person typed; `None` at the end of the input.
async fn read_task(paint: Paint) -> Option<String> {
    let typed = std::io::stdin().is_terminal();
    let mut task = String::new();
    loop {
        if typed {
            Out::Stdout.print(&paint.bold(if task.is_empty() { "cap> " } else { "...> " }));
        }
        let Some(line) = tokio::task::spawn_blocking(read_line).await.ok().flatten() else {
            return Some(task).filter(|t| !t.trim().is_empty());
        };
        if take_line(&mut task, &line) {
            return Some(task);
        }
    }
}

/// What follows a turn: the answer if it is not on the screen yet, then how the turn ended.
pub fn outcome(report: &Report, shown: &str, paint: Paint) -> String {
    let mut out = String::new();
    let text = report.text.trim();
    if !text.is_empty() && text != shown.trim() {
        out.push_str(text);
        out.push('\n');
    }
    if report.status != Status::Answered {
        if let (Status::TestsFail, Some(tail)) = (&report.status, &report.failure) {
            out.push_str(tail.trim_end());
            out.push('\n');
        }
        out.push_str(&painted_headline(&report.status, paint));
        out.push('\n');
    }
    if !report.changed.is_empty() {
        out.push_str(&format!("changed: {}\n", report.changed.join(", ")));
    }
    out
}

pub(crate) fn status_text(ready: &Ready, used: u32) -> String {
    let s = &ready.settings;
    let tools: usize = ready.hub.tools().len();
    let servers = ready.hub.servers().count();
    [
        format!("provider    {} ({})", s.provider.name, s.provider.base_url),
        format!("model       {}", s.provider.model),
        format!("context     {used} of {} tokens in use", s.provider.context),
        format!("mode        {}", s.mode.as_str()),
        format!("tools       {}", ready.profile().as_str()),
        format!("tests       {}", ready.tests()),
        format!("isolation   {}", ready.sandbox.name()),
        format!("outside     {servers} servers, {tools} tools"),
        format!("skills      {}", ready.skills.len()),
        format!("folder      {}", ready.ws.root().display()),
    ]
    .join("\n")
}

/// Why the conversation loop handed back.
pub(crate) enum Next {
    Quit,
    Mode(Mode),
    Model(String),
    Clear,
}

/// Talk until something changes that the agent has to be set up again for.
async fn converse<M: Model>(
    mut session: Session<'_, M>,
    ready: &Ready,
    screen: &Screen,
    interrupt: &Interrupt,
    paint: Paint,
) -> (Vec<Message>, Next) {
    let out = Out::Stdout;
    loop {
        let Some(line) = read_task(paint).await else {
            return (session.into_history(), Next::Quit);
        };
        let task = line.trim();
        if task.is_empty() {
            continue;
        }
        match parse(task) {
            None => {}
            Some(Command::Quit) => return (session.into_history(), Next::Quit),
            Some(Command::Clear) => return (session.into_history(), Next::Clear),
            Some(Command::Mode(Some(name))) => {
                match config::parse_mode(&name) {
                    Ok(mode) if mode == ready.settings.mode => out.line(&format!("the mode is {} already", mode.as_str())),
                    Ok(mode) => return (session.into_history(), Next::Mode(mode)),
                    Err(why) => out.line(&why),
                }
                continue;
            }
            Some(Command::Model(Some(name))) => return (session.into_history(), Next::Model(name)),
            Some(command) => {
                out.line(&match command {
                    Command::Help => format!("{COMMANDS}\n{KEYS}"),
                    Command::Mode(_) => {
                        format!("the mode is {}. /mode read, /mode edit or /mode auto changes it", ready.settings.mode.as_str())
                    }
                    Command::Model(_) => format!("the model is {}. /model NAME changes it", ready.settings.provider.model),
                    Command::Status => status_text(ready, session.tokens()),
                    Command::Skills => skills_text(&ready.skills),
                    Command::Mcp => mcp_text(&ready.hub),
                    Command::Unknown(name) => format!("no command /{name}. /help lists the commands"),
                    Command::Quit | Command::Clear => String::new(),
                });
                continue;
            }
        }
        interrupt.begin();
        screen.begin_turn();
        let report = session.turn(task).await;
        interrupt.end();
        out.print(&outcome(&report, &screen.shown_answer(), paint));
        out.line(&paint.dim(&format!(
            "{} steps · {:.1} s · context {}/{} tokens",
            report.steps,
            report.seconds,
            session.tokens(),
            ready.settings.provider.context
        )));
    }
}

/// The conversation, until the person ends it.
pub async fn run(mut ready: Ready, interrupt: &Interrupt) {
    let out = Out::Stdout;
    let paint = Paint::for_out(out);
    out.line(&ready.header(paint));
    out.line(&paint.dim("/help lists the commands. Ctrl-C stops a turn; /quit ends."));
    let screen = Screen::live(out);
    let ask = Ask { out };
    let mut history: Vec<Message> = Vec::new();
    loop {
        let (kept, next) = {
            let client = ready.client();
            let gate = ready.gate();
            let agent = Agent {
                model: &client,
                ws: &ready.ws,
                gate: &gate,
                approver: &ask,
                sink: &screen,
                // A question that changes nothing is answered without a test run.
                config: Config { check_answers: false, ..ready.config() },
                cancel: Some(&*interrupt.cancel),
                sandbox: &ready.sandbox,
                skills: &ready.skills,
                mcp: ready.mcp(),
            };
            let session = if history.is_empty() { agent.session() } else { agent.resume(std::mem::take(&mut history)) };
            converse(session, &ready, &screen, interrupt, paint).await
        };
        history = kept;
        match next {
            Next::Quit => return,
            Next::Clear => {
                history.clear();
                out.line(&paint.dim("a new conversation"));
            }
            Next::Mode(mode) => {
                ready.settings.mode = mode;
                out.line(&paint.dim(&format!("mode {}", mode.as_str())));
            }
            Next::Model(name) => {
                out.line(&paint.dim(&format!("model {name} (context window {} tokens, as before)", ready.settings.provider.context)));
                ready.settings.provider.model = name;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_line_with_a_slash_is_a_command_and_anything_else_a_task() {
        assert_eq!(parse("fix the failing test"), None);
        assert_eq!(parse("  what does a/b.py do?"), None);
        assert_eq!(parse("/help"), Some(Command::Help));
        assert_eq!(parse("  /STATUS  "), Some(Command::Status));
        assert_eq!(parse("/mode"), Some(Command::Mode(None)));
        assert_eq!(parse("/mode  read "), Some(Command::Mode(Some("read".into()))));
        assert_eq!(parse("/model qwen3:14b"), Some(Command::Model(Some("qwen3:14b".into()))));
        assert_eq!(parse("/clear"), Some(Command::Clear));
        assert_eq!(parse("/skills"), Some(Command::Skills));
        assert_eq!(parse("/mcp"), Some(Command::Mcp));
        assert_eq!(parse("/quit"), Some(Command::Quit));
        assert_eq!(parse("/exit"), Some(Command::Quit));
        assert_eq!(parse("/frobnicate now"), Some(Command::Unknown("frobnicate".into())));
    }

    #[test]
    fn a_backslash_at_the_end_carries_the_task_into_the_next_line() {
        let mut task = String::new();
        assert!(!take_line(&mut task, "first \\"));
        assert!(!take_line(&mut task, "second\\"));
        assert!(take_line(&mut task, "third"));
        assert_eq!(task, "first \nsecond\nthird");

        let mut one = String::new();
        assert!(take_line(&mut one, "just this"));
        assert_eq!(one, "just this");
    }

    fn report(status: Status, text: &str) -> Report {
        Report {
            status,
            text: text.to_string(),
            steps: 3,
            prompt_tokens: 100,
            completion_tokens: 20,
            seconds: 1.5,
            changed: Vec::new(),
            rejections: 0,
            failure: None,
            transcript: Vec::new(),
        }
    }

    #[test]
    fn a_turn_ends_with_the_answer_once_and_the_verdict() {
        let plain = Paint(false);
        // The answer the person watched arrive is not printed again; a plain answer needs no verdict.
        assert_eq!(outcome(&report(Status::Answered, "Two files.\n"), "Two files.", plain), "");
        assert_eq!(outcome(&report(Status::Answered, "Two files."), "", plain), "Two files.\n");

        let mut done = report(Status::Verified, "Fixed the off-by-one.");
        done.changed = vec!["a.py".into(), "b.py".into()];
        assert_eq!(outcome(&done, "Fixed the off-by-one.", plain), "DONE — the tests pass\nchanged: a.py, b.py\n");

        let mut failing = report(Status::TestsFail, "");
        failing.failure = Some("E   assert 1 == 2\n1 failed\n".into());
        assert_eq!(outcome(&failing, "", plain), "E   assert 1 == 2\n1 failed\nNOT DONE — the tests still fail\n");

        let stopped = outcome(&report(Status::Interrupted, ""), "", Paint(true));
        assert!(stopped.starts_with("\x1b[33mSTOPPED"), "{stopped}");
    }
}
