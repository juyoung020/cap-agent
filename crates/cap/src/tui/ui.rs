//! What the window shows and what each key does, apart from the terminal it is drawn on.

use super::card::{Card, boxed, fill};
use super::input::Composer;
use super::model::{View, plural};
use super::text::{BORDER, BRAND, ERROR, FG, Line, MUTED, SUCCESS, Style, clip, clip_text};
use cap_core::perm::Mode;
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use std::collections::{HashSet, VecDeque};
use std::time::Instant;

const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const PLACEHOLDER: &str = "Type a task, or / for commands";
/// The rows of the message that show at once.
const INPUT_ROWS: usize = 6;
/// The queued messages that show at once.
const QUEUE_ROWS: usize = 3;

/// One picture of the window: its rows from the top, and where the caret blinks.
pub struct Frame {
    pub rows: Vec<Line>,
    /// Column and row, counted from zero.
    pub cursor: Option<(u16, u16)>,
}

/// What the status line says about the session.
pub struct Chrome {
    pub mode: Mode,
    pub sandboxed: bool,
    /// The model's context window, in tokens.
    pub context: u32,
}

/// What the agent is doing, and since when.
pub struct Busy {
    pub label: &'static str,
    pub since: Instant,
}

/// What a key asks the session to do.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Action {
    Send(String),
    Cancel,
    Quit,
    CycleMode,
    Redraw,
}

pub struct Ui {
    pub chrome: Chrome,
    pub view: View,
    pub composer: Composer,
    pub busy: Option<Busy>,
    pub card: Option<Card>,
    /// Messages typed while a turn runs.
    pub queue: VecDeque<String>,
    /// The actions the person allowed for the whole session.
    pub allowed: HashSet<String>,
    /// The seconds the finished turns took.
    pub seconds: f32,
    /// Something changed since the window was drawn.
    pub dirty: bool,
    /// Rows the person moved up from the end of the conversation.
    scroll: usize,
    /// The rows of the conversation that fit in the window.
    page: usize,
    quit_armed: bool,
}

fn muted() -> Style {
    Style::fg(MUTED)
}

impl Ui {
    pub fn new(chrome: Chrome, view: View) -> Ui {
        Ui {
            chrome,
            view,
            composer: Composer::default(),
            busy: None,
            card: None,
            queue: VecDeque::new(),
            allowed: HashSet::new(),
            seconds: 0.0,
            dirty: true,
            scroll: 0,
            page: 1,
            quit_armed: false,
        }
    }

    /// Changes when the moving mark has to be drawn again.
    pub fn stamp(&self, now: Instant) -> u128 {
        match (&self.busy, &self.card) {
            (Some(busy), None) => now.saturating_duration_since(busy.since).as_millis() / 80,
            _ => 0,
        }
    }

    fn scroll_by(&mut self, rows: isize) {
        self.scroll = self.scroll.saturating_add_signed(rows);
        self.dirty = true;
    }

    pub fn wheel(&mut self, up: bool) {
        self.scroll_by(if up { 3 } else { -3 });
    }

    pub fn paste(&mut self, text: &str) {
        self.composer.insert(text);
        self.dirty = true;
    }

    fn control(&mut self, c: char, running: bool, armed: bool) -> Option<Action> {
        match c {
            'c' if running => return Some(Action::Cancel),
            'c' if !self.composer.is_empty() => self.composer.clear(),
            'c' if armed => return Some(Action::Quit),
            'c' => self.quit_armed = true,
            'd' if !running && self.composer.is_empty() => return Some(Action::Quit),
            'j' => self.composer.newline(),
            'o' => self.view.toggle(),
            'l' => return Some(Action::Redraw),
            'a' => self.composer.home(),
            'e' => self.composer.end(),
            'u' => self.composer.kill_to_start(),
            'w' => self.composer.kill_word(),
            _ => {}
        }
        None
    }

