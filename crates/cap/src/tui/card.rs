//! The question the window asks before an action that needs a yes, and the frame around a box.

use super::text::{BRAND, FG, Line, MUTED, Rgb, Style, clip, wrap};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// What the person answered.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Choice {
    Yes,
    /// Yes, and the same action needs no question for the rest of the session.
    Always,
    No,
    /// No, and the turn stops.
    NoAndCancel,
}

const ANSWERS: [(&str, Choice); 3] =
    [("Yes", Choice::Yes), ("Yes, and don't ask again for this during the session", Choice::Always), ("No  (esc)", Choice::No)];

/// An action that waits for a yes or a no.
pub struct Card {
    action: String,
    why: String,
    pick: usize,
}

impl Card {
    pub fn new(action: &str, why: &str) -> Card {
        Card { action: action.to_string(), why: why.to_string(), pick: 0 }
    }

    fn command(&self) -> Option<&str> {
        self.action.strip_prefix("run `").map(|rest| rest.strip_suffix('`').unwrap_or(rest))
    }

    fn title(&self) -> String {
        let file = self.action.strip_prefix("edit ").or_else(|| self.action.strip_prefix("write "));
        match file {
            Some(file) => format!("Do you want to make this edit to {file}?"),
            None if self.command().is_some() => "Do you want to run this command?".to_string(),
            None if self.why.contains("(MCP)") => "Do you want to call this outside tool?".to_string(),
            None => "Do you want to allow this?".to_string(),
        }
    }

    /// What a key answers; `None` while the person is still choosing.
    pub fn key(&mut self, key: KeyEvent) -> Option<Choice> {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Char('c' | 'C') if ctrl => return Some(Choice::NoAndCancel),
            KeyCode::Char(_) if ctrl => {}
            KeyCode::Up | KeyCode::BackTab => self.pick = (self.pick + ANSWERS.len() - 1) % ANSWERS.len(),
            KeyCode::Down | KeyCode::Tab => self.pick = (self.pick + 1) % ANSWERS.len(),
            KeyCode::Enter => return Some(ANSWERS[self.pick].1),
            KeyCode::Esc => return Some(Choice::No),
            KeyCode::Char('1' | 'y' | 'Y') => return Some(Choice::Yes),
            KeyCode::Char('2' | 'a' | 'A') => return Some(Choice::Always),
            KeyCode::Char('3' | 'n' | 'N') => return Some(Choice::No),
            _ => {}
        }
        None
    }

    /// The rows inside the frame, for a box `width` columns wide inside.
    pub fn rows(&self, width: usize) -> Vec<Line> {
        let shown = match self.command() {
            Some(command) => format!("$ {command}"),
            None => self.action.clone(),
        };
        let mut rows = wrap(&Line::styled(shown, Style::fg(FG)), width, 0, false);
        if rows.len() > 3 {
            rows.truncate(3);
            rows[2] = clip(&rows[2].clone().with("…", Style::fg(MUTED)), width);
        }
        if !self.why.is_empty() {
            rows.push(clip(&Line::styled(self.why.as_str(), Style::fg(MUTED)), width));
        }
        rows.push(Line::new());
        rows.push(clip(&Line::styled(self.title(), Style::fg(BRAND).bold()), width));
        for (at, (text, _)) in ANSWERS.iter().enumerate() {
            let row = if at == self.pick {
                Line::styled(format!("❯ {}. {text}", at + 1), Style::fg(BRAND).bold())
            } else {
                Line::styled(format!("  {}. {text}", at + 1), Style::fg(FG))
            };
            rows.push(clip(&row, width));
        }
        rows
    }
}

/// A row made exactly `width` columns wide, with the row's own background under all of it.
pub fn fill(line: &Line, width: usize) -> Line {
    let mut out = clip(line, width);
    let bg = out.bg.take();
    for span in &mut out.spans {
        span.style.bg = span.style.bg.or(bg);
    }
    let missing = width.saturating_sub(out.width());
    out.push(" ".repeat(missing), Style { bg, ..Style::default() });
    out
}

/// Rows in a rounded frame that is `width` columns wide with its margins.
pub fn boxed(rows: &[Line], width: usize, colour: Rgb) -> Vec<Line> {
    let inner = width.saturating_sub(6);
    let edge = Style::fg(colour);
    let bar = "─".repeat(inner + 2);
    let mut out = vec![Line::styled(format!(" ╭{bar}╮"), edge)];
    for row in rows {
        let mut line = Line::styled(" │ ", edge);
        line.spans.extend(fill(row, inner).spans);
        out.push(line.with(" │", edge));
    }
    out.push(Line::styled(format!(" ╰{bar}╯"), edge));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn texts(rows: &[Line]) -> Vec<String> {
        rows.iter().map(Line::text).collect()
    }

    #[test]
    fn the_question_names_what_is_asked_for() {
        let card = Card::new("run `cargo test`", "it is not the test command");
        assert_eq!(
            texts(&card.rows(60)),
            [
                "$ cargo test",
                "it is not the test command",
                "",
                "Do you want to run this command?",
                "❯ 1. Yes",
                "  2. Yes, and don't ask again for this during the session",
                "  3. No  (esc)",
            ]
        );
        assert_eq!(texts(&Card::new("edit calc.py", "").rows(60))[2], "Do you want to make this edit to calc.py?");
        let outside = Card::new("search docs", "it calls a tool of an outside server (MCP)");
        assert_eq!(texts(&outside.rows(60))[3], "Do you want to call this outside tool?");
        assert_eq!(texts(&Card::new("read ../notes.txt", "it is outside the project").rows(60))[3], "Do you want to allow this?");
    }

    #[test]
    fn the_keys_move_the_mark_and_answer() {
        let mut card = Card::new("edit a.py", "");
        assert_eq!(card.key(press(KeyCode::Down)), None);
        assert_eq!(card.key(press(KeyCode::Enter)), Some(Choice::Always));
        assert_eq!(card.key(press(KeyCode::Up)), None);
        assert_eq!(card.key(press(KeyCode::Up)), None);
        assert_eq!(card.key(press(KeyCode::Enter)), Some(Choice::No));
        assert_eq!(card.key(press(KeyCode::Char('y'))), Some(Choice::Yes));
        assert_eq!(card.key(press(KeyCode::Char('2'))), Some(Choice::Always));
        assert_eq!(card.key(press(KeyCode::Char('n'))), Some(Choice::No));
        assert_eq!(card.key(press(KeyCode::Esc)), Some(Choice::No));
        assert_eq!(card.key(press(KeyCode::Char('x'))), None);
        assert_eq!(card.key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)), Some(Choice::NoAndCancel));
    }

    #[test]
    fn a_long_command_takes_three_rows_at_most() {
        let card = Card::new(&format!("run `{}`", "word ".repeat(40).trim()), "");
        let rows = texts(&card.rows(30));
        assert!(rows[2].ends_with('…'), "{rows:?}");
        assert_eq!(rows[3], "");
        assert!(rows.iter().all(|row| super::super::text::width(row) <= 30), "{rows:?}");
    }

    #[test]
    fn a_frame_is_as_wide_as_asked_and_keeps_the_row_colour() {
        let rows = boxed(&[Line::styled("hi", Style::fg(FG)).on(BRAND)], 12, MUTED);
        assert_eq!(texts(&rows), [" ╭────────╮", " │ hi     │", " ╰────────╯"]);
        assert!(rows[1].spans[1..3].iter().all(|span| span.style.bg == Some(BRAND)));
        assert_eq!(fill(&Line::styled("abcdefgh", Style::default()), 5).text(), "abcd…");
    }
}
