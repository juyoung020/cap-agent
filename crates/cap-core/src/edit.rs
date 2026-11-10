//! Edit engine: replace `old` with `new` in a text, tolerant of the slips small models make
//! (wrong indentation, trailing spaces, copied line numbers) and strict about ambiguity.
//!
//! All functions here work on text with `\n` line ends; `Eol` converts at the file boundary.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Eol {
    Lf,
    CrLf,
}

impl Eol {
    /// The line end a file uses (the majority of its line ends).
    pub fn detect(text: &str) -> Self {
        let crlf = text.matches("\r\n").count();
        let lf = text.matches('\n').count() - crlf;
        if crlf > lf { Eol::CrLf } else { Eol::Lf }
    }

    pub fn to_lf(text: &str) -> String {
        text.replace("\r\n", "\n")
    }

    /// Text with `\n` line ends back to this file's line ends.
    pub fn restore(self, text: &str) -> String {
        match self {
            Eol::Lf => text.to_string(),
            Eol::CrLf => text.replace('\n', "\r\n"),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Matched {
    Exact,
    /// Matched after ignoring spaces at line ends.
    TrailingSpace,
    /// Matched after ignoring indentation; `new` was re-indented to fit the file.
    Indentation,
    /// `old` carried the `12| ` line numbers of a `read_file` result.
    LineNumbers,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Applied {
    pub text: String,
    /// First changed line, 1-based.
    pub line: usize,
    pub old_lines: usize,
    pub new_lines: usize,
    pub matched: Matched,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EditError {
    /// `old` and `new` are the same text.
    Noop,
    EmptyOld,
    NotFound {
        closest: Option<(usize, String)>,
    },
    Ambiguous {
        lines: Vec<usize>,
    },
}

impl EditError {
    /// What the model is told. Short, and says what to do next.
    pub fn message(&self, path: &str) -> String {
        match self {
            EditError::Noop => {
                "old and new are the same, so this changes nothing. If this part is already the way you want it, go on to the next step."
                    .to_string()
            }
            EditError::EmptyOld => "old is empty. Give the exact lines to replace, or use write_file for a new file.".to_string(),
            EditError::NotFound { closest: Some((line, shown)) } => {
                format!(
                    "old was not found in {path}. The closest place is line {line}:\n{shown}\nCopy old from these lines exactly (without the line numbers)."
                )
            }
            EditError::NotFound { closest: None } => {
                format!("old was not found in {path}. Read the file and copy the lines exactly.")
            }
            EditError::Ambiguous { lines } => {
                let at: Vec<String> = lines.iter().take(8).map(usize::to_string).collect();
                format!(
                    "old appears {} times in {path} (lines {}). Add a neighbouring line to old so it names one place.",
                    lines.len(),
                    at.join(", ")
                )
            }
        }
    }
}

fn line_of(text: &str, byte: usize) -> usize {
    text[..byte].matches('\n').count() + 1
}

fn count_lines(s: &str) -> usize {
    if s.is_empty() { 0 } else { s.trim_end_matches('\n').split('\n').count() }
}

fn indent_of(line: &str) -> &str {
    &line[..line.len() - line.trim_start().len()]
}

/// Blank lines at both ends removed; returns the lines and how many were removed at each end.
fn trim_blank_edges<'a>(lines: &[&'a str]) -> (Vec<&'a str>, usize, usize) {
    let head = lines.iter().take_while(|l| l.trim().is_empty()).count();
    let tail = lines[head..].iter().rev().take_while(|l| l.trim().is_empty()).count();
    (lines[head..lines.len() - tail].to_vec(), head, tail)
}

fn find_blocks(hay: &[&str], needle: &[&str], norm: fn(&str) -> &str) -> Vec<usize> {
    if needle.is_empty() || needle.len() > hay.len() {
        return Vec::new();
    }
    (0..=hay.len() - needle.len()).filter(|&i| needle.iter().enumerate().all(|(k, n)| norm(hay[i + k]) == norm(n))).collect()
}

/// `12| text` lines as `read_file` prints them, with the numbers removed.
fn strip_line_numbers(s: &str) -> Option<String> {
    let mut out = Vec::new();
    let mut any = false;
    for line in s.split('\n') {
        if line.trim().is_empty() {
            out.push("");
            continue;
        }
        let t = line.trim_start();
        let digits = t.chars().take_while(char::is_ascii_digit).count();
        let rest = t[digits..].strip_prefix('|')?;
        if digits == 0 {
            return None;
        }
        any = true;
        out.push(rest.strip_prefix(' ').unwrap_or(rest));
    }
    any.then(|| out.join("\n"))
}

fn closest(hay: &[&str], needle: &[&str]) -> Option<(usize, String)> {
    let anchor = needle.iter().map(|l| l.trim()).find(|l| l.len() >= 4)?;
    let at = hay.iter().position(|l| l.trim() == anchor).or_else(|| hay.iter().position(|l| l.contains(anchor)))?;
    let from = at.saturating_sub(1);
    let to = (at + needle.len().clamp(1, 10) + 1).min(hay.len());
    let shown: Vec<String> = (from..to).map(|i| format!("{}| {}", i + 1, hay[i])).collect();
    Some((at + 1, shown.join("\n")))
}

/// Replace `old` with `new` in `text`. `old` must name exactly one place.
pub fn apply_edit(text: &str, old: &str, new: &str) -> Result<Applied, EditError> {
    if old == new {
        return Err(EditError::Noop);
    }
    if old.trim().is_empty() {
        return Err(EditError::EmptyOld);
    }

    let hits: Vec<usize> = text.match_indices(old).map(|(i, _)| i).collect();
    match hits.len() {
        1 => {
            let at = hits[0];
            let mut out = String::with_capacity(text.len() + new.len());
            out.push_str(&text[..at]);
            out.push_str(new);
            out.push_str(&text[at + old.len()..]);
            return Ok(Applied {
                text: out,
                line: line_of(text, at),
                old_lines: count_lines(old),
                new_lines: count_lines(new),
                matched: Matched::Exact,
            });
        }
        0 => {}
        _ => return Err(EditError::Ambiguous { lines: hits.iter().map(|&i| line_of(text, i)).collect() }),
    }

    let hay: Vec<&str> = text.split('\n').collect();
    let old_lines: Vec<&str> = old.split('\n').collect();
    let (needle, cut_head, cut_tail) = trim_blank_edges(&old_lines);
    type Trim = fn(&str) -> &str;
    let levels: [(Trim, Matched); 2] = [(str::trim_end, Matched::TrailingSpace), (str::trim, Matched::Indentation)];
    for (norm, matched) in levels {
        let starts = find_blocks(&hay, &needle, norm);
        match starts.len() {
            0 => continue,
            1 => {}
            _ => return Err(EditError::Ambiguous { lines: starts.iter().map(|s| s + 1).collect() }),
        }
        let start = starts[0];
        // `new` loses the same blank edge lines that were ignored in `old`.
        let mut new_lines: Vec<&str> = if new.is_empty() { Vec::new() } else { new.split('\n').collect() };
        let head = new_lines.iter().take(cut_head).take_while(|l| l.trim().is_empty()).count();
        new_lines.drain(..head);
        let tail = new_lines.iter().rev().take(cut_tail).take_while(|l| l.trim().is_empty()).count();
        new_lines.truncate(new_lines.len() - tail);

        let fitted: Vec<String> = if matched == Matched::Indentation {
            let (file_indent, old_indent) = (indent_of(hay[start]), indent_of(needle[0]));
            new_lines
                .iter()
                .map(|l| match l.strip_prefix(old_indent) {
                    _ if l.trim().is_empty() => String::new(),
                    Some(rest) => format!("{file_indent}{rest}"),
                    None => l.to_string(),
                })
                .collect()
        } else {
            new_lines.iter().map(|l| l.to_string()).collect()
        };

        let mut out: Vec<&str> = Vec::with_capacity(hay.len() + fitted.len());
        out.extend_from_slice(&hay[..start]);
        out.extend(fitted.iter().map(String::as_str));
        out.extend_from_slice(&hay[start + needle.len()..]);
        return Ok(Applied { text: out.join("\n"), line: start + 1, old_lines: needle.len(), new_lines: fitted.len(), matched });
    }

    if let Some(bare_old) = strip_line_numbers(old) {
        let bare_new = strip_line_numbers(new).unwrap_or_else(|| new.to_string());
        if bare_old != old
            && let Ok(mut applied) = apply_edit(text, &bare_old, &bare_new)
        {
            applied.matched = Matched::LineNumbers;
            return Ok(applied);
        }
    }

    Err(EditError::NotFound { closest: closest(&hay, &needle) })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LineEdit {
    pub text: String,
    /// First line of the replaced range, 1-based.
    pub start: usize,
    pub old_lines: usize,
    pub new_lines: usize,
    /// `new` came without any indentation and got that of the lines it replaces.
    pub indented: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LineError {
    /// The range does not lie inside the file, which has `total` lines.
    BadRange { total: usize },
    /// The lines already read exactly like `new`.
    Noop,
}

/// The lines of a text; the line end after the last line does not start another one.
pub fn lines_of(text: &str) -> Vec<&str> {
    if text.is_empty() { Vec::new() } else { text.strip_suffix('\n').unwrap_or(text).split('\n').collect() }
}

/// The indentation that `new` lacks: none of its lines has any, and every line it replaces has
/// some. A small model often sends the changed statement alone, without its leading spaces.
fn missing_indent<'a>(old: &[&'a str], new: &[&str]) -> Option<&'a str> {
    let old: Vec<&'a str> = old.iter().copied().filter(|l| !l.trim().is_empty()).collect();
    let new: Vec<&str> = new.iter().copied().filter(|l| !l.trim().is_empty()).collect();
    let indent = indent_of(old.first()?);
    let fits = !new.is_empty() && old.iter().all(|l| !indent_of(l).is_empty()) && new.iter().all(|l| indent_of(l).is_empty());
    fits.then_some(indent)
}

/// Replace lines `start..=end` (1-based) with `new`. `end = start - 1` inserts before line `start`
/// (`start = total + 1` appends); an empty `new` deletes the lines. A `new` without any
/// indentation that replaces indented lines is indented like the first of them.
pub fn replace_lines(text: &str, start: usize, end: usize, new: &str) -> Result<LineEdit, LineError> {
    let lines = lines_of(text);
    let total = lines.len();
    if start < 1 || start > total + 1 || end + 1 < start || end > total {
        return Err(LineError::BadRange { total });
    }
    // Line numbers copied from a `read_file` result are not part of the file.
    let new = strip_line_numbers(new).unwrap_or_else(|| new.to_string());
    let fresh = lines_of(&new);
    let indent = missing_indent(&lines[start - 1..end], &fresh);
    let shifted: Vec<String>;
    let fresh: Vec<&str> = match indent {
        Some(indent) => {
            shifted = fresh.iter().map(|l| if l.trim().is_empty() { l.to_string() } else { format!("{indent}{l}") }).collect();
            shifted.iter().map(String::as_str).collect()
        }
        None => fresh,
    };
    if lines[start - 1..end] == fresh[..] {
        return Err(LineError::Noop);
    }
    let out: Vec<&str> = lines[..start - 1].iter().chain(&fresh).chain(&lines[end..]).copied().collect();
    let mut joined = out.join("\n");
    if !out.is_empty() && (text.is_empty() || text.ends_with('\n')) {
        joined.push('\n');
    }
    Ok(LineEdit { text: joined, start, old_lines: end + 1 - start, new_lines: fresh.len(), indented: indent.is_some() })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SRC: &str = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n";

    #[test]
    fn exact_single_match_is_replaced() {
        let a = apply_edit(SRC, "def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b").unwrap();
        assert_eq!(a.text, SRC.replacen("a - b", "a + b", 1));
        assert_eq!((a.line, a.old_lines, a.new_lines, a.matched), (1, 2, 2, Matched::Exact));
    }

    #[test]
    fn ambiguous_old_is_refused_with_line_numbers() {
        let e = apply_edit(SRC, "    return a - b", "    return a + b").unwrap_err();
        assert_eq!(e, EditError::Ambiguous { lines: vec![2, 6] });
        assert!(e.message("calc.py").contains("lines 2, 6"));
    }

    #[test]
    fn same_old_and_new_changes_nothing() {
        assert_eq!(apply_edit(SRC, "return a - b", "return a - b"), Err(EditError::Noop));
        assert_eq!(apply_edit(SRC, "  \n", "x"), Err(EditError::EmptyOld));
    }

    #[test]
    fn wrong_indentation_still_matches_and_new_is_reindented() {
        let src = "class A:\n    def f(self):\n        x = 1\n        return x\n";
        let a = apply_edit(src, "x = 1\nreturn x", "x = 2\nif x:\n    return x").unwrap();
        assert_eq!(a.matched, Matched::Indentation);
        assert_eq!(a.text, "class A:\n    def f(self):\n        x = 2\n        if x:\n            return x\n");
        assert_eq!((a.line, a.old_lines, a.new_lines), (3, 2, 3));
    }

    #[test]
    fn trailing_spaces_and_blank_edge_lines_are_ignored() {
        let src = "a = 1  \nb = 2\nc = 3\n";
        let a = apply_edit(src, "\na = 1\nb = 2\n", "\na = 10\nb = 2\n").unwrap();
        assert_eq!(a.matched, Matched::TrailingSpace);
        assert_eq!(a.text, "a = 10\nb = 2\nc = 3\n");
    }

    #[test]
    fn copied_line_numbers_are_removed() {
        let a = apply_edit(SRC, "1| def add(a, b):\n2|     return a - b", "1| def add(a, b):\n2|     return a + b").unwrap();
        assert_eq!(a.matched, Matched::LineNumbers);
        assert!(a.text.starts_with("def add(a, b):\n    return a + b\n"));
    }

    #[test]
    fn deleting_lines_with_an_empty_new() {
        let src = "import os\nimport sys\nprint(1)\n";
        assert_eq!(apply_edit(src, "import sys\n", "").unwrap().text, "import os\nprint(1)\n");
        let a = apply_edit(src, "  import sys", "").unwrap();
        assert_eq!(a.text, "import os\nprint(1)\n");
    }

    #[test]
    fn not_found_shows_the_closest_place() {
        let e = apply_edit(SRC, "def sub(a, b):\n    return b - a", "x").unwrap_err();
        let EditError::NotFound { closest: Some((line, shown)) } = &e else { panic!("{e:?}") };
        assert_eq!(*line, 5);
        assert!(shown.contains("5| def sub(a, b):") && shown.contains("6|     return a - b"));
        assert_eq!(apply_edit(SRC, "zzzz qqqq", "x"), Err(EditError::NotFound { closest: None }));
    }

    #[test]
    fn line_ends_are_detected_and_restored() {
        let crlf = "a = 1\r\nb = 2\r\n";
        assert_eq!(Eol::detect(crlf), Eol::CrLf);
        assert_eq!(Eol::detect("a\nb\n"), Eol::Lf);
        let a = apply_edit(&Eol::to_lf(crlf), "b = 2", "b = 3").unwrap();
        assert_eq!(Eol::CrLf.restore(&a.text), "a = 1\r\nb = 3\r\n");
    }

    #[test]
    fn a_line_range_is_replaced_inserted_or_deleted() {
        let text = "a\nb\nc\n";
        let r = replace_lines(text, 2, 2, "B1\nB2").unwrap();
        assert_eq!((r.text.as_str(), r.start, r.old_lines, r.new_lines), ("a\nB1\nB2\nc\n", 2, 1, 2));
        // `end = start - 1` inserts before `start`; `start = total + 1` appends.
        assert_eq!(replace_lines(text, 1, 0, "top\n").unwrap().text, "top\na\nb\nc\n");
        assert_eq!(replace_lines(text, 4, 3, "d").unwrap().text, "a\nb\nc\nd\n");
        // An empty `new` deletes; "\n" is one empty line.
        assert_eq!(replace_lines(text, 1, 2, "").unwrap().text, "c\n");
        assert_eq!(replace_lines(text, 2, 2, "\n").unwrap().text, "a\n\nc\n");
        // The file's last line end stays the way it was.
        assert_eq!(replace_lines("a\nb", 2, 2, "B").unwrap().text, "a\nB");
        assert_eq!(replace_lines("", 1, 0, "first").unwrap().text, "first\n");
        assert_eq!(replace_lines("a\n", 1, 1, "").unwrap().text, "");
    }

    #[test]
    fn copied_line_numbers_are_dropped_and_bad_ranges_are_named() {
        let text = "def f():\n    return 1\n";
        assert_eq!(replace_lines(text, 2, 2, "2|     return 2").unwrap().text, "def f():\n    return 2\n");
        assert_eq!(replace_lines(text, 2, 2, "    return 1"), Err(LineError::Noop));
        assert_eq!(replace_lines(text, 1, 0, ""), Err(LineError::Noop));
        for (start, end) in [(0, 1), (4, 4), (2, 3), (3, 1)] {
            assert_eq!(replace_lines(text, start, end, "x"), Err(LineError::BadRange { total: 2 }), "{start}-{end}");
        }
    }

    #[test]
    fn new_lines_without_indentation_get_that_of_the_lines_they_replace() {
        let text = "def f(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total\n";
        // The statement alone, as a small model sends it.
        let r = replace_lines(text, 5, 5, "return total / len(xs)").unwrap();
        assert!(r.indented && r.text.ends_with("\n    return total / len(xs)\n"), "{}", r.text);
        let r = replace_lines(text, 4, 4, "4| total += 2 * x").unwrap();
        assert!(r.indented && r.text.contains("\n        total += 2 * x\n"), "{}", r.text);
        // Several flat lines move together; an empty line stays empty.
        let r = replace_lines(text, 2, 2, "total = 0\n\ncount = 0").unwrap();
        assert!(r.indented && r.text.starts_with("def f(xs):\n    total = 0\n\n    count = 0\n    for x"), "{}", r.text);
        assert_eq!(r.new_lines, 3);
        // Sent again, the line already reads like that.
        assert_eq!(replace_lines(text, 5, 5, "return total"), Err(LineError::Noop));

        // `new` that brings indentation of its own is written as it is.
        let r = replace_lines(text, 5, 5, "  return total").unwrap();
        assert!(!r.indented && r.text.ends_with("\n  return total\n"));
        let r = replace_lines(text, 4, 4, "if x:\n    total += x").unwrap();
        assert!(!r.indented && r.text.contains("\nif x:\n    total += x\n"));
        // So is one that replaces a line at the left edge, and one that is inserted.
        let r = replace_lines(text, 1, 2, "def g(xs):\ntotal = 1").unwrap();
        assert!(!r.indented && r.text.starts_with("def g(xs):\ntotal = 1\n"));
        let r = replace_lines(text, 6, 5, "print(f([1]))").unwrap();
        assert!(!r.indented && r.text.ends_with("    return total\nprint(f([1]))\n"));
        // Tabs are taken over as they are.
        assert_eq!(replace_lines("f() {\n\treturn 1;\n}\n", 2, 2, "return 2;").unwrap().text, "f() {\n\treturn 2;\n}\n");
    }
}