    /// Take a key while no question is open. `running` says that a turn is under way.
    pub fn key(&mut self, key: KeyEvent, running: bool) -> Option<Action> {
        if key.kind == KeyEventKind::Release {
            return None;
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let armed = std::mem::take(&mut self.quit_armed);
        self.dirty = true;
        match key.code {
            // Ctrl with Alt is how some keyboards type a letter (AltGr).
            KeyCode::Char(c) if ctrl && !alt => return self.control(c.to_ascii_lowercase(), running, armed),
            KeyCode::Char(c) => self.composer.insert_char(c),
            KeyCode::Enter if alt || shift => self.composer.newline(),
            KeyCode::Enter => {
                let text = self.composer.take();
                if text.trim().is_empty() {
                    return None;
                }
                self.scroll = 0;
                if !running {
                    return Some(Action::Send(text));
                }
                self.queue.push_back(text);
            }
            KeyCode::Tab => self.composer.complete(),
            KeyCode::BackTab if !running => return Some(Action::CycleMode),
            KeyCode::Backspace => self.composer.backspace(),
            KeyCode::Delete => self.composer.delete(),
            KeyCode::Left => self.composer.left(),
            KeyCode::Right => self.composer.right(),
            KeyCode::Home => self.composer.home(),
            KeyCode::End => self.composer.end(),
            KeyCode::Up => self.composer.up(),
            KeyCode::Down => self.composer.down(),
            KeyCode::PageUp => self.scroll_by(self.page.max(2) as isize - 1),
            KeyCode::PageDown => self.scroll_by(1 - self.page.max(2) as isize),
            KeyCode::Esc if running => return Some(Action::Cancel),
            KeyCode::Esc => self.scroll = 0,
            _ => {}
        }
        None
    }

    fn busy_row(busy: &Busy, now: Instant) -> Line {
        let elapsed = now.saturating_duration_since(busy.since);
        let mark = SPINNER[(elapsed.as_millis() / 80 % SPINNER.len() as u128) as usize];
        Line::styled(format!("  {mark} {}", busy.label), Style::fg(BRAND))
            .with(format!("  ({}s · esc to interrupt)", elapsed.as_secs()), muted())
    }

    /// The rows of the message that show, and the caret's row and column among them.
    fn input_rows(&self, width: usize) -> (Vec<Line>, (usize, usize)) {
        if self.composer.is_empty() {
            return (vec![Line::styled(PLACEHOLDER, muted())], (0, 0));
        }
        let (rows, (row, column)) = self.composer.layout(width.saturating_sub(1));
        let first = (row + 1).saturating_sub(INPUT_ROWS);
        let shown = rows[first..].iter().take(INPUT_ROWS).map(|text| Line::styled(text.as_str(), Style::fg(FG))).collect();
        (shown, (row - first, column))
    }

    fn status(&self, width: usize, now: Instant) -> Line {
        let (icon, name, colour) = match self.chrome.mode {
            Mode::Read => ("◇", "read only", MUTED),
            Mode::Edit => ("?", "ask before commands", BRAND),
            Mode::Auto => ("⏵⏵", "auto-run commands", SUCCESS),
        };
        let mut left = Line::styled(format!("  {icon} {name}"), Style::fg(colour).bold());
        if self.chrome.sandboxed {
            left.push("  · sandboxed", muted());
        }
        let hint = if self.card.is_some() {
            "  ·  ↑↓ select · enter confirm"
        } else if self.quit_armed {
            "  ·  ctrl+c again to quit"
        } else if self.busy.is_some() {
            "  ·  type ahead — messages are queued"
        } else {
            "  (shift+tab to cycle)"
        };
        let percent = u64::from(self.view.tokens) * 100 / u64::from(self.chrome.context.max(1));
        let weight = if percent < 60 {
            MUTED
        } else if percent < 85 {
            FG
        } else {
            ERROR
        };
        let running = self.busy.as_ref().map_or(0.0, |busy| now.saturating_duration_since(busy.since).as_secs_f32());
        let right = Line::styled(format!("{percent}% context"), Style::fg(weight)).with(
            format!("  ·  {}  ·  {:.0}s  ", plural(self.view.calls as usize, "call", "calls"), (self.seconds + running).floor()),
            muted(),
        );
        let hinted = left.clone().with(hint, muted());
        let left = if hinted.width() + 2 + right.width() <= width { hinted } else { left };
        let mut row = clip(&left, width.saturating_sub(right.width()));
        row.push(" ".repeat(width.saturating_sub(row.width() + right.width())), Style::default());
        row.spans.extend(right.spans);
        clip(&row, width)
    }

    /// The window as it stands, `width` columns by `height` rows.
    pub fn frame(&mut self, width: usize, height: usize, now: Instant) -> Frame {
        if width < 24 || height < 6 {
            let mut rows = vec![Line::styled(clip_text("the window is too small", width), muted())];
            rows.resize(height.max(1), Line::new());
            return Frame { rows, cursor: None };
        }
        let room = width - 4;
        let mut below: Vec<Line> = Vec::new();
        for text in self.queue.iter().take(QUEUE_ROWS) {
            let first = text.lines().next().unwrap_or_default();
            below.push(Line::styled(format!("  › {}", clip_text(first, room - 2)), muted()));
        }
        if let (Some(busy), None) = (&self.busy, &self.card) {
            below.push(Ui::busy_row(busy, now));
        }
        let names = self.composer.completions();
        if self.card.is_none() && !names.is_empty() {
            let list: Vec<String> = names.iter().map(|name| format!("/{name}")).collect();
            below.push(clip(&Line::styled(format!("   {}", list.join("  ")), muted()), width));
        }
        let box_top = below.len();
        let mut caret = None;
        match &self.card {
            Some(card) => below.extend(boxed(&card.rows(width - 6), width, BRAND)),
            None => {
                let (rows, at) = self.input_rows(width - 6);
                caret = Some(at);
                below.extend(boxed(&rows, width, BORDER));
            }
        }
        below.push(self.status(width, now));

        let page = height.saturating_sub(below.len() + 1).max(1);
        self.page = page;
        let (shown, total, scroll) = self.view.window(room, page, self.scroll);
        self.scroll = scroll;
        // The mark at the right edge that says which part of the conversation shows.
        let thumb = (total > page).then(|| {
            let size = (page * page / total).max(1);
            let top = (total - page - scroll) * (page - size) / (total - page);
            top..top + size
        });
        let mut rows = Vec::with_capacity(page + 1 + below.len());
        for at in 0..page {
            let mut row = Line::styled("  ", Style::default());
            if let Some(line) = shown.get(at) {
                row.spans.extend(fill(line, room).spans);
            }
            if thumb.as_ref().is_some_and(|thumb| thumb.contains(&at)) {
                row.push(" ".repeat(width - 1 - row.width()), Style::default());
                row.push("┃", Style::fg(BORDER));
            }
            rows.push(row);
        }
        rows.push(Line::new());
        rows.extend(below);
        // A window too low for all of it keeps the rows at the bottom.
        let cut = rows.len().saturating_sub(height);
        rows.drain(..cut);
        let cursor = caret.and_then(|(row, column)| {
            let y = (page + 1 + box_top + 1 + row).checked_sub(cut)?;
            Some(((3 + column) as u16, y as u16))
        });
        Frame { rows, cursor }
    }
}

#[cfg(test)]
mod tests {
    use super::super::card::Choice;
    use super::*;
    use std::path::Path;
    use std::time::Duration;

