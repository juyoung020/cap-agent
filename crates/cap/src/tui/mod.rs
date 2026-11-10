//! The window `cap` opens on a terminal: the conversation above, the input and the state below.

mod card;
mod input;
mod model;
mod screen;
mod text;
mod ui;

use crate::chat::{self, Command, Next};
use crate::config::{self, Env};
use crate::{Ready, mcp_text, skills_text};
use cap_core::agent::{Agent, Config, Event, Session, Sink};
use cap_core::cancel::Cancel;
use cap_core::model::{Message, Model};
use cap_core::perm::{Approver, Mode};
use card::{Card, Choice};
use crossterm::event::{self, Event as Input, KeyCode, KeyEventKind, MouseEventKind};
use model::View;
use screen::{Colours, Term};
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};
use text::{BRAND, FG, Line, MUTED, SUCCESS, Style, WARNING};
use tokio::time::MissedTickBehavior;
use ui::{Action, Busy, Chrome, Ui};

/// What the keys do, next to the commands.
const KEYS: &str = "\
enter          send (alt+enter or ctrl+j starts a new line)
tab            complete a command
shift+tab      change the mode
ctrl+o         open or fold the details
pgup, pgdn     scroll (so does the wheel)
esc            stop the turn that is running
ctrl+c twice   end";

/// A folder with `~` for the person's own folder.
fn short_path(path: &Path, home: Option<&Path>) -> String {
    match home.and_then(|home| path.strip_prefix(home).ok()) {
        Some(rest) if rest.as_os_str().is_empty() => "~".to_string(),
        Some(rest) => format!("~{}{}", std::path::MAIN_SEPARATOR, rest.display()),
        None => path.display().to_string(),
    }
}

/// The rows a conversation opens with.
fn welcome(model: &str, place: &str, tests: &str, notes: &[String]) -> Vec<Line> {
    let brand = Style::fg(BRAND).bold();
    let muted = Style::fg(MUTED);
    let mut rows = vec![
        Line::styled("╭─╮╭─╮╭─╮", brand).with("   CAP", Style::fg(FG).bold()).with("  Code As Planned", muted),
        Line::styled("│  ├─┤├─╯", brand).with(format!("   {}", cap_core::VERSION), muted),
        Line::styled("╰─╯╵ ╵╵  ", brand),
        Line::new(),
        Line::styled(model, Style::fg(FG)).with(format!("  ·  {place}"), muted),
        Line::styled(format!("tests: {tests}"), muted),
    ];
    rows.extend(notes.iter().map(|note| Line::styled(format!("! {note}"), Style::fg(WARNING))));
    rows.push(Line::new());
    rows.push(Line::styled("/help lists the commands and the keys", muted));
    rows
}

fn next_mode(mode: Mode) -> Mode {
    match mode {
        Mode::Read => Mode::Edit,
        Mode::Edit => Mode::Auto,
        Mode::Auto => Mode::Read,
    }
}

/// Hand what the terminal reported to the window. `running` says that a turn is under way.
fn feed(ui: &mut Ui, batch: Vec<Input>, running: bool) -> Vec<Action> {
    let batch: Vec<Input> =
        batch.into_iter().filter(|input| !matches!(input, Input::Key(key) if key.kind == KeyEventKind::Release)).collect();
    let mut actions = Vec::new();
    for (at, input) in batch.iter().enumerate() {
        match input {
            Input::Key(key) => {
                // Pasted text arrives as keys in one go; a line end in the middle of it is text, not a send.
                let pasted = key.code == KeyCode::Enter
                    && key.modifiers.is_empty()
                    && batch[at + 1..].iter().any(|next| matches!(next, Input::Key(_)));
                if pasted {
                    ui.paste("\n");
                } else if let Some(action) = ui.key(*key, running) {
                    actions.push(action);
                }
            }
            Input::Mouse(mouse) => match mouse.kind {
                MouseEventKind::ScrollUp => ui.wheel(true),
                MouseEventKind::ScrollDown => ui.wheel(false),
                _ => continue,
            },
            Input::Paste(text) => ui.paste(text),
            Input::Resize(..) => {}
            _ => continue,
        }
        ui.dirty = true;
    }
    actions
}

