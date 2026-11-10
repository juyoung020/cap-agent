//! Styled text for the conversation window: colours, display width, wrapping, light markdown
//! and a line diff.

use unicode_width::UnicodeWidthChar;

/// A colour as red, green and blue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rgb(pub u8, pub u8, pub u8);

pub const BG: Rgb = Rgb(0x0f, 0x0f, 0x12);
pub const SURFACE: Rgb = Rgb(0x16, 0x16, 0x1b);
pub const PANEL: Rgb = Rgb(0x1d, 0x1d, 0x24);
pub const BORDER: Rgb = Rgb(0x4a, 0x4a, 0x58);
pub const MUTED: Rgb = Rgb(0x8b, 0x8b, 0x99);
pub const FG: Rgb = Rgb(0xe6, 0xe6, 0xeb);
pub const BRAND: Rgb = Rgb(0x8a, 0xb4, 0xf8);
pub const SUCCESS: Rgb = Rgb(0x6c, 0xc0, 0x70);
pub const WARNING: Rgb = Rgb(0xf2, 0x8b, 0x82);
pub const ERROR: Rgb = Rgb(0xef, 0x53, 0x50);
pub const PURPLE: Rgb = Rgb(0xb3, 0x9d, 0xdb);

/// How a piece of text looks. No colour means the window's own.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Style {
    pub fg: Option<Rgb>,
    pub bg: Option<Rgb>,
    pub bold: bool,
}

impl Style {
    pub fn fg(colour: Rgb) -> Style {
        Style { fg: Some(colour), ..Style::default() }
    }

    pub fn bold(mut self) -> Style {
        self.bold = true;
        self
    }
}

/// Text in one style.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Span {
    pub text: String,
    pub style: Style,
}

/// One row of the window. `bg` colours the row up to the right edge of its area.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Line {
    pub spans: Vec<Span>,
    pub bg: Option<Rgb>,
}

impl Line {
    pub fn new() -> Line {
        Line::default()
    }

    pub fn styled(text: impl Into<String>, style: Style) -> Line {
        Line::new().with(text, style)
    }

    pub fn push(&mut self, text: impl Into<String>, style: Style) {
        let text = text.into();
        if !text.is_empty() {
            self.spans.push(Span { text, style });
        }
    }

    pub fn with(mut self, text: impl Into<String>, style: Style) -> Line {
        self.push(text, style);
        self
    }

    pub fn on(mut self, bg: Rgb) -> Line {
        self.bg = Some(bg);
        self
    }

    /// The characters without their styles.
    pub fn text(&self) -> String {
        self.spans.iter().map(|s| s.text.as_str()).collect()
    }

    /// The columns the row takes on the screen.
    pub fn width(&self) -> usize {
        self.spans.iter().map(|s| width(&s.text)).sum()
    }
}

/// The columns a character takes: two for Korean, Chinese and Japanese letters, none for marks
/// that sit on another letter.
pub fn char_width(c: char) -> usize {
    c.width().unwrap_or(0)
}

/// The columns a text takes on the screen.
pub fn width(text: &str) -> usize {
    text.chars().map(char_width).sum()
}

/// Text that is safe to draw: tabs become spaces, and colour codes and other control characters
/// that a command printed are left out. Line ends stay.
pub fn clean(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\x1b' => match chars.peek() {
                Some('[') => {
                    chars.next();
                    for d in chars.by_ref() {
                        if ('@'..='~').contains(&d) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    chars.next();
                    while let Some(d) = chars.next() {
                        if d == '\x07' {
                            break;
                        }
                        if d == '\x1b' {
                            chars.next_if_eq(&'\\');
                            break;
                        }
                    }
                }
                _ => {}
            },
            '\t' => out.push_str("    "),
            '\n' => out.push('\n'),
            c if c.is_control() => {}
            c => out.push(c),
        }
    }
    out
}

type Cell = (char, Style);

fn cells(line: &Line) -> Vec<Cell> {
    line.spans.iter().flat_map(|s| s.text.chars().map(move |c| (c, s.style))).collect()
}

fn build(cells: &[Cell], bg: Option<Rgb>) -> Line {
    let mut line = Line { spans: Vec::new(), bg };
    for &(c, style) in cells {
        match line.spans.last_mut() {
            Some(last) if last.style == style => last.text.push(c),
            _ => line.spans.push(Span { text: c.to_string(), style }),
        }
    }
    line
}

fn without_trailing_spaces(mut row: Vec<Cell>) -> Vec<Cell> {
    while row.last().is_some_and(|c| c.0 == ' ') {
        row.pop();
    }
    row
}