    fn ui(mode: Mode) -> Ui {
        Ui::new(Chrome { mode, sandboxed: false, context: 1000 }, View::new(Path::new("."), "none"))
    }

    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn ctrl(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
    }

    fn typed(ui: &mut Ui, text: &str, running: bool) {
        for c in text.chars() {
            assert_eq!(ui.key(press(KeyCode::Char(c)), running), None);
        }
    }

    fn texts(frame: &Frame) -> Vec<String> {
        frame.rows.iter().map(|row| row.text().trim_end().to_string()).collect()
    }

    #[test]
    fn the_window_opens_with_an_empty_box_and_the_status_line() {
        let mut ui = ui(Mode::Edit);
        let frame = ui.frame(80, 12, Instant::now());
        let rows = texts(&frame);
        assert_eq!(rows.len(), 12);
        assert!(rows[..8].iter().all(String::is_empty), "{rows:?}");
        assert_eq!(rows[8], format!(" ╭{}╮", "─".repeat(76)));
        assert_eq!(rows[9], format!(" │ {PLACEHOLDER}{} │", " ".repeat(74 - PLACEHOLDER.len())));
        assert_eq!(rows[10], format!(" ╰{}╯", "─".repeat(76)));
        assert_eq!(rows[11], "  ? ask before commands  (shift+tab to cycle)    0% context  ·  0 calls  ·  0s");
        assert_eq!(frame.cursor, Some((3, 9)));
        assert!(frame.rows.iter().all(|row| row.width() <= 80));
    }

