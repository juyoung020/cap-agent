//! What the conversation shows: the person's messages, the model's words, and one entry for
//! every tool call with what came of it.

use super::text::{BRAND, ERROR, FG, Line, MUTED, Row, SUCCESS, SURFACE, Style, WARNING, changes, clean, clip, diff, markdown, wrap};
use crate::headline;
use crate::term::first_line;
use cap_core::agent::{Event, Report, Status};
use cap_core::gate::Verdict;
use cap_core::tools::{self, Status as ToolStatus};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

/// A file larger than this is changed without a comparison on the screen.
const COMPARED_BYTES: u64 = 400_000;
/// Rows of an entry while it is folded.
const FOLDED_CHANGES: usize = 8;
const FOLDED_OUTPUT: usize = 3;
/// Rows of an entry when it is opened with Ctrl+O.
const OPEN_CHANGES: usize = 400;
const OPEN_OUTPUT: usize = 60;
/// Unchanged lines shown around a change.
const AROUND: usize = 2;
/// The most characters of a call's subject in its head line.
const SUBJECT: usize = 300;

#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    Running,
    Ok,
    Failed,
    Warn,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Read,
    List,
    Search,
    Change,
    Run,
    Test,
    /// A call whose result has nothing to add to its head line.
    Quiet,
    Other,
}

enum Body {
    None,
    /// Lines a tool printed. `end` when the last lines say the most.
    Output {
        lines: Vec<String>,
        end: bool,
    },
    Diff(Vec<Row>),
}

/// One tool call and what came of it.
struct Tool {
    verb: String,
    target: String,
    kind: Kind,
    state: State,
    /// The short result next to the head.
    extra: String,
    body: Body,
    /// For a change: the file.
    path: PathBuf,
    /// For a change: what the file held before, when that can be compared.
    before: Option<String>,
}

impl Tool {
    fn new(verb: &str, target: String, kind: Kind) -> Tool {
        Tool {
            verb: verb.to_string(),
            target,
            kind,
            state: State::Running,
            extra: String::new(),
            body: Body::None,
            path: PathBuf::new(),
            before: None,
        }
    }
}

enum Cell {
    /// Rows made by the window itself: the welcome, a command's answer, how a turn ended.
    Lines(Vec<Line>),
    User(String),
    /// What the model wrote next to its tool calls.
    Text(String),
    Answer(String),
    Tool(Tool),
}

/// A file's text as it is drawn, or nothing when it is too large or not text.
fn snapshot(path: &Path) -> Option<String> {
    match std::fs::metadata(path) {
        Err(_) => Some(String::new()),
        Ok(meta) if meta.len() > COMPARED_BYTES => None,
        Ok(_) => std::fs::read_to_string(path).ok().map(|text| clean(&text)),
    }
}

/// `$ command -> exit 3` and the output after it: the exit code and the output.
fn exit_of(text: &str) -> Option<(&str, &str)> {
    let rest = &text[text.find(" -> exit ")? + " -> exit ".len()..];
    let (code, output) = rest.split_once('\n').unwrap_or((rest, ""));
    let code = code.trim();
    (!code.is_empty() && code.chars().all(|c| c.is_ascii_digit() || c == '-')).then_some((code, output))
}

fn filled_lines(text: &str) -> Vec<String> {
    text.lines().map(str::trim_end).filter(|line| !line.trim().is_empty()).map(str::to_string).collect()
}

pub(super) fn plural(n: usize, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}

/// The conversation as it stands.
pub struct View {
    cells: Vec<Cell>,
    root: PathBuf,
    test_cmd: String,
    /// The model's text while it arrives.
    live: String,
    /// The entry of the call that is being carried out.
    running: Option<usize>,
    /// Model calls in the turns before this one, and in all.
    calls_before: u32,
    pub calls: u32,
    /// The tokens the conversation takes.
    pub tokens: u32,
    /// The answer of this turn that is on the screen.
    answer: String,
    open: bool,
    version: u64,
    drawn: Option<(u64, usize, bool)>,
    rows: Vec<Line>,
}