/// Answer a slash command in the window; `Some` when the agent has to be set up again for it.
fn command(ui: &mut Ui, line: &str, command: Command, ready: &Ready, used: u32) -> Option<Next> {
    ui.view.user(line);
    ui.dirty = true;
    let said = match command {
        Command::Quit => return Some(Next::Quit),
        Command::Clear => return Some(Next::Clear),
        Command::Mode(Some(name)) => match config::parse_mode(&name) {
            Ok(mode) if mode == ready.settings.mode => format!("the mode is {} already", mode.as_str()),
            Ok(mode) => {
                ui.view.say(&format!("mode {}", mode.as_str()), MUTED);
                return Some(Next::Mode(mode));
            }
            Err(why) => {
                ui.view.say(&why, WARNING);
                return None;
            }
        },
        Command::Model(Some(name)) => {
            ui.view.say(&format!("model {name} (context window {} tokens, as before)", ready.settings.provider.context), MUTED);
            return Some(Next::Model(name));
        }
        Command::Help => format!("{}\n\n{KEYS}", chat::COMMANDS),
        Command::Mode(None) => format!("the mode is {}. /mode read, /mode edit or /mode auto changes it", ready.settings.mode.as_str()),
        Command::Model(None) => format!("the model is {}. /model NAME changes it", ready.settings.provider.model),
        Command::Status => chat::status_text(ready, used),
        Command::Skills => skills_text(&ready.skills),
        Command::Mcp => mcp_text(&ready.hub),
        Command::Unknown(name) => format!("no command /{name}. /help lists the commands"),
    };
    ui.view.say(&said, FG);
    None
}

/// The terminal and what it shows at the moment.
struct Painter {
    term: Term,
    stamp: u128,
    size: (usize, usize),
}

/// What the agent's side and the person's side of the window both reach.
struct Shared {
    ui: Mutex<Ui>,
    painter: Mutex<Painter>,
    cancel: Arc<Cancel>,
}

impl Shared {
    fn ui(&self) -> MutexGuard<'_, Ui> {
        self.ui.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Bring the terminal up to date when something changed.
    fn draw(&self) {
        let mut ui = self.ui();
        let mut painter = self.painter.lock().unwrap_or_else(PoisonError::into_inner);
        let now = Instant::now();
        let (stamp, size) = (ui.stamp(now), painter.term.size());
        if !ui.dirty && stamp == painter.stamp && size == painter.size {
            return;
        }
        let frame = ui.frame(size.0, size.1, now);
        ui.dirty = false;
        (painter.stamp, painter.size) = (stamp, size);
        // A terminal that takes no more output has nothing to show an error on.
        let _ = painter.term.draw(&frame);
    }

    /// Draw every row again, for a terminal something else wrote on.
    fn redraw(&self) {
        self.painter.lock().unwrap_or_else(PoisonError::into_inner).term.forget();
        self.ui().dirty = true;
    }

    /// What the person did since the last look.
    fn pump(&self, running: bool) -> Vec<Action> {
        let mut batch = Vec::new();
        while event::poll(Duration::ZERO).unwrap_or(false) {
            match event::read() {
                Ok(input) => batch.push(input),
                Err(_) => break,
            }
        }
        if batch.is_empty() { Vec::new() } else { feed(&mut self.ui(), batch, running) }
    }

    fn interrupt(&self) {
        self.cancel.cancel();
        let mut ui = self.ui();
        if let Some(busy) = ui.busy.as_mut() {
            busy.label = "Interrupting…";
        }
        ui.dirty = true;
    }
}

impl Sink for Shared {
    fn event(&self, event: &Event<'_>) {
        let mut ui = self.ui();
        ui.view.event(event);
        let label = match event {
            Event::Call { name: "run_tests", .. } => Some("Testing…"),
            Event::Call { name: "run", .. } => Some("Running…"),
            Event::Call { .. } => Some("Working…"),
            Event::Step(_) | Event::Result { .. } => Some("Thinking…"),
            _ => None,
        };
        if let (Some(label), Some(busy)) = (label, ui.busy.as_mut())
            && !self.cancel.is_cancelled()
        {
            busy.label = label;
        }
        ui.dirty = true;
    }
}