    #[test]
    fn a_message_is_sent_when_idle_and_queued_while_a_turn_runs() {
        let mut ui = ui(Mode::Edit);
        typed(&mut ui, "fix it", false);
        assert_eq!(ui.frame(80, 12, Instant::now()).cursor, Some((9, 9)));
        assert_eq!(ui.key(press(KeyCode::Enter), false), Some(Action::Send("fix it".into())));
        assert_eq!(ui.key(press(KeyCode::Enter), false), None);

        let since = Instant::now();
        ui.busy = Some(Busy { label: "Thinking…", since });
        typed(&mut ui, "then the docs", true);
        assert_eq!(ui.key(press(KeyCode::Enter), true), None);
        assert_eq!(ui.queue, ["then the docs"]);
        let rows = texts(&ui.frame(100, 12, since + Duration::from_millis(2170)));
        assert_eq!(rows[6], "  › then the docs");
        assert_eq!(rows[7], "  ⠧ Thinking…  (2s · esc to interrupt)");
        assert!(rows[11].starts_with("  ? ask before commands  ·  type ahead — messages are queued"), "{}", rows[11]);
        assert!(rows[11].ends_with("0 calls  ·  2s"), "{}", rows[11]);
        assert_eq!(ui.key(press(KeyCode::BackTab), true), None);
        assert_eq!(ui.key(press(KeyCode::Esc), true), Some(Action::Cancel));
        assert_eq!(ui.key(ctrl('c'), true), Some(Action::Cancel));
    }

    #[test]
    fn ctrl_c_empties_the_box_first_and_ends_on_the_second_press() {
        let mut ui = ui(Mode::Auto);
        typed(&mut ui, "half a thought", false);
        assert_eq!(ui.key(ctrl('c'), false), None);
        assert!(ui.composer.is_empty());
        assert_eq!(ui.key(ctrl('c'), false), None);
        let status = texts(&ui.frame(80, 12, Instant::now()))[11].clone();
        assert!(status.starts_with("  ⏵⏵ auto-run commands  ·  ctrl+c again to quit"), "{status}");
        // Any other key takes the warning back.
        typed(&mut ui, "x", false);
        assert_eq!(ui.key(press(KeyCode::Backspace), false), None);
        assert_eq!(ui.key(ctrl('c'), false), None);
        assert_eq!(ui.key(ctrl('c'), false), Some(Action::Quit));
        assert_eq!(ui.key(ctrl('d'), false), Some(Action::Quit));
        assert_eq!(ui.key(press(KeyCode::BackTab), false), Some(Action::CycleMode));
        assert_eq!(ui.key(ctrl('l'), false), Some(Action::Redraw));
    }

    #[test]
    fn the_keys_for_new_lines_and_command_names_work_in_the_box() {
        let mut ui = ui(Mode::Read);
        typed(&mut ui, "one", false);
        assert_eq!(ui.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT), false), None);
        typed(&mut ui, "two", false);
        assert_eq!(ui.key(ctrl('j'), false), None);
        assert_eq!(ui.composer.text(), "one\ntwo\n");
        let frame = ui.frame(80, 14, Instant::now());
        let rows = texts(&frame);
        assert_eq!(
            &rows[9..12],
            [format!(" │ one{} │", " ".repeat(71)), format!(" │ two{} │", " ".repeat(71)), format!(" │ {} │", " ".repeat(74))]
        );
        assert_eq!(frame.cursor, Some((3, 11)));
        assert!(rows[13].starts_with("  ◇ read only"), "{}", rows[13]);