impl View {
    pub fn new(root: &Path, test_cmd: &str) -> View {
        View {
            cells: Vec::new(),
            root: root.to_path_buf(),
            test_cmd: test_cmd.to_string(),
            live: String::new(),
            running: None,
            calls_before: 0,
            calls: 0,
            tokens: 0,
            answer: String::new(),
            open: false,
            version: 0,
            drawn: None,
            rows: Vec::new(),
        }
    }

    fn push(&mut self, cell: Cell) {
        self.cells.push(cell);
        self.version += 1;
    }

    /// Rows from the window itself.
    pub fn note(&mut self, lines: Vec<Line>) {
        self.push(Cell::Lines(lines));
    }

    /// Plain text from the window itself, such as a command's answer.
    pub fn say(&mut self, text: &str, colour: super::text::Rgb) {
        self.note(clean(text).lines().map(|line| Line::styled(line, Style::fg(colour))).collect());
    }

    pub fn user(&mut self, text: &str) {
        self.push(Cell::User(text.to_string()));
    }

    /// Fold or open the entries.
    pub fn toggle(&mut self) {
        self.open = !self.open;
    }

    /// Forget the conversation.
    pub fn reset(&mut self) {
        self.cells.clear();
        self.live.clear();
        self.running = None;
        self.calls_before = 0;
        self.calls = 0;
        self.tokens = 0;
        self.version += 1;
    }

    pub fn begin_turn(&mut self) {
        self.answer.clear();
        self.live.clear();
        self.running = None;
    }

    pub fn event(&mut self, event: &Event<'_>) {
        match event {
            Event::Step(n) => {
                self.calls = self.calls_before + n;
                self.live.clear();
            }
            Event::Context(tokens) => self.tokens = *tokens,
            Event::Delta(text) => self.live.push_str(text),
            Event::Text(text) => {
                self.live.clear();
                if !text.trim().is_empty() {
                    self.push(Cell::Text(text.to_string()));
                }
            }
            Event::Answer(text) => {
                self.live.clear();
                self.answer = text.to_string();
                if !text.trim().is_empty() {
                    self.push(Cell::Answer(text.to_string()));
                }
            }
            Event::Call { name, args } => self.call(name, args),
            Event::Result { status, text, .. } => self.result(*status, text),
            Event::Gate(verdict) => self.gate(verdict),
            Event::Nudge(text) => self.say(&format!("! {}", text.trim()), WARNING),
            Event::Compacted { stubbed, dropped } => self.say(
                &format!(
                    "~ context trimmed ({} shortened, {} dropped)",
                    plural(*stubbed, "tool output", "tool outputs"),
                    plural(*dropped, "step", "steps")
                ),
                MUTED,
            ),
        }
    }

    fn call(&mut self, name: &str, args: &str) {
        self.live.clear();
        self.running = None;
        let known = tools::canonical_name(name);
        if known == Some("done") {
            return;
        }
        let mut map: Map<String, Value> = serde_json::from_str(args).unwrap_or_default();
        if let Some(tool) = known {
            tools::normalize_args(tool, &mut map);
        }
        let arg = |key: &str| map.get(key).and_then(Value::as_str).unwrap_or("").to_string();
        let mut tool = match known {
            Some("read_file") => Tool::new("Read", arg("path"), Kind::Read),
            Some("list_files") => {
                Tool::new("List", Some(arg("path")).filter(|p| !p.is_empty()).unwrap_or_else(|| ".".to_string()), Kind::List)
            }
            Some("search") => Tool::new("Search", arg("pattern"), Kind::Search),
            Some("replace_lines" | "edit_file") => Tool::new("Edit", arg("path"), Kind::Change),
            Some("write_file") => Tool::new("Write", arg("path"), Kind::Change),
            Some("run") => Tool::new("Run", first_line(&arg("command"), SUBJECT), Kind::Run),
            Some("run_tests") => Tool::new("Test", self.test_cmd.clone(), Kind::Test),
            Some("note") => Tool::new("Note", first_line(&arg("text"), SUBJECT), Kind::Quiet),
            Some("use_skill") => Tool::new("Skill", arg("name"), Kind::Quiet),
            _ => match name.strip_prefix("mcp__").and_then(|rest| rest.split_once("__")) {
                Some((server, tool)) => Tool::new("Tool", format!("{server} · {tool}"), Kind::Other),
                None => Tool::new(name, first_line(args, SUBJECT), Kind::Other),
            },
        };
        if tool.kind == Kind::Change && !tool.target.is_empty() {
            tool.path = self.root.join(&tool.target);
            tool.before = snapshot(&tool.path);
        }
        self.running = Some(self.cells.len());
        self.push(Cell::Tool(tool));
    }

