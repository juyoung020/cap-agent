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
}