        ui.composer.clear();
        typed(&mut ui, "/m", false);
        assert_eq!(texts(&ui.frame(80, 12, Instant::now()))[7], "   /mode  /model  /mcp");
        typed(&mut ui, "c", false);
        assert_eq!(ui.key(press(KeyCode::Tab), false), None);
        assert_eq!(ui.composer.text(), "/mcp ");
        // A key that is let go types nothing.
        let mut up = press(KeyCode::Char('z'));
        up.kind = KeyEventKind::Release;
        assert_eq!(ui.key(up, false), None);
        assert_eq!(ui.composer.text(), "/mcp ");
    }

    #[test]
    fn a_question_takes_the_place_of_the_box() {
        let mut ui = ui(Mode::Edit);
        ui.busy = Some(Busy { label: "Running…", since: Instant::now() });
        ui.card = Some(Card::new("run `cargo doc`", "it is not the test command"));
        let frame = ui.frame(100, 20, Instant::now());
        let rows = texts(&frame);
        assert_eq!(frame.cursor, None);
        assert_eq!(rows[11], format!(" │ $ cargo doc{} │", " ".repeat(83)));
        assert_eq!(rows[14].trim_end_matches([' ', '│']), " │ Do you want to run this command?");
        assert!(rows[15].starts_with(" │ ❯ 1. Yes"), "{}", rows[15]);
        assert!(rows[19].contains("↑↓ select · enter confirm"), "{}", rows[19]);
        assert!(!rows.iter().any(|row| row.contains("esc to interrupt")), "{rows:?}");
        assert_eq!(ui.card.as_mut().unwrap().key(press(KeyCode::Char('2'))), Some(Choice::Always));
    }

    #[test]
    fn a_long_conversation_scrolls_and_shows_where_it_is() {
        let mut ui = ui(Mode::Edit);
        ui.view.note((1..=40).map(|n| Line::styled(format!("row {n}"), Style::fg(FG))).collect());
        let rows = texts(&ui.frame(60, 15, Instant::now()));
        assert_eq!(rows[0], "  row 31");
        assert_eq!(rows[9], format!("  row 40{}┃", " ".repeat(51)));
        assert!(!rows[0].ends_with('┃'));

        assert_eq!(ui.key(press(KeyCode::PageUp), false), None);
        assert_eq!(texts(&ui.frame(60, 15, Instant::now()))[0], "  row 22");
        ui.wheel(false);
        assert_eq!(texts(&ui.frame(60, 15, Instant::now()))[0], "  row 25");
        for _ in 0..20 {
            ui.wheel(true);
        }
        let rows = texts(&ui.frame(60, 15, Instant::now()));
        assert_eq!(rows[0], format!("  row 1{}┃", " ".repeat(52)));
        assert_eq!(ui.key(press(KeyCode::Esc), false), None);
        assert_eq!(texts(&ui.frame(60, 15, Instant::now()))[0], "  row 31");
    }

    #[test]
    fn the_status_line_counts_the_context_and_gives_way_in_a_narrow_window() {
        let mut ui = ui(Mode::Edit);
        ui.chrome.sandboxed = true;
        ui.view.tokens = 870;
        ui.seconds = 61.4;
        let frame = ui.frame(80, 12, Instant::now());
        assert_eq!(texts(&frame)[11], format!("  ? ask before commands  · sandboxed{}87% context  ·  0 calls  ·  61s", " ".repeat(11)));
        let status = &frame.rows[11];
        assert!(status.spans.iter().any(|span| span.text == "87% context" && span.style.fg == Some(ERROR)));

        let rows = texts(&ui.frame(20, 12, Instant::now()));
        assert_eq!(rows[0], "the window is too s…");
        assert_eq!(rows.len(), 12);
        // A window too low for everything keeps the box and the status line.
        let frame = ui.frame(60, 6, Instant::now());
        assert_eq!(frame.rows.len(), 6);
        assert_eq!(frame.cursor, Some((3, 3)));
    }
}
