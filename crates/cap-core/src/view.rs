//! Which lines of which files the model has in front of it.
//!
//! An edit by line number is only safe on lines the model has seen in their current state.
//! `Views` records what `read_file` and edit results showed and forgets it when the file changes.

use crate::edit::lines_of;
use std::collections::HashMap;
use std::hash::{DefaultHasher, Hash, Hasher};

fn hash(text: &str) -> u64 {
    let mut h = DefaultHasher::new();
    text.hash(&mut h);
    h.finish()
}

#[derive(Default)]
struct View {
    hash: u64,
    /// Line ranges shown, 1-based and inclusive, sorted and merged.
    seen: Vec<(usize, usize)>,
}

impl View {
    fn add(&mut self, from: usize, to: usize) {
        if from == 0 || to < from {
            return;
        }
        self.seen.push((from, to));
        self.seen.sort_unstable();
        let mut merged: Vec<(usize, usize)> = Vec::with_capacity(self.seen.len());
        for &(a, b) in &self.seen {
            match merged.last_mut() {
                Some(last) if a <= last.1 + 1 => last.1 = last.1.max(b),
                _ => merged.push((a, b)),
            }
        }
        self.seen = merged;
    }

    fn covers(&self, from: usize, to: usize) -> bool {
        self.seen.iter().any(|&(a, b)| a <= from && to <= b)
    }
}

/// `old_len` lines at line `start` were replaced by `new_len` lines.
#[derive(Clone, Copy, Debug)]
pub struct Change {
    pub start: usize,
    pub old_len: usize,
    pub new_len: usize,
}

#[derive(Default)]
pub struct Views {
    files: HashMap<String, View>,
}

impl Views {
    /// Lines `from..=to` of `path`, whose whole text is `text`, were shown to the model.
    pub fn saw(&mut self, path: &str, text: &str, from: usize, to: usize) {
        let now = hash(text);
        let view = self.files.entry(path.to_string()).or_default();
        if view.hash != now {
            *view = View { hash: now, seen: Vec::new() };
        }
        view.add(from, to);
    }

    /// Whether a `replace_lines` on `start..=end` acts on lines the model has seen as they are now.
    /// An insertion (`end = start - 1`) needs one of the two lines it goes between.
    pub fn has_seen(&self, path: &str, text: &str, start: usize, end: usize) -> bool {
        let total = lines_of(text).len();
        if total == 0 {
            return true;
        }
        let Some(view) = self.files.get(path).filter(|v| v.hash == hash(text)) else { return false };
        if end >= start {
            return view.covers(start, end);
        }
        (start > 1 && view.covers(start - 1, start - 1)) || (start <= total && view.covers(start, start))
    }

    /// The file went from `old_text` to `new_text` by `change`.
    /// Lines above the edit stay seen; lines below stay seen only when their numbers did not move.
    /// `shown` is the range the edit result printed.
    pub fn edited(&mut self, path: &str, old_text: &str, new_text: &str, change: Change, shown: Option<(usize, usize)>) {
        let Change { start, old_len, new_len } = change;
        let view = self.files.entry(path.to_string()).or_default();
        let before = if view.hash == hash(old_text) { std::mem::take(&mut view.seen) } else { Vec::new() };
        *view = View { hash: hash(new_text), seen: Vec::new() };
        let after = start + old_len;
        for (a, b) in before {
            if a < start {
                view.add(a, b.min(start - 1));
            }
            if old_len == new_len && b >= after {
                view.add(a.max(after), b);
            }
        }
        if let Some((from, to)) = shown {
            view.add(from, to);
        }
    }

    pub fn forget(&mut self, path: &str) {
        self.files.remove(path);
    }

    pub fn clear(&mut self) {
        self.files.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEXT: &str = "1\n2\n3\n4\n5\n6\n7\n8\n";

    #[test]
    fn only_lines_that_were_shown_count_as_seen() {
        let mut v = Views::default();
        assert!(!v.has_seen("a.py", TEXT, 2, 3));
        v.saw("a.py", TEXT, 1, 3);
        v.saw("a.py", TEXT, 4, 5);
        assert!(v.has_seen("a.py", TEXT, 2, 5));
        assert!(!v.has_seen("a.py", TEXT, 5, 6));
        assert!(!v.has_seen("b.py", TEXT, 1, 1));
        // An insertion needs a neighbour: before line 6 (line 5 was seen), not before line 8.
        assert!(v.has_seen("a.py", TEXT, 6, 5));
        assert!(!v.has_seen("a.py", TEXT, 8, 7));
        // Appending needs the last line.
        assert!(!v.has_seen("a.py", TEXT, 9, 8));
        v.saw("a.py", TEXT, 8, 8);
        assert!(v.has_seen("a.py", TEXT, 9, 8));
        // An empty file has nothing to see.
        assert!(v.has_seen("new.py", "", 1, 0));
    }

    #[test]
    fn a_changed_file_has_to_be_seen_again() {
        let mut v = Views::default();
        v.saw("a.py", TEXT, 1, 8);
        let changed = TEXT.replace("4\n", "four\n");
        assert!(!v.has_seen("a.py", &changed, 1, 1));
        v.saw("a.py", &changed, 2, 2);
        assert!(v.has_seen("a.py", &changed, 2, 2) && !v.has_seen("a.py", &changed, 1, 1));
        v.forget("a.py");
        assert!(!v.has_seen("a.py", &changed, 2, 2));
    }

    #[test]
    fn an_edit_keeps_what_did_not_move() {
        let mut v = Views::default();
        v.saw("a.py", TEXT, 1, 8);
        // Lines 4-5 become three lines: everything below moved.
        let grown = "1\n2\n3\na\nb\nc\n6\n7\n8\n";
        v.edited("a.py", TEXT, grown, Change { start: 4, old_len: 2, new_len: 3 }, Some((2, 8)));
        assert!(v.has_seen("a.py", grown, 1, 8));
        assert!(!v.has_seen("a.py", grown, 9, 9));

        // Same line count: lines below keep their numbers and stay seen; the changed lines do not.
        let mut v = Views::default();
        v.saw("a.py", TEXT, 1, 8);
        let same = TEXT.replace("4\n", "four\n");
        v.edited("a.py", TEXT, &same, Change { start: 4, old_len: 1, new_len: 1 }, None);
        assert!(v.has_seen("a.py", &same, 1, 3) && v.has_seen("a.py", &same, 5, 8));
        assert!(!v.has_seen("a.py", &same, 4, 4));

        // An edit on a file that changed behind the record keeps nothing but what the result shows.
        let mut v = Views::default();
        v.saw("a.py", "old\n", 1, 1);
        v.edited("a.py", TEXT, &same, Change { start: 4, old_len: 1, new_len: 1 }, Some((3, 5)));
        assert!(v.has_seen("a.py", &same, 3, 5) && !v.has_seen("a.py", &same, 1, 1));
    }
}