    fn result(&mut self, status: ToolStatus, text: &str) {
        let Some(Cell::Tool(tool)) = self.running.take().and_then(|at| self.cells.get_mut(at)) else {
            return;
        };
        // A test run is filled in by its verdict.
        if tool.state != State::Running {
            return;
        }
        let text = clean(text);
        let first = first_line(&text, SUBJECT);
        let exit = if tool.kind == Kind::Run { exit_of(&text) } else { None };
        if matches!(status, ToolStatus::Denied) {
            tool.state = State::Warn;
            tool.extra = first;
        } else if let Some((code, output)) = exit {
            tool.state = if code == "0" { State::Ok } else { State::Failed };
            tool.extra = format!("→ exit {code}");
            tool.body = Body::Output { lines: filled_lines(output), end: true };
        } else if matches!(status, ToolStatus::Failed) {
            tool.state = State::Failed;
            tool.extra = first;
        } else {
            tool.state = State::Ok;
            match tool.kind {
                Kind::Read => {
                    if let Some(at) = first.find(" lines ") {
                        tool.extra = format!("→{}", &first[at..]);
                    } else if first.ends_with(" is empty") {
                        tool.extra = "→ empty".to_string();
                    }
                }
                Kind::List => tool.extra = first.split_once(": ").map(|(_, found)| format!("→ {found}")).unwrap_or_default(),
                Kind::Search => {
                    tool.extra = first.rsplit_once("`: ").map(|(_, found)| format!("→ {found}")).unwrap_or_default();
                    tool.body = Body::Output { lines: filled_lines(&text).into_iter().skip(1).collect(), end: false };
                }
                Kind::Change => match (&tool.before, snapshot(&tool.path)) {
                    (Some(before), Some(after)) => {
                        let rows = diff(before, &after);
                        if changes(&rows) == (0, 0) {
                            tool.extra = "→ unchanged".to_string();
                        } else {
                            tool.body = Body::Diff(rows);
                        }
                    }
                    _ => tool.extra = format!("→ {}", first.rsplit_once(": ").map_or(first.as_str(), |(_, what)| what)),
                },
                Kind::Test => tool.extra = format!("→ {}", first.strip_prefix("run_tests: ").unwrap_or(&first)),
                Kind::Run | Kind::Other => tool.body = Body::Output { lines: filled_lines(&text), end: false },
                Kind::Quiet => {}
            }
        }
        self.version += 1;
    }

    fn gate(&mut self, verdict: &Verdict) {
        let (state, extra, lines) = match verdict {
            Verdict::Pass => (State::Ok, "→ tests pass".to_string(), Vec::new()),
            Verdict::NoTests => (State::Warn, "→ no tests were found".to_string(), Vec::new()),
            Verdict::Fail { tail, .. } => {
                let lines = filled_lines(&clean(tail));
                let last = lines.last().map_or("", |line| line.trim_matches(|c| c == '=' || c == ' '));
                (State::Failed, format!("→ {}", if last.is_empty() { "tests fail" } else { last }), lines)
            }
        };
        let asked = self.running.and_then(|at| match self.cells.get_mut(at) {
            Some(Cell::Tool(tool)) if tool.kind == Kind::Test && tool.state == State::Running => Some(tool),
            _ => None,
        });
        let fill = |tool: &mut Tool| {
            tool.state = state;
            tool.extra = extra;
            tool.body = if lines.is_empty() { Body::None } else { Body::Output { lines, end: true } };
        };
        match asked {
            Some(tool) => fill(tool),
            // Tests the harness started by itself are worth a row only when they say something.
            None if matches!(verdict, Verdict::NoTests) => return,
            None => {
                let mut tool = Tool::new("Test", self.test_cmd.clone(), Kind::Test);
                fill(&mut tool);
                self.cells.push(Cell::Tool(tool));
            }
        }
        self.version += 1;
    }