/// Break a row into rows of at most `width` columns. Rows after the first start with `indent`
/// spaces. With `words` the breaks fall after a space or next to a wide letter, and the space at
/// a break is dropped; without, the text is cut exactly where the row is full.
pub fn wrap(line: &Line, width: usize, indent: usize, words: bool) -> Vec<Line> {
    let width = width.max(2);
    let indent = if indent + 8 > width { 0 } else { indent };
    let mut rows: Vec<Vec<Cell>> = Vec::new();
    let mut row: Vec<Cell> = Vec::new();
    let mut used = 0;
    // Everything before this index may stay when the row has to be broken.
    let mut brk: Option<usize> = None;
    'cells: for (c, style) in cells(line) {
        let w = char_width(c);
        if words && c == ' ' && row.is_empty() && !rows.is_empty() {
            continue;
        }
        while used + w > width && !row.is_empty() {
            if words && c == ' ' {
                rows.push(without_trailing_spaces(std::mem::take(&mut row)));
                used = indent;
                brk = None;
                continue 'cells;
            }
            let rest = match brk.filter(|&at| words && w < 2 && at > 0) {
                Some(at) => row.split_off(at),
                None => Vec::new(),
            };
            let full = std::mem::replace(&mut row, rest);
            rows.push(if words { without_trailing_spaces(full) } else { full });
            used = indent + row.iter().map(|c| char_width(c.0)).sum::<usize>();
            brk = None;
        }
        row.push((c, style));
        used += w;
        if words && (c == ' ' || w == 2) {
            brk = Some(row.len());
        }
    }
    rows.push(row);
    rows.into_iter()
        .enumerate()
        .map(|(i, cells)| {
            let mut out = build(&cells, line.bg);
            if i > 0 && indent > 0 {
                out.spans.insert(0, Span { text: " ".repeat(indent), style: Style::default() });
            }
            out
        })
        .collect()
}

/// The start of a row that fits in `width` columns.
pub fn cut(line: &Line, width: usize) -> Line {
    let mut used = 0;
    let kept: Vec<Cell> = cells(line)
        .into_iter()
        .take_while(|c| {
            used += char_width(c.0);
            used <= width
        })
        .collect();
    build(&kept, line.bg)
}

/// The row, or its start and `…` when it is wider than `width` columns.
pub fn clip(line: &Line, width: usize) -> Line {
    if line.width() <= width {
        return line.clone();
    }
    let mut out = cut(line, width.saturating_sub(1));
    let style = out.spans.last().map(|s| s.style).unwrap_or_default();
    out.push("…", style);
    out
}

/// A text, or its start and `…` when it is wider than `width` columns.
pub fn clip_text(text: &str, width: usize) -> String {
    clip(&Line::styled(text, Style::default()), width).text()
}

fn heading(line: &str) -> Option<&str> {
    let rest = line.trim_start_matches('#');
    let marks = line.len() - rest.len();
    ((1..=6).contains(&marks) && rest.starts_with(' ')).then(|| rest.trim())
}

fn bullet(line: &str) -> Option<&str> {
    ["- ", "* ", "+ "].iter().find_map(|mark| line.strip_prefix(mark)).map(str::trim_start)
}

/// `3. text` or `3) text`: the number with its mark, and the text.
fn numbered(line: &str) -> Option<(&str, &str)> {
    let digits = line.chars().take_while(char::is_ascii_digit).count();
    let rest = &line[digits..];
    ((1..=3).contains(&digits) && (rest.starts_with(". ") || rest.starts_with(") "))).then(|| (&line[..digits + 1], rest[2..].trim_start()))
}

/// Add a paragraph's text to a row, with `**bold**` and `` `code` `` shown as such.
fn inline(line: &mut Line, text: &str, base: Style) {
    let mut plain = String::new();
    let mut rest = text;
    while let Some(c) = rest.chars().next() {
        if let Some(after) = rest.strip_prefix("**")
            && let Some(end) = after.find("**").filter(|&end| end > 0)
        {
            line.push(std::mem::take(&mut plain), base);
            line.push(&after[..end], base.bold());
            rest = &after[end + 2..];
            continue;
        }
        if let Some(after) = rest.strip_prefix('`')
            && let Some(end) = after.find('`').filter(|&end| end > 0)
        {
            line.push(std::mem::take(&mut plain), base);
            line.push(&after[..end], Style::fg(PURPLE));
            rest = &after[end + 1..];
            continue;
        }
        plain.push(c);
        rest = &rest[c.len_utf8()..];
    }
    line.push(plain, base);
}

