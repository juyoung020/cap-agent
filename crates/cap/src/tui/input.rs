//! The message being typed: editing keys, earlier messages, and the names of the commands.

use super::text::{char_width, clean, width};

/// The commands a line that starts with a slash can name.
pub const COMMANDS: [&str; 8] = ["help", "status", "mode", "model", "clear", "skills", "mcp", "quit"];

/// The text in the input box and the place of the caret in it.
#[derive(Default)]
pub struct Composer {
    text: String,
    /// The caret, as a byte index into `text`.
    cursor: usize,
    history: Vec<String>,
    /// Which earlier message is shown, while the person walks through them.
    at: Option<usize>,
    /// What was typed before the walk started.
    draft: String,
}

impl Composer {
    #[cfg(test)]
    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.at = None;
    }

    /// Put typed or pasted text at the caret.
    pub fn insert(&mut self, text: &str) {
        let text = clean(&text.replace("\r\n", "\n").replace('\r', "\n"));
        self.text.insert_str(self.cursor, &text);
        self.cursor += text.len();
    }

    pub fn insert_char(&mut self, c: char) {
        self.insert(c.encode_utf8(&mut [0; 4]));
    }

    pub fn newline(&mut self) {
        self.text.insert(self.cursor, '\n');
        self.cursor += 1;
    }

    pub fn backspace(&mut self) {
        if let Some(c) = self.text[..self.cursor].chars().next_back() {
            self.cursor -= c.len_utf8();
            self.text.remove(self.cursor);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor < self.text.len() {
            self.text.remove(self.cursor);
        }
    }

    pub fn left(&mut self) {
        if let Some(c) = self.text[..self.cursor].chars().next_back() {
            self.cursor -= c.len_utf8();
        }
    }

    pub fn right(&mut self) {
        if let Some(c) = self.text[self.cursor..].chars().next() {
            self.cursor += c.len_utf8();
        }
    }

    fn line_start(&self) -> usize {
        self.text[..self.cursor].rfind('\n').map_or(0, |at| at + 1)
    }

    fn line_end(&self) -> usize {
        self.text[self.cursor..].find('\n').map_or(self.text.len(), |at| self.cursor + at)
    }

    pub fn home(&mut self) {
        self.cursor = self.line_start();
    }

    pub fn end(&mut self) {
        self.cursor = self.line_end();
    }

    /// Remove what stands between the start of the line and the caret.
    pub fn kill_to_start(&mut self) {
        let start = self.line_start();
        self.text.drain(start..self.cursor);
        self.cursor = start;
    }

    /// Remove the word before the caret.
    pub fn kill_word(&mut self) {
        let before = &self.text[..self.cursor];
        let word_end = before.trim_end_matches(' ').len();
        let start = before[..word_end].rfind([' ', '\n']).map_or(0, |at| at + 1);
        self.text.drain(start..self.cursor);
        self.cursor = start;
    }

    /// The caret at the given column of the line that starts at `start`, or at the line's end.
    fn place(&mut self, start: usize, column: usize) {
        let mut used = 0;
        self.cursor = start;
        for c in self.text[start..].chars() {
            if c == '\n' || used + char_width(c) > column {
                break;
            }
            used += char_width(c);
            self.cursor += c.len_utf8();
        }
    }

    fn show(&mut self, text: String) {
        self.cursor = text.len();
        self.text = text;
    }

    /// One line up in a text of several lines; in the first line, the message sent before.
    pub fn up(&mut self) {
        let start = self.line_start();
        if start > 0 {
            let column = width(&self.text[start..self.cursor]);
            let above = self.text[..start - 1].rfind('\n').map_or(0, |at| at + 1);
            self.place(above, column);
            return;
        }
        let earlier = match self.at {
            None if self.history.is_empty() => return,
            None => self.history.len() - 1,
            Some(0) => return,
            Some(at) => at - 1,
        };
        if self.at.is_none() {
            self.draft = std::mem::take(&mut self.text);
        }
        self.at = Some(earlier);
        self.show(self.history[earlier].clone());
    }

    /// One line down; in the last line, the message sent after the one shown.
    pub fn down(&mut self) {
        let end = self.line_end();
        if end < self.text.len() {
            let column = width(&self.text[self.line_start()..self.cursor]);
            self.place(end + 1, column);
            return;
        }
        match self.at {
            None => {}
            Some(at) if at + 1 < self.history.len() => {
                self.at = Some(at + 1);
                self.show(self.history[at + 1].clone());
            }
            Some(_) => {
                self.at = None;
                let draft = std::mem::take(&mut self.draft);
                self.show(draft);
            }
        }
    }

    /// The message, for sending. The box is empty afterwards.
    pub fn take(&mut self) -> String {
        let text = std::mem::take(&mut self.text);
        self.cursor = 0;
        self.at = None;
        self.draft.clear();
        if !text.trim().is_empty() && self.history.last() != Some(&text) {
            self.history.push(text.clone());
        }
        text
    }

    /// The rows the text takes in a box `width` columns wide, and the caret's row and column.
    pub fn layout(&self, width: usize) -> (Vec<String>, (usize, usize)) {
        let width = width.max(2);
        let mut rows = Vec::new();
        let mut row = String::new();
        let mut used = 0;
        let mut caret = (0, 0);
        for (at, c) in self.text.char_indices() {
            if c == '\n' {
                if at == self.cursor {
                    caret = (rows.len(), used);
                }
                rows.push(std::mem::take(&mut row));
                used = 0;
                continue;
            }
            let w = char_width(c);
            if used + w > width {
                rows.push(std::mem::take(&mut row));
                used = 0;
            }
            if at == self.cursor {
                caret = (rows.len(), used);
            }
            row.push(c);
            used += w;
        }
        if self.cursor == self.text.len() {
            caret = (rows.len(), used);
        }
        rows.push(row);
        (rows, caret)
    }

    /// The commands that start with what is typed, while a command name is being typed.
    pub fn completions(&self) -> Vec<&'static str> {
        match self.text.strip_prefix('/') {
            Some(typed) if self.cursor == self.text.len() && !typed.contains([' ', '\n']) => {
                COMMANDS.into_iter().filter(|name| name.starts_with(typed) && *name != typed).collect()
            }
            _ => Vec::new(),
        }
    }

    /// Finish the command name as far as it is certain.
    pub fn complete(&mut self) {
        let names = self.completions();
        let Some(first) = names.first() else {
            return;
        };
        let shared = names.iter().fold(first.len(), |n, name| first.bytes().zip(name.bytes()).take(n).take_while(|(a, b)| a == b).count());
        let text = if names.len() == 1 { format!("/{first} ") } else { format!("/{}", &first[..shared]) };
        self.show(text);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn typed(text: &str) -> Composer {
        let mut composer = Composer::default();
        composer.insert(text);
        composer
    }

    #[test]
    fn typing_and_removing_work_on_whole_letters() {
        let mut c = typed("가나다");
        c.left();
        c.backspace();
        assert_eq!(c.text(), "가다");
        c.insert_char('x');
        assert_eq!(c.text(), "가x다");
        c.delete();
        assert_eq!(c.text(), "가x");
        c.delete();
        c.home();
        c.backspace();
        c.right();
        c.insert("\ty\r\nz\x07");
        assert_eq!(c.text(), "가    y\nzx");
    }

    #[test]
    fn the_line_keys_stay_in_their_line() {
        let mut c = typed("one two\nthree four");
        c.kill_word();
        assert_eq!(c.text(), "one two\nthree ");
        c.kill_word();
        assert_eq!(c.text(), "one two\n");
        c.insert("abc");
        c.home();
        c.insert("x");
        assert_eq!(c.text(), "one two\nxabc");
        c.end();
        c.kill_to_start();
        assert_eq!(c.text(), "one two\n");
    }

    #[test]
    fn up_and_down_move_between_lines_and_keep_the_column() {
        let mut c = typed("abcdef\n한글\nxyz");
        c.up();
        c.insert("|");
        assert_eq!(c.text(), "abcdef\n한|글\nxyz");
        c.up();
        c.insert("|");
        assert_eq!(c.text(), "abc|def\n한|글\nxyz");
        c.down();
        c.down();
        c.insert("|");
        assert_eq!(c.text(), "abc|def\n한|글\nxyz|");
    }

    #[test]
    fn up_in_the_first_line_brings_back_what_was_sent() {
        let mut c = typed("first");
        assert_eq!(c.take(), "first");
        c.insert("second");
        c.take();
        c.insert("second");
        c.take();
        c.insert("draft");
        c.up();
        assert_eq!(c.text(), "second");
        c.up();
        assert_eq!(c.text(), "first");
        c.up();
        assert_eq!(c.text(), "first");
        c.down();
        c.down();
        assert_eq!(c.text(), "draft");
        c.down();
        assert_eq!(c.text(), "draft");
        assert!(c.take() == "draft" && c.is_empty());
    }

    #[test]
    fn the_box_breaks_long_lines_and_knows_where_the_caret_is() {
        let c = typed("abcdefgh\n한글한");
        assert_eq!(c.layout(5), (vec!["abcde".to_string(), "fgh".to_string(), "한글".to_string(), "한".to_string()], (3, 2)));
        let mut c = typed("abcde");
        assert_eq!(c.layout(5).1, (0, 5));
        c.home();
        assert_eq!(c.layout(5).1, (0, 0));
        assert_eq!(Composer::default().layout(5), (vec![String::new()], (0, 0)));
        let mut c = typed("ab\ncd");
        c.up();
        c.end();
        assert_eq!(c.layout(5).1, (0, 2));
    }

    #[test]
    fn a_command_name_is_finished_as_far_as_it_is_certain() {
        let mut c = typed("/m");
        assert_eq!(c.completions(), ["mode", "model", "mcp"]);
        c.insert("o");
        c.complete();
        assert_eq!(c.text(), "/mode");
        assert_eq!(c.completions(), ["model"]);
        let mut c = typed("/st");
        c.complete();
        assert_eq!(c.text(), "/status ");
        assert!(c.completions().is_empty());
        assert!(typed("fix /m").completions().is_empty());
        let mut c = typed("/zzz");
        c.complete();
        assert_eq!(c.text(), "/zzz");
    }
}