    /// Close the turn: the answer if it is not on the screen yet, how the turn ended, and what it took.
    pub fn end_turn(&mut self, report: &Report) {
        self.live.clear();
        if let Some(Cell::Tool(tool)) = self.running.take().and_then(|at| self.cells.get_mut(at))
            && tool.state == State::Running
        {
            tool.state = State::Warn;
            tool.extra = "stopped".to_string();
        }
        let text = report.text.trim();
        if !text.is_empty() && text != self.answer.trim() {
            self.push(Cell::Answer(text.to_string()));
        }
        let mut lines = Vec::new();
        if report.status != Status::Answered {
            if let (Status::TestsFail, Some(tail)) = (&report.status, &report.failure) {
                let tail = filled_lines(&clean(tail));
                lines.extend(tail[tail.len().saturating_sub(12)..].iter().map(|line| Line::styled(line.as_str(), Style::fg(MUTED))));
            }
            let (mark, colour) = match report.status {
                Status::Verified => ("✓", SUCCESS),
                Status::Unverified | Status::Interrupted => ("!", WARNING),
                _ => ("✗", ERROR),
            };
            lines.push(Line::styled(format!("{mark} {}", headline(&report.status)), Style::fg(colour).bold()));
        }
        let mut foot = format!("── {} · {:.0}s", plural(report.steps as usize, "call", "calls"), report.seconds.floor());
        if !report.changed.is_empty() {
            foot.push_str(&format!(" · changed {}", report.changed.join(", ")));
        }
        lines.push(Line::styled(foot, Style::fg(MUTED)));
        self.note(lines);
        self.calls = self.calls_before + report.steps;
        self.calls_before = self.calls;
    }

    /// The rows that show in a window `height` rows high when the person has moved `scroll` rows
    /// up from the end. Also: how many rows there are, and the scroll that was possible.
    pub fn window(&mut self, width: usize, height: usize, scroll: usize) -> (Vec<Line>, usize, usize) {
        let key = (self.version, width, self.open);
        if self.drawn != Some(key) {
            self.rows = self.draw(width);
            self.drawn = Some(key);
        }
        let mut live = Vec::new();
        let arriving = clean(&self.live);
        if !arriving.trim().is_empty() {
            if !self.rows.is_empty() {
                live.push(Line::new());
            }
            live.extend(arriving.trim().lines().flat_map(|line| wrap(&Line::styled(line, Style::fg(MUTED)), width, 0, true)));
        }
        let total = self.rows.len() + live.len();
        let scroll = scroll.min(total.saturating_sub(height));
        let start = total.saturating_sub(height + scroll);
        let shown = self.rows.iter().chain(&live).skip(start).take(height).cloned().collect();
        (shown, total, scroll)
    }

    fn draw(&self, width: usize) -> Vec<Line> {
        let mut out: Vec<Line> = Vec::new();
        for cell in &self.cells {
            if !out.is_empty() {
                out.push(Line::new());
            }
            match cell {
                Cell::Lines(lines) => out.extend(lines.iter().flat_map(|line| wrap(line, width, 0, true))),
                Cell::User(text) => {
                    for (i, line) in clean(text).lines().enumerate() {
                        let mark = if i == 0 { "› " } else { "  " };
                        let row = Line::styled(mark, Style::fg(BRAND).bold()).with(line, Style::fg(FG)).on(SURFACE);
                        out.extend(wrap(&row, width, 2, true));
                    }
                }
                Cell::Text(text) => {
                    out.extend(clean(text).trim().lines().flat_map(|line| wrap(&Line::styled(line, Style::fg(MUTED)), width, 0, true)));
                }
                Cell::Answer(text) => out.extend(markdown(text, width)),
                Cell::Tool(tool) => self.draw_tool(tool, width, &mut out),
            }
        }
        out
    }