impl Approver for Shared {
    fn approve(&self, action: &str, why: &str) -> bool {
        if self.cancel.is_cancelled() {
            return false;
        }
        {
            let mut ui = self.ui();
            if ui.allowed.contains(action) {
                return true;
            }
            ui.card = Some(Card::new(action, why));
            ui.dirty = true;
        }
        // The turn waits for the answer, so the window is kept alive from here.
        let choice = loop {
            self.draw();
            match event::poll(Duration::from_millis(50)) {
                Ok(true) => {}
                Ok(false) => continue,
                Err(_) => break Choice::No,
            }
            match event::read() {
                Ok(Input::Key(key)) if key.kind != KeyEventKind::Release => {
                    let mut ui = self.ui();
                    ui.dirty = true;
                    if let Some(choice) = ui.card.as_mut().and_then(|card| card.key(key)) {
                        break choice;
                    }
                }
                Ok(Input::Resize(..)) => self.ui().dirty = true,
                Ok(_) => {}
                Err(_) => break Choice::No,
            }
        };
        let mut ui = self.ui();
        ui.card = None;
        ui.dirty = true;
        match choice {
            Choice::Yes => true,
            Choice::Always => {
                ui.allowed.insert(action.to_string());
                ui.view.note(vec![Line::styled("✓ Allowed for this session  ", Style::fg(SUCCESS)).with(action, Style::fg(MUTED))]);
                true
            }
            Choice::No => false,
            Choice::NoAndCancel => {
                drop(ui);
                self.interrupt();
                false
            }
        }
    }
}

/// Talk until something changes that the agent has to be set up again for.
async fn converse<M: Model>(mut session: Session<'_, M>, ready: &Ready, shared: &Shared) -> (Vec<Message>, Next) {
    let mut tick = tokio::time::interval(Duration::from_millis(16));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tick.tick().await;
        for action in shared.pump(false) {
            match action {
                Action::Quit => return (session.into_history(), Next::Quit),
                Action::CycleMode => return (session.into_history(), Next::Mode(next_mode(ready.settings.mode))),
                Action::Redraw => shared.redraw(),
                Action::Send(text) => shared.ui().queue.push_back(text),
                Action::Cancel => {}
            }
        }
        shared.draw();
        let Some(line) = shared.ui().queue.pop_front() else { continue };
        let task = line.trim();
        if task.is_empty() {
            continue;
        }
        if let Some(asked) = chat::parse(task) {
            let next = command(&mut shared.ui(), task, asked, ready, session.tokens());
            match next {
                Some(next) => return (session.into_history(), next),
                None => continue,
            }
        }
        shared.cancel.reset();
        {
            let mut ui = shared.ui();
            ui.view.user(task);
            ui.view.begin_turn();
            ui.busy = Some(Busy { label: "Thinking…", since: Instant::now() });
            ui.dirty = true;
        }
        let report = {
            let turn = session.turn(task);
            tokio::pin!(turn);
            loop {
                tokio::select! {
                    report = &mut turn => break report,
                    _ = tick.tick() => {
                        for action in shared.pump(true) {
                            match action {
                                Action::Cancel => shared.interrupt(),
                                Action::Redraw => shared.redraw(),
                                _ => {}
                            }
                        }
                        shared.draw();
                    }
                }
            }
        };
        let tokens = session.tokens();
        let mut ui = shared.ui();
        ui.view.end_turn(&report);
        ui.view.tokens = tokens;
        ui.seconds += report.seconds;
        ui.busy = None;
        ui.dirty = true;
    }
}