/// The model's answer as rows: headings, lists, code blocks, bold and inline code. Everything
/// else is shown as it was written.
pub fn markdown(text: &str, width: usize) -> Vec<Line> {
    let base = Style::fg(FG);
    let mut out: Vec<Line> = Vec::new();
    let mut code = false;
    for raw in clean(text).lines() {
        let line = raw.trim_start();
        if line.starts_with("```") {
            code = !code;
            continue;
        }
        if code {
            out.extend(wrap(&Line::styled(format!(" {raw} "), base).on(PANEL), width, 1, false));
            continue;
        }
        if line.is_empty() {
            if out.last().is_some_and(|last| !last.spans.is_empty()) {
                out.push(Line::new());
            }
            continue;
        }
        let lead = raw.len() - line.len();
        if let Some(title) = heading(line) {
            let mut row = Line::new();
            inline(&mut row, title, Style::fg(BRAND).bold());
            out.extend(wrap(&row, width, 0, true));
        } else if let Some(item) = bullet(line) {
            let mut row = Line::styled(format!("{}• ", " ".repeat(lead)), Style::fg(MUTED));
            inline(&mut row, item, base);
            out.extend(wrap(&row, width, lead + 2, true));
        } else if let Some((number, item)) = numbered(line) {
            let mut row = Line::styled(format!("{}{number} ", " ".repeat(lead)), Style::fg(MUTED));
            inline(&mut row, item, base);
            out.extend(wrap(&row, width, lead + number.len() + 1, true));
        } else {
            let mut row = Line::new();
            inline(&mut row, raw.trim_end(), base);
            out.extend(wrap(&row, width, 0, true));
        }
    }
    while out.last().is_some_and(|last| last.spans.is_empty()) {
        out.pop();
    }
    out
}

/// One line of a comparison between two versions of a file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Row {
    Same(String),
    Del(String),
    Add(String),
}

/// The most cells of the comparison table that are worth their memory.
const DIFF_CELLS: usize = 1_000_000;

/// The lines that went, came and stayed between two versions of a file, in file order.
pub fn diff(old: &str, new: &str) -> Vec<Row> {
    let a: Vec<&str> = old.lines().collect();
    let b: Vec<&str> = new.lines().collect();
    let head = a.iter().zip(&b).take_while(|(x, y)| x == y).count();
    let tail = a[head..].iter().rev().zip(b[head..].iter().rev()).take_while(|(x, y)| x == y).count();
    let (am, bm) = (&a[head..a.len() - tail], &b[head..b.len() - tail]);

    let mut rows: Vec<Row> = a[..head].iter().map(|l| Row::Same(l.to_string())).collect();
    let (n, m) = (am.len(), bm.len());
    if n.saturating_mul(m) > DIFF_CELLS {
        rows.extend(am.iter().map(|l| Row::Del(l.to_string())));
        rows.extend(bm.iter().map(|l| Row::Add(l.to_string())));
    } else {
        // common[i][j]: the most lines `am[i..]` and `bm[j..]` share in order.
        let mut common = vec![0u32; (n + 1) * (m + 1)];
        let at = |i: usize, j: usize| i * (m + 1) + j;
        for i in (0..n).rev() {
            for j in (0..m).rev() {
                common[at(i, j)] =
                    if am[i] == bm[j] { common[at(i + 1, j + 1)] + 1 } else { common[at(i + 1, j)].max(common[at(i, j + 1)]) };
            }
        }
        let (mut i, mut j) = (0, 0);
        while i < n || j < m {
            if i < n && j < m && am[i] == bm[j] {
                rows.push(Row::Same(am[i].to_string()));
                i += 1;
                j += 1;
            } else if i < n && (j == m || common[at(i + 1, j)] >= common[at(i, j + 1)]) {
                rows.push(Row::Del(am[i].to_string()));
                i += 1;
            } else {
                rows.push(Row::Add(bm[j].to_string()));
                j += 1;
            }
        }
    }
    rows.extend(a[a.len() - tail..].iter().map(|l| Row::Same(l.to_string())));
    rows
}