    fn draw_tool(&self, tool: &Tool, width: usize, out: &mut Vec<Line>) {
        let (dot, result) = match tool.state {
            State::Running => (MUTED, MUTED),
            State::Ok => (SUCCESS, MUTED),
            State::Failed => (ERROR, ERROR),
            State::Warn => (WARNING, WARNING),
        };
        let mut head = Line::styled("● ", Style::fg(dot)).with(tool.verb.as_str(), Style::fg(FG).bold());
        if !tool.target.is_empty() {
            head.push(format!("  {}", tool.target), Style::fg(FG));
        }
        if let Body::Diff(rows) = &tool.body {
            let (added, removed) = changes(rows);
            head.push(format!("  +{added}"), Style::fg(SUCCESS));
            head.push(format!(" −{removed}"), Style::fg(ERROR));
        } else if !tool.extra.is_empty() {
            head.push(format!("  {}", tool.extra), Style::fg(result));
        }
        out.extend(wrap(&head, width, 2, true));

        let muted = |text: String| Line::styled(text, Style::fg(MUTED));
        let (rows, more): (Vec<Line>, Option<String>) = match &tool.body {
            Body::None => return,
            Body::Output { lines, end } => {
                let limit = if self.open { OPEN_OUTPUT } else { FOLDED_OUTPUT };
                let hidden = lines.len().saturating_sub(limit);
                let shown = if *end { &lines[hidden..] } else { &lines[..lines.len() - hidden] };
                let more = (hidden > 0).then(|| {
                    let lines = plural(hidden, "line", "lines");
                    if self.open { format!("… +{lines}") } else { format!("… +{lines} (ctrl+o to expand)") }
                });
                (shown.iter().map(|line| muted(line.clone())).collect(), more)
            }
            Body::Diff(rows) => {
                let painted = |row: &Row| match row {
                    Row::Same(text) => muted(format!("  {text}")),
                    Row::Del(text) => Line::styled(format!("− {text}"), Style::fg(ERROR)),
                    Row::Add(text) => Line::styled(format!("+ {text}"), Style::fg(SUCCESS)),
                };
                if self.open {
                    let changed: Vec<usize> =
                        rows.iter().enumerate().filter(|(_, row)| !matches!(row, Row::Same(_))).map(|(i, _)| i).collect();
                    let near = |i: usize| changed.iter().any(|&c| c.abs_diff(i) <= AROUND);
                    let mut shown = Vec::new();
                    let mut last = None;
                    for (i, row) in rows.iter().enumerate().filter(|(i, _)| near(*i)) {
                        if last.is_some_and(|last| last + 1 < i) {
                            shown.push(muted("⋯".to_string()));
                        }
                        shown.push(painted(row));
                        last = Some(i);
                    }
                    let hidden = shown.len().saturating_sub(OPEN_CHANGES);
                    shown.truncate(OPEN_CHANGES);
                    (shown, (hidden > 0).then(|| format!("… +{}", plural(hidden, "line", "lines"))))
                } else {
                    let shown: Vec<Line> =
                        rows.iter().filter(|row| !matches!(row, Row::Same(_))).take(FOLDED_CHANGES).map(painted).collect();
                    let more = (rows.len() > shown.len()).then(|| "… ctrl+o to expand".to_string());
                    (shown, more)
                }
            }
        };
        let inner = width.saturating_sub(5).max(1);
        for (i, row) in rows.iter().chain(more.map(muted).iter()).enumerate() {
            let lead = if i == 0 { "  ⎿  " } else { "     " };
            let parts = if self.open { wrap(row, inner, 0, false) } else { vec![clip(row, inner)] };
            for (j, part) in parts.into_iter().enumerate() {
                let mut line = Line::styled(if j == 0 { lead } else { "     " }, Style::fg(MUTED));
                line.spans.extend(part.spans);
                out.push(line);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use cap_core::tools::Status as S;

    fn view() -> View {
        View::new(Path::new("."), "cargo test")
    }

    fn texts(view: &mut View, width: usize) -> Vec<String> {
        view.window(width, 1000, 0).0.iter().map(Line::text).collect()
    }

    fn call(view: &mut View, name: &str, args: &str, status: S, text: &str) {
        view.event(&Event::Call { name, args });
        view.event(&Event::Result { name, status, text });
    }

    fn report(status: Status, text: &str) -> Report {
        Report {
            status,
            text: text.to_string(),
            steps: 3,
            prompt_tokens: 0,
            completion_tokens: 0,
            seconds: 12.4,
            changed: vec!["calc.py".to_string()],
            rejections: 0,
            failure: None,
            transcript: Vec::new(),
        }
    }

    #[test]
    fn a_call_shows_what_it_did_and_what_came_of_it() {
        let mut v = view();
        v.user("fix the bug");
        v.event(&Event::Step(1));
        v.event(&Event::Call { name: "read_file", args: r#"{"file":"calc.py"}"# });
        assert_eq!(texts(&mut v, 60), ["› fix the bug", "", "● Read  calc.py"]);
        v.event(&Event::Result { name: "read_file", status: S::Ok, text: "calc.py lines 1-12 of 12\n1| def add" });
        call(&mut v, "ls", "{}", S::Ok, ".: 2 files\ncalc.py\ntest_calc.py");
        call(
            &mut v,
            "grep",
            r#"{"query":"def "}"#,
            S::Ok,
            "search `def `: 5 matches\na.py:1: def a\na.py:2: def b\na.py:3: def c\na.py:4: def d\na.py:5: def e",
        );
        call(
            &mut v,
            "run",
            r#"{"command":"python x.py"}"#,
            S::Failed,
            "$ python x.py -> exit 1\nTraceback\n\n  File x\nNameError: y\nlast",
        );
        call(&mut v, "run", r#"{"command":"rm -rf /"}"#, S::Denied, "The person said no to this command.");
        call(&mut v, "mcp__docs__lookup", "{}", S::Ok, "found it");
        call(&mut v, "note", r#"{"text":"the bug is in div"}"#, S::Ok, "noted");
        call(&mut v, "done", r#"{"summary":"fixed"}"#, S::Ok, "ok");
        assert_eq!(
            texts(&mut v, 60)[2..],
            [
                "● Read  calc.py  → lines 1-12 of 12",
                "",
                "● List  .  → 2 files",
                "",
                "● Search  def   → 5 matches",
                "  ⎿  a.py:1: def a",
                "     a.py:2: def b",
                "     a.py:3: def c",
                "     … +2 lines (ctrl+o to expand)",
                "",
                "● Run  python x.py  → exit 1",
                "  ⎿    File x",
                "     NameError: y",
                "     last",
                "     … +1 line (ctrl+o to expand)",
                "",
                "● Run  rm -rf /  The person said no to this command.",
                "",
                "● Tool  docs · lookup",
                "  ⎿  found it",
                "",
                "● Note  the bug is in div",
            ]
        );
        v.toggle();
        let open = texts(&mut v, 60);
        assert!(open.contains(&"     a.py:5: def e".to_string()) && open.contains(&"  ⎿  Traceback".to_string()));
        assert_eq!(v.calls, 1);
    }

    #[test]
    fn an_edit_shows_the_lines_that_went_and_came() {
        let dir = std::env::temp_dir().join(format!("cap-tui-edit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("calc.py"), "def div(a, b):\n    return a // b\n\n\ndef add(a, b):\n    return a + b\n").unwrap();
        let mut v = View::new(&dir, "pytest -q");
        v.event(&Event::Call { name: "replace_lines", args: r#"{"path":"calc.py","start":2,"end":2,"new":"return a / b"}"# });
        std::fs::write(dir.join("calc.py"), "def div(a, b):\n    return a / b\n\n\ndef add(a, b):\n    return a + b\n").unwrap();
        v.event(&Event::Result { name: "replace_lines", status: S::Ok, text: "lines 2-2 replaced (1 -> 1 lines)" });
        call(&mut v, "write_file", r#"{"path":"new.txt","content":"x"}"#, S::Failed, "new.txt: the folder is read only");
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(
            texts(&mut v, 60),
            [
                "● Edit  calc.py  +1 −1",
                "  ⎿  −     return a // b",
                "     +     return a / b",
                "     … ctrl+o to expand",
                "",
                "● Write  new.txt  new.txt: the folder is read only",
            ]
        );
        v.toggle();
        assert_eq!(
            texts(&mut v, 60)[..7],
            [
                "● Edit  calc.py  +1 −1",
                "  ⎿    def div(a, b):",
                "     −     return a // b",
                "     +     return a / b",
                "       ",
                "       ",
                ""
            ]
        );
    }

    #[test]
    fn a_test_run_shows_its_verdict_once() {
        let mut v = view();
        v.event(&Event::Call { name: "run_tests", args: "{}" });
        v.event(&Event::Gate(&Verdict::Fail { tail: "E assert 1 == 2\n==== 2 failed, 3 passed in 0.1s ====\n".to_string(), signature: 1 }));
        v.event(&Event::Result { name: "run_tests", status: S::Ok, text: "run_tests: tests fail\n…" });
        // Tests the harness ran by itself after an edit.
        v.event(&Event::Gate(&Verdict::Pass));
        v.event(&Event::Gate(&Verdict::NoTests));
        assert_eq!(
            texts(&mut v, 60),
            [
                "● Test  cargo test  → 2 failed, 3 passed in 0.1s",
                "  ⎿  E assert 1 == 2",
                "     ==== 2 failed, 3 passed in 0.1s ====",
                "",
                "● Test  cargo test  → tests pass",
            ]
        );
        call(&mut v, "run_tests", "{}", S::Denied, "The mode is read only.");
        assert_eq!(texts(&mut v, 60).last().unwrap(), "● Test  cargo test  The mode is read only.");
    }

    #[test]
    fn the_models_words_show_while_they_arrive_and_once_when_complete() {
        let mut v = view();
        v.begin_turn();
        v.event(&Event::Delta("Let me "));
        v.event(&Event::Delta("look."));
        assert_eq!(texts(&mut v, 60), ["Let me look."]);
        v.event(&Event::Text("Let me look."));
        v.event(&Event::Delta("The answer"));
        v.event(&Event::Answer("The answer is **4**."));
        assert_eq!(texts(&mut v, 60), ["Let me look.", "", "The answer is 4."]);
        v.end_turn(&Report { changed: Vec::new(), ..report(Status::Answered, "The answer is **4**.") });
        assert_eq!(texts(&mut v, 60)[3..], ["", "── 3 calls · 12s"]);
        assert_eq!(v.calls, 3);
    }

    #[test]
    fn the_end_of_a_turn_says_how_it_went() {
        let mut v = view();
        v.begin_turn();
        v.event(&Event::Call { name: "run", args: r#"{"command":"sleep 99"}"# });
        v.end_turn(&report(Status::Interrupted, ""));
        assert_eq!(
            texts(&mut v, 80),
            ["● Run  sleep 99  stopped", "", "! STOPPED — interrupted before the work was done", "── 3 calls · 12s · changed calc.py"]
        );
        v.begin_turn();
        v.event(&Event::Step(2));
        assert_eq!(v.calls, 5);
        v.end_turn(&Report { failure: Some("E assert\n1 failed\n".to_string()), ..report(Status::TestsFail, "I tried.") });
        assert_eq!(
            texts(&mut v, 80)[5..],
            ["I tried.", "", "E assert", "1 failed", "✗ NOT DONE — the tests still fail", "── 3 calls · 12s · changed calc.py"]
        );
        v.reset();
        assert!(texts(&mut v, 80).is_empty() && v.calls == 0);
    }

    #[test]
    fn the_window_shows_the_end_and_scrolls_back() {
        let mut v = view();
        v.say("1\n2\n3\n4\n5", FG);
        assert_eq!(v.window(10, 2, 0).0.iter().map(Line::text).collect::<Vec<_>>(), ["4", "5"]);
        let (rows, total, scroll) = v.window(10, 2, 99);
        assert_eq!((rows.iter().map(Line::text).collect::<Vec<_>>(), total, scroll), (vec!["1".to_string(), "2".to_string()], 5, 3));
        assert_eq!(v.window(10, 9, 4).0.len(), 5);
        // Long rows wrap to the width of the window.
        v.user("one two three four");
        assert_eq!(v.window(12, 3, 0).0.iter().map(Line::text).collect::<Vec<_>>(), ["", "› one two", "  three four"]);
    }
}