/// The conversation in the window, until the person ends it.
pub async fn run(mut ready: Ready, env: Env<'_>) -> std::io::Result<()> {
    let root = ready.ws.root().to_path_buf();
    let place = short_path(&root, config::home(env).as_deref());
    let folder = root.file_name().map_or_else(|| place.clone(), |name| name.to_string_lossy().into_owned());
    let opening = |ready: &Ready| welcome(&ready.settings.provider.model, &place, ready.tests(), &ready.notes());

    let mut view = View::new(&root, ready.tests());
    view.note(opening(&ready));
    let chrome = Chrome { mode: ready.settings.mode, sandboxed: ready.sandbox.is_on(), context: ready.settings.provider.context };
    let term = Term::open(Colours::detect(env), &format!("cap — {folder}"))?;
    let shared = Shared {
        ui: Mutex::new(Ui::new(chrome, view)),
        painter: Mutex::new(Painter { term, stamp: 0, size: (0, 0) }),
        cancel: Arc::new(Cancel::new()),
    };

    let mut history: Vec<Message> = Vec::new();
    loop {
        let (kept, next) = {
            let client = ready.client();
            let gate = ready.gate();
            let agent = Agent {
                model: &client,
                ws: &ready.ws,
                gate: &gate,
                approver: &shared,
                sink: &shared,
                // A question that changes nothing is answered without a test run.
                config: Config { check_answers: false, ..ready.config() },
                cancel: Some(&*shared.cancel),
                sandbox: &ready.sandbox,
                skills: &ready.skills,
                mcp: ready.mcp(),
            };
            let session = if history.is_empty() { agent.session() } else { agent.resume(std::mem::take(&mut history)) };
            converse(session, &ready, &shared).await
        };
        history = kept;
        let mut ui = shared.ui();
        ui.dirty = true;
        match next {
            Next::Quit => return Ok(()),
            Next::Clear => {
                history.clear();
                ui.view.reset();
                ui.view.note(opening(&ready));
                ui.view.say("a new conversation", MUTED);
            }
            Next::Mode(mode) => {
                ready.settings.mode = mode;
                ui.chrome.mode = mode;
            }
            Next::Model(name) => ready.settings.provider.model = name,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseEvent};
    use std::path::PathBuf;

    fn ui() -> Ui {
        Ui::new(Chrome { mode: Mode::Edit, sandboxed: false, context: 16_000 }, View::new(Path::new("."), "none"))
    }

    fn key(code: KeyCode) -> Input {
        Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
    }

    #[test]
    fn a_line_end_inside_pasted_text_stays_in_the_text() {
        let mut ui = ui();
        let pasted = feed(&mut ui, vec![key(KeyCode::Char('a')), key(KeyCode::Enter), key(KeyCode::Char('b'))], false);
        assert_eq!(pasted, []);
        assert_eq!(ui.composer.text(), "a\nb");
        // Enter on its own sends what is there.
        assert_eq!(feed(&mut ui, vec![key(KeyCode::Enter)], false), [Action::Send("a\nb".to_string())]);
        assert!(ui.composer.is_empty());
    }

    #[test]
    fn a_key_let_go_and_a_moving_mouse_change_nothing() {
        let mut ui = ui();
        ui.dirty = false;
        let up = Input::Key(KeyEvent::new_with_kind(KeyCode::Char('x'), KeyModifiers::NONE, KeyEventKind::Release));
        let moved = Input::Mouse(MouseEvent { kind: MouseEventKind::Moved, column: 3, row: 4, modifiers: KeyModifiers::NONE });
        assert_eq!(feed(&mut ui, vec![up, moved], false), []);
        assert!(ui.composer.is_empty() && !ui.dirty);

        let wheel = Input::Mouse(MouseEvent { kind: MouseEventKind::ScrollUp, column: 3, row: 4, modifiers: KeyModifiers::NONE });
        assert_eq!(feed(&mut ui, vec![wheel], false), []);
        assert!(ui.dirty);
    }

    #[test]
    fn while_a_turn_runs_a_sent_line_waits_and_escape_stops_the_turn() {
        let mut ui = ui();
        let typed = feed(&mut ui, vec![key(KeyCode::Char('g')), key(KeyCode::Char('o'))], true);
        assert_eq!(typed, []);
        assert_eq!(feed(&mut ui, vec![key(KeyCode::Enter)], true), []);
        assert_eq!(ui.queue, ["go"]);
        assert_eq!(feed(&mut ui, vec![key(KeyCode::Esc)], true), [Action::Cancel]);
    }

    #[test]
    fn the_own_folder_is_written_short() {
        let home = PathBuf::from("base").join("me");
        let sep = std::path::MAIN_SEPARATOR;
        assert_eq!(short_path(&home.join("work").join("calc"), Some(&home)), format!("~{sep}work{sep}calc"));
        assert_eq!(short_path(&home, Some(&home)), "~");
        assert_eq!(short_path(Path::new("elsewhere"), Some(&home)), "elsewhere");
        assert_eq!(short_path(Path::new("elsewhere"), None), "elsewhere");
    }

    #[test]
    fn a_conversation_opens_with_what_runs_and_where() {
        let rows: Vec<String> =
            welcome("qwen3.5-9b", "~/calc", "cargo test", &["no isolation here".to_string()]).iter().map(Line::text).collect();
        assert_eq!(
            rows,
            [
                "╭─╮╭─╮╭─╮   CAP  Code As Planned".to_string(),
                format!("│  ├─┤├─╯   {}", cap_core::VERSION),
                "╰─╯╵ ╵╵  ".to_string(),
                String::new(),
                "qwen3.5-9b  ·  ~/calc".to_string(),
                "tests: cargo test".to_string(),
                "! no isolation here".to_string(),
                String::new(),
                "/help lists the commands and the keys".to_string(),
            ]
        );
    }

    #[test]
    fn the_modes_go_round() {
        assert_eq!([Mode::Read, Mode::Edit, Mode::Auto].map(next_mode), [Mode::Edit, Mode::Auto, Mode::Read]);
    }
}