/// How many lines came and how many went.
pub fn changes(rows: &[Row]) -> (usize, usize) {
    let added = rows.iter().filter(|r| matches!(r, Row::Add(_))).count();
    let removed = rows.iter().filter(|r| matches!(r, Row::Del(_))).count();
    (added, removed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn texts(lines: &[Line]) -> Vec<String> {
        lines.iter().map(Line::text).collect()
    }

    fn plain(text: &str) -> Line {
        Line::styled(text, Style::default())
    }

    #[test]
    fn korean_letters_take_two_columns() {
        assert_eq!(width("ab"), 2);
        assert_eq!(width("한글 ab"), 7);
        assert_eq!(width("e\u{301}"), 1);
        assert_eq!(clip_text("한글한글", 5), "한글…");
        assert_eq!(clip_text("short", 5), "short");
        assert_eq!(cut(&plain("한글한글"), 5).text(), "한글");
    }

    #[test]
    fn what_a_command_printed_is_made_safe_to_draw() {
        assert_eq!(clean("\x1b[31mred\x1b[0m\tx\r\n\x1b]0;title\x07done\x08"), "red    x\ndone");
    }

    #[test]
    fn prose_breaks_at_spaces_and_drops_the_space_at_the_break() {
        assert_eq!(texts(&wrap(&plain("the quick brown fox"), 10, 0, true)), ["the quick", "brown fox"]);
        assert_eq!(texts(&wrap(&plain("the quick  brown fox"), 9, 0, true)), ["the quick", "brown fox"]);
        // A word longer than the row is cut where the row is full.
        assert_eq!(texts(&wrap(&plain("abcdefghijkl mn"), 5, 0, true)), ["abcde", "fghij", "kl mn"]);
        assert_eq!(texts(&wrap(&plain(""), 10, 0, true)), [""]);
        // Rows after the first hang under the text of the first.
        assert_eq!(texts(&wrap(&plain("• one two three four five"), 14, 2, true)), ["• one two", "  three four", "  five"]);
    }

    #[test]
    fn korean_text_breaks_between_letters() {
        assert_eq!(texts(&wrap(&plain("한글한글한글"), 5, 0, true)), ["한글", "한글", "한글"]);
        assert_eq!(texts(&wrap(&plain("fix 버그를 고쳤습니다"), 10, 0, true)), ["fix 버그를", "고쳤습니다"]);
    }

    #[test]
    fn code_is_cut_exactly_and_keeps_its_spaces_and_styles() {
        assert_eq!(texts(&wrap(&plain("ab  cdefgh"), 4, 0, false)), ["ab  ", "cdef", "gh"]);
        let row = Line::styled("abc", Style::fg(ERROR)).with("def", Style::fg(SUCCESS)).on(PANEL);
        let rows = wrap(&row, 4, 0, false);
        assert_eq!(
            rows[0].spans,
            [Span { text: "abc".into(), style: Style::fg(ERROR) }, Span { text: "d".into(), style: Style::fg(SUCCESS) }]
        );
        assert_eq!(rows[1].spans, [Span { text: "ef".into(), style: Style::fg(SUCCESS) }]);
        assert!(rows.iter().all(|r| r.bg == Some(PANEL)));
    }

    #[test]
    fn an_answer_shows_headings_lists_code_and_bold() {
        let rows = markdown("# Plan\n\n\n- fix **a.py**\n- run `pytest`\n1. first\n```py\nx = 1\n```\ndone\n\n", 40);
        assert_eq!(texts(&rows), ["Plan", "", "• fix a.py", "• run pytest", "1. first", " x = 1 ", "done"]);
        assert!(rows[0].spans[0].style.bold);
        assert!(rows[2].spans.iter().any(|s| s.text == "a.py" && s.style.bold));
        assert!(rows[3].spans.iter().any(|s| s.text == "pytest" && s.style.fg == Some(PURPLE)));
        assert_eq!(rows[5].bg, Some(PANEL));
        // Marks without a partner are plain text.
        assert_eq!(texts(&markdown("2 ** 3 and a ` alone", 40)), ["2 ** 3 and a ` alone"]);
    }

    #[test]
    fn a_comparison_names_the_lines_that_went_and_came() {
        let rows = diff("a\nb\nc\nd\n", "a\nB\nc\nd\ne\n");
        assert_eq!(
            rows,
            [
                Row::Same("a".into()),
                Row::Del("b".into()),
                Row::Add("B".into()),
                Row::Same("c".into()),
                Row::Same("d".into()),
                Row::Add("e".into())
            ]
        );
        assert_eq!(changes(&rows), (2, 1));
        assert_eq!(changes(&diff("", "x\ny\n")), (2, 0));
        assert_eq!(changes(&diff("x\ny\n", "x\ny\n")), (0, 0));
        assert_eq!(diff("x\n", ""), [Row::Del("x".into())]);
    }
}
