//! The tools a model can call. Every call passes the permission table before it touches anything.
//!
//! Results start with a one-line header (`path lines a-b of N`, `$ cmd -> exit 0`), which is
//! what remains when an old result is dropped from the context.

use crate::edit::{self, Eol, LineError, Matched, lines_of};
use crate::exec;
use crate::model::ToolSpec;
use crate::perm::{self, Approver, Decision, Mode};
use crate::sandbox::{self, Sandbox};
use crate::view::{Change, Views};
use crate::workspace::Workspace;
use serde_json::{Map, Value, json};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::Duration;

pub const READ_LINES: usize = 250;
pub const READ_CHARS: usize = 12_000;
pub const LIST_LIMIT: usize = 200;
pub const SEARCH_LIMIT: usize = 40;
const SEARCH_FILE_BYTES: u64 = 1_000_000;
/// New lines an edit result prints in full; longer ones show their first and last lines.
const ECHO_LINES: usize = 12;

#[cfg(windows)]
const ISOLATION_NOTE: &str = "[commands run isolated: they change files only inside the project folder and have no network]";
#[cfg(not(windows))]
const ISOLATION_NOTE: &str =
    "[commands run isolated: they change files only inside the project folder, the temp folders and the build caches]";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Ok,
    /// The call was well formed and did not work (file missing, `old` not found, exit code 1).
    Failed,
    /// The permission table or the person said no.
    Denied,
}

#[derive(Clone, Debug)]
pub struct ToolResult {
    pub status: Status,
    pub text: String,
    /// Path from the root of a file this call created or changed.
    pub changed: Option<String>,
}

impl ToolResult {
    pub fn ok(text: impl Into<String>) -> Self {
        Self { status: Status::Ok, text: text.into(), changed: None }
    }
    pub fn failed(text: impl Into<String>) -> Self {
        Self { status: Status::Failed, text: text.into(), changed: None }
    }
    pub fn denied(text: impl Into<String>) -> Self {
        Self { status: Status::Denied, text: text.into(), changed: None }
    }
}

pub struct Ctx<'a> {
    pub ws: &'a Workspace,
    pub mode: Mode,
    pub approver: &'a dyn Approver,
    pub command_timeout: Duration,
    /// The lines the model has in front of it; `replace_lines` works only on those.
    pub views: &'a Mutex<Views>,
    pub sandbox: &'a Sandbox,
}

fn views<'a>(ctx: &Ctx<'a>) -> MutexGuard<'a, Views> {
    ctx.views.lock().unwrap_or_else(PoisonError::into_inner)
}

/// How many tools a model is offered. A small model picks better from a short list.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile {
    /// Windows up to 16k tokens: one way to do each thing, short descriptions.
    Small,
    /// Up to 64k tokens: both edit tools and the file listing.
    Standard,
    /// Larger windows: everything, with notes.
    Full,
}

impl Profile {
    pub fn for_window(tokens: u32) -> Profile {
        match tokens {
            0..=16_384 => Profile::Small,
            16_385..=64_000 => Profile::Standard,
            _ => Profile::Full,
        }
    }

    pub fn parse(text: &str) -> Option<Profile> {
        match text.trim().to_ascii_lowercase().as_str() {
            "small" => Some(Profile::Small),
            "standard" | "medium" => Some(Profile::Standard),
            "full" | "large" => Some(Profile::Full),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Profile::Small => "small",
            Profile::Standard => "standard",
            Profile::Full => "full",
        }
    }
}

/// What decides the tool list of a session.
#[derive(Clone, Copy, Debug)]
pub struct Offer {
    pub mode: Mode,
    pub has_tests: bool,
    pub profile: Profile,
    /// The file list in the first message was cut, so a small model needs `list_files` too.
    pub listing_cut: bool,
    /// The model has to end with a `done` call.
    pub done: bool,
    /// Skills are installed.
    pub skills: bool,
}

impl Offer {
    pub fn new(mode: Mode, has_tests: bool, profile: Profile) -> Offer {
        Offer { mode, has_tests, profile, listing_cut: false, done: false, skills: false }
    }
}

/// The tools offered to the model, always in the same order. A tool that is not offered still
/// runs when a model calls it by name.
pub fn specs(offer: &Offer) -> Vec<ToolSpec> {
    let obj = |props: Value, required: &[&str]| json!({"type": "object", "properties": props, "required": required});
    let small = offer.profile == Profile::Small;
    let pick = |long: &'static str, short: &'static str| if small { short } else { long };
    let mut out = vec![ToolSpec::new(
        "read_file",
        pick(
            "Read a text file. Lines come back numbered as `12| text`. Long files come in parts: pass start to continue.",
            "Read a file. Lines come back numbered as `12| text`.",
        ),
        obj(
            json!({"path": {"type": "string"}, "start": {"type": "integer", "description": "first line, from 1"}, "limit": {"type": "integer", "description": "number of lines"}}),
            &["path"],
        ),
    )];
    if offer.mode == Mode::Read || !small || offer.listing_cut {
        out.push(ToolSpec::new(
            "list_files",
            "List the files of the project or of one folder.",
            obj(json!({"path": {"type": "string", "description": "folder; leave out for the whole project"}}), &[]),
        ));
    }
    out.push(ToolSpec::new(
        "search",
        pick("Find lines that match a regular expression. Returns `path:line: text`.", "Find lines that match a regular expression."),
        obj(json!({"pattern": {"type": "string"}, "path": {"type": "string", "description": "file or folder to search in"}}), &["pattern"]),
    ));
    if offer.mode != Mode::Read {
        out.push(ToolSpec::new(
            "replace_lines",
            pick(
                "Replace lines `start` to `end` of a file with `new`, using the line numbers read_file showed. To insert before line N without removing anything pass start=N and end=N-1. An empty `new` removes the lines.",
                "Replace lines start..end of a file with `new` (line numbers from read_file). To insert before line N: start=N, end=N-1.",
            ),
            obj(
                json!({"path": {"type": "string"}, "start": {"type": "integer", "description": "first line to replace"}, "end": {"type": "integer", "description": "last line to replace"}, "new": {"type": "string", "description": "the new lines with their indentation, without line numbers"}}),
                &["path", "start", "end", "new"],
            ),
        ));
        if !small {
            out.push(ToolSpec::new(
                "edit_file",
                "Replace `old` with `new` in a file. `old` is the exact current text (no line numbers) and must name one place; keep it to the few lines that change.",
                obj(json!({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}), &["path", "old", "new"]),
            ));
        }
        out.push(ToolSpec::new(
            "write_file",
            pick(
                "Create a file, or replace a whole file, with `content`. For a change inside an existing file use replace_lines.",
                "Create a file, or replace a whole file, with `content`.",
            ),
            obj(json!({"path": {"type": "string"}, "content": {"type": "string"}}), &["path", "content"]),
        ));
    }
    if offer.has_tests {
        out.push(ToolSpec::new("run_tests", "Run the project's tests and see what fails.", obj(json!({}), &[])));
    }
    out.push(ToolSpec::new("run", "Run a shell command in the project folder.", obj(json!({"command": {"type": "string"}}), &["command"])));
    if offer.profile == Profile::Full {
        out.push(ToolSpec::new(
            "note",
            "Keep one short fact for the rest of the work. Notes stay in view when old results are dropped.",
            obj(json!({"text": {"type": "string"}}), &["text"]),
        ));
    }
    if offer.skills {
        out.push(ToolSpec::new(
            "use_skill",
            "Load the instructions of one of the listed skills.",
            obj(json!({"name": {"type": "string"}}), &["name"]),
        ));
    }
    if offer.done {
        out.push(ToolSpec::new(
            "done",
            "Say that the work is finished. The tests are run to check it.",
            obj(json!({"summary": {"type": "string", "description": "what was done, in one or two sentences"}}), &["summary"]),
        ));
    }
    out
}

/// The tool a model meant by a name it made up or carried over from another agent.
pub fn canonical_name(name: &str) -> Option<&'static str> {
    let key = name.trim().to_lowercase().replace(['-', ' ', '.'], "_");
    Some(match key.as_str() {
        "read_file" | "read" | "cat" | "view" | "open" | "open_file" | "view_file" | "readfile" => "read_file",
        "list_files" | "ls" | "list" | "list_dir" | "list_directory" | "glob" | "listfiles" | "tree" => "list_files",
        "search" | "grep" | "find" | "search_files" | "search_file" | "rg" | "grep_search" => "search",
        "replace_lines" | "replace_line" | "edit_lines" | "edit_line" | "line_edit" | "replacelines" | "set_lines" => "replace_lines",
        "edit_file" | "edit" | "replace" | "str_replace" | "apply_edit" | "editfile" | "str_replace_editor" | "replace_in_file" => {
            "edit_file"
        }
        "write_file" | "write" | "create" | "create_file" | "writefile" | "save_file" => "write_file",
        "run_tests" | "test" | "tests" | "run_test" | "pytest" | "runtests" => "run_tests",
        "run" | "bash" | "shell" | "exec" | "run_command" | "execute" | "execute_command" | "terminal" | "cmd" => "run",
        "note" | "remember" | "memo" | "save_note" | "take_note" => "note",
        "use_skill" | "skill" | "load_skill" | "read_skill" => "use_skill",
        "done" | "finish" | "finished" | "complete" | "submit" | "task_complete" | "attempt_completion" | "final_answer" => "done",
        _ => return None,
    })
}

const ARG_ALIASES: &[(&str, &[&str])] = &[
    ("path", &["file", "file_path", "filename", "filepath", "file_name", "target_file", "dir", "directory"]),
    ("old", &["old_string", "old_str", "old_text", "search", "find", "original"]),
    (
        "new",
        &[
            "new_string",
            "new_str",
            "new_text",
            "replace",
            "replacement",
            "updated",
            "new_content",
            "new_lines",
            "content",
            "text",
            "lines",
            "code",
        ],
    ),
    ("content", &["text", "contents", "body", "file_text", "data"]),
    ("command", &["cmd", "script", "shell_command"]),
    ("pattern", &["query", "regex", "q", "search_term"]),
    ("start", &["offset", "start_line", "line", "from", "first"]),
    ("end", &["end_line", "to", "last", "stop"]),
    ("limit", &["count", "max_lines", "num_lines"]),
    ("summary", &["message", "text", "result", "answer", "content"]),
    ("text", &["note", "content", "fact", "message"]),
    ("name", &["skill", "skill_name", "id"]),
];

/// Rename argument keys other agents use to the ones this tool takes. Known keys are left alone.
pub fn normalize_args(tool: &str, args: &mut Map<String, Value>) {
    let wanted: &[&str] = match tool {
        "read_file" => &["path", "start", "limit"],
        "list_files" => &["path"],
        "search" => &["pattern", "path"],
        "replace_lines" => &["path", "start", "end", "new"],
        "edit_file" => &["path", "old", "new"],
        "write_file" => &["path", "content"],
        "run" => &["command"],
        "note" => &["text"],
        "use_skill" => &["name"],
        "done" => &["summary"],
        _ => &[],
    };
    for key in wanted {
        if args.contains_key(*key) {
            continue;
        }
        let aliases = ARG_ALIASES.iter().find(|(k, _)| k == key).map_or(&[][..], |(_, a)| a);
        if let Some(alias) = aliases.iter().find(|a| args.contains_key(**a))
            && let Some(v) = args.remove(*alias)
        {
            args.insert(key.to_string(), v);
        }
    }
}

fn text_arg<'a>(args: &'a Map<String, Value>, key: &str) -> Result<&'a str, ToolResult> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(s),
        Some(_) => Err(ToolResult::failed(format!("`{key}` must be a string."))),
        None => Err(ToolResult::failed(format!("`{key}` is missing."))),
    }
}

fn number_arg(args: &Map<String, Value>, key: &str) -> Option<usize> {
    match args.get(key)? {
        Value::Number(n) => n.as_u64().map(|n| n as usize),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn permit(ctx: &Ctx<'_>, decision: Decision, action: &str) -> Result<(), ToolResult> {
    match decision {
        Decision::Allow => Ok(()),
        Decision::Deny(why) => Err(ToolResult::denied(format!("Refused: {why}. This is a fixed rule; work without it."))),
        Decision::Ask(why) => {
            if ctx.approver.approve(action, &why) {
                Ok(())
            } else {
                Err(ToolResult::denied(format!("Not approved: {why}. Go on without this step, or finish and say what is left.")))
            }
        }
    }
}

fn load(abs: &std::path::Path, rel: &str) -> Result<String, ToolResult> {
    if abs.is_dir() {
        return Err(ToolResult::failed(format!("{rel} is a folder. Use list_files.")));
    }
    let bytes = std::fs::read(abs).map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => ToolResult::failed(format!("{rel} does not exist. Use list_files to see the real names.")),
        _ => ToolResult::failed(format!("{rel} could not be read: {e}")),
    })?;
    if bytes.iter().take(8000).any(|&b| b == 0) {
        return Err(ToolResult::failed(format!("{rel} is a binary file.")));
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// Lines `from..=to` (1-based) in the `12| text` form.
pub fn numbered(lines: &[&str], from: usize, to: usize) -> String {
    let mut out = String::new();
    for (i, line) in lines.iter().enumerate().take(to).skip(from.saturating_sub(1)) {
        out.push_str(&format!("{}| {line}\n", i + 1));
    }
    out.truncate(out.trim_end_matches('\n').len());
    out
}

/// What `read_file` prints for a text with `\n` line ends, from line `start`, and the last line it shows.
/// The text has at least `start` lines.
pub fn page(rel: &str, text: &str, start: usize, limit: usize, max_chars: usize) -> (String, usize) {
    let lines = lines_of(text);
    let total = lines.len();
    let mut body = String::new();
    let mut last = start - 1;
    for (i, line) in lines.iter().enumerate().skip(start - 1).take(limit) {
        if body.len() + line.len() > max_chars && last >= start {
            break;
        }
        body.push_str(&format!("{}| {line}\n", i + 1));
        last = i + 1;
    }
    let mut out = format!("{rel} lines {start}-{last} of {total}\n{body}");
    if last < total {
        out.push_str(&format!("[more: read_file with start={}]", last + 1));
    }
    (out.trim_end().to_string(), last)
}

fn read_file(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = text_arg(args, "path")?;
    let target = ctx.ws.resolve(raw);
    permit(ctx, perm::check_read(target.as_ref().map(|t| t.rel.as_str())), &format!("read {raw}"))?;
    let target = target.expect("allowed paths resolve");
    let text = Eol::to_lf(&load(&target.abs, &target.rel)?);
    let total = lines_of(&text).len();
    if total == 0 {
        return Ok(ToolResult::ok(format!("{} is empty", target.rel)));
    }
    let start = number_arg(args, "start").unwrap_or(1).max(1);
    if start > total {
        return Err(ToolResult::failed(format!("{} has {total} lines; start={start} is past the end.", target.rel)));
    }
    let limit = number_arg(args, "limit").unwrap_or(READ_LINES).clamp(1, READ_LINES);
    let (out, last) = page(&target.rel, &text, start, limit, READ_CHARS);
    views(ctx).saw(&target.rel, &text, start, last);
    Ok(ToolResult::ok(out))
}

fn list_files(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = args.get("path").and_then(Value::as_str).unwrap_or(".");
    let target = ctx.ws.resolve(raw);
    permit(ctx, perm::check_read(target.as_ref().map(|t| t.rel.as_str())), &format!("list {raw}"))?;
    let target = target.expect("allowed paths resolve");
    if target.abs.is_file() {
        return Ok(ToolResult::ok(format!("{} is a file. Use read_file.", target.rel)));
    }
    if !target.abs.is_dir() {
        return Err(ToolResult::failed(format!("{} does not exist.", target.rel)));
    }
    let (files, more) = ctx.ws.files(&target.abs, LIST_LIMIT);
    let files: Vec<String> = files.into_iter().filter(|f| !perm::is_secret(f)).collect();
    if files.is_empty() {
        return Ok(ToolResult::ok(format!("{}: no files", target.rel)));
    }
    let mut out = format!("{}: {} files{}\n{}", target.rel, files.len(), if more { " (first part)" } else { "" }, files.join("\n"));
    if more {
        out.push_str("\n[more files exist: list a subfolder]");
    }
    Ok(ToolResult::ok(out))
}

fn search(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let pattern = text_arg(args, "pattern")?;
    if pattern.is_empty() {
        return Err(ToolResult::failed("`pattern` is empty."));
    }
    let raw = args.get("path").and_then(Value::as_str).unwrap_or(".");
    let target = ctx.ws.resolve(raw);
    permit(ctx, perm::check_read(target.as_ref().map(|t| t.rel.as_str())), &format!("search {raw}"))?;
    let target = target.expect("allowed paths resolve");
    // A pattern that is not a valid expression (`foo(`) is searched for as plain text.
    let re = regex::Regex::new(pattern)
        .or_else(|_| regex::Regex::new(&regex::escape(pattern)))
        .map_err(|e| ToolResult::failed(format!("bad pattern: {e}")))?;
    let files = if target.abs.is_file() { vec![target.rel.clone()] } else { ctx.ws.files(&target.abs, 5000).0 };
    let mut hits = Vec::new();
    let mut more = false;
    'files: for rel in files.iter().filter(|f| !perm::is_secret(f)) {
        let abs = ctx.ws.root().join(rel);
        if std::fs::metadata(&abs).map_or(true, |m| m.len() > SEARCH_FILE_BYTES) {
            continue;
        }
        let Ok(text) = load(&abs, rel) else { continue };
        for (i, line) in text.lines().enumerate() {
            if re.is_match(line) {
                if hits.len() == SEARCH_LIMIT {
                    more = true;
                    break 'files;
                }
                let shown: String = line.trim().chars().take(200).collect();
                hits.push(format!("{rel}:{}: {shown}", i + 1));
            }
        }
    }
    if hits.is_empty() {
        return Ok(ToolResult::ok(format!("search `{pattern}`: no matches in {}", target.rel)));
    }
    let mut out = format!("search `{pattern}`: {} matches{}\n{}", hits.len(), if more { " (first part)" } else { "" }, hits.join("\n"));
    if more {
        out.push_str("\n[more matches exist: narrow the pattern or the path]");
    }
    Ok(ToolResult::ok(out))
}

/// `lines 3-5`, or the place an insertion goes.
fn span(start: usize, end: usize) -> String {
    if end < start { format!("the place before line {start}") } else { format!("lines {start}-{end}") }
}

fn replace_lines(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = text_arg(args, "path")?;
    let new = text_arg(args, "new")?;
    let Some(start) = number_arg(args, "start") else {
        return Err(ToolResult::failed("`start` is missing: the number of the first line to replace."));
    };
    let end = number_arg(args, "end").unwrap_or(start);
    let target = ctx.ws.resolve(raw);
    let exists = target.as_ref().is_some_and(|t| t.abs.is_file());
    permit(ctx, perm::check_write(ctx.mode, target.as_ref().map(|t| t.rel.as_str()), exists), &format!("edit {raw}"))?;
    let target = target.expect("allowed paths resolve");
    let rel = target.rel.as_str();
    if !exists && !target.abs.is_dir() {
        return Err(ToolResult::failed(format!("{rel} does not exist. Use write_file to create it.")));
    }
    let original = load(&target.abs, rel)?;
    let text = Eol::to_lf(&original);
    let done = edit::replace_lines(&text, start, end, &Eol::to_lf(new)).map_err(|e| {
        ToolResult::failed(match e {
            LineError::BadRange { total } => {
                format!("{rel} has {total} lines; {} is not in it. To insert before line N pass start=N and end=N-1.", span(start, end))
            }
            LineError::Noop => format!("{rel}: {} already read exactly like `new`; nothing was changed.", span(start, end)),
        })
    })?;

    let mut seen = views(ctx);
    if !seen.has_seen(rel, &text, start, end) {
        let before = lines_of(&text);
        let (from, to) = (start.saturating_sub(3).max(1), (end.max(start) + 3).min(before.len()));
        if to + 1 - from > READ_LINES {
            return Err(ToolResult::failed(format!(
                "{rel}: you have not read {} as they are now, so nothing was changed. Read them with read_file (start={from}) first.",
                span(start, end)
            )));
        }
        // The lines are shown here, so the same call works when it is sent again.
        seen.saw(rel, &text, from, to);
        return Err(ToolResult::failed(format!(
            "{rel}: you have not read {} as they are now, so nothing was changed. They read:\n{}\nCheck the line numbers and send replace_lines again.",
            span(start, end),
            numbered(&before, from, to)
        )));
    }

    std::fs::write(&target.abs, Eol::detect(&original).restore(&done.text))
        .map_err(|e| ToolResult::failed(format!("{rel} could not be written: {e}")))?;
    let after = lines_of(&done.text);
    // Two lines of the file on each side of the new lines.
    let (from, to) = (start.saturating_sub(2).max(1), (start + done.new_lines + 1).min(after.len()));
    let shown = (from <= to).then_some((from, to));
    seen.edited(rel, &text, &done.text, Change { start, old_len: done.old_lines, new_len: done.new_lines }, shown);
    drop(seen);

    let what = match (done.old_lines, done.new_lines) {
        (0, n) => format!("{n} lines inserted at line {start}"),
        (_, 0) => format!("lines {start}-{end} removed"),
        (old, new) => format!("lines {start}-{end} replaced ({old} -> {new} lines)"),
    };
    let fitted = if done.indented { " (`new` had no indentation; it got that of the replaced lines)" } else { "" };
    let now = match shown {
        None => "Now the file is empty.".to_string(),
        Some((from, to)) if done.new_lines > ECHO_LINES => {
            let last_new = start + done.new_lines - 1;
            format!("Now:\n{}\n   ...\n{}", numbered(&after, from, start + 3), numbered(&after, last_new - 3, to))
        }
        Some((from, to)) => format!("Now:\n{}", numbered(&after, from, to)),
    };
    Ok(ToolResult { status: Status::Ok, text: format!("{rel}: {what}{fitted}. {now}"), changed: Some(target.rel) })
}

fn edit_file(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = text_arg(args, "path")?;
    let (old, new) = (text_arg(args, "old")?, text_arg(args, "new")?);
    let target = ctx.ws.resolve(raw);
    let exists = target.as_ref().is_some_and(|t| t.abs.is_file());
    permit(ctx, perm::check_write(ctx.mode, target.as_ref().map(|t| t.rel.as_str()), exists), &format!("edit {raw}"))?;
    let target = target.expect("allowed paths resolve");
    if !exists && !target.abs.is_dir() {
        return Err(ToolResult::failed(format!("{} does not exist. Use write_file to create it.", target.rel)));
    }
    let original = load(&target.abs, &target.rel)?;
    let eol = Eol::detect(&original);
    let text = Eol::to_lf(&original);
    let applied = edit::apply_edit(&text, &Eol::to_lf(old), &Eol::to_lf(new)).map_err(|e| ToolResult::failed(e.message(&target.rel)))?;
    std::fs::write(&target.abs, eol.restore(&applied.text))
        .map_err(|e| ToolResult::failed(format!("{} could not be written: {e}", target.rel)))?;
    let change = Change { start: applied.line, old_len: applied.old_lines, new_len: applied.new_lines };
    views(ctx).edited(&target.rel, &text, &applied.text, change, None);
    let end = applied.line + applied.new_lines.max(1) - 1;
    let note = match applied.matched {
        Matched::Exact | Matched::TrailingSpace => "",
        Matched::Indentation => " (indentation fitted to the file)",
        Matched::LineNumbers => " (line numbers removed from old)",
    };
    let text =
        format!("{}: lines {}-{end} replaced ({} -> {} lines){note}", target.rel, applied.line, applied.old_lines, applied.new_lines);
    Ok(ToolResult { status: Status::Ok, text, changed: Some(target.rel) })
}

fn write_file(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = text_arg(args, "path")?;
    let content = text_arg(args, "content")?;
    let target = ctx.ws.resolve(raw);
    let exists = target.as_ref().is_some_and(|t| t.abs.is_file());
    permit(ctx, perm::check_write(ctx.mode, target.as_ref().map(|t| t.rel.as_str()), exists), &format!("write {raw}"))?;
    let target = target.expect("allowed paths resolve");
    if target.abs.is_dir() {
        return Err(ToolResult::failed(format!("{} is a folder.", target.rel)));
    }
    let mut content = Eol::to_lf(content);
    if !content.is_empty() && !content.ends_with('\n') {
        content.push('\n');
    }
    let lines = content.lines().count();
    if exists {
        let original = load(&target.abs, &target.rel)?;
        if Eol::to_lf(&original) == content {
            return Ok(ToolResult::ok(format!("{}: unchanged, the file already has this content", target.rel)));
        }
        content = Eol::detect(&original).restore(&content);
    } else if let Some(parent) = target.abs.parent() {
        std::fs::create_dir_all(parent).map_err(|e| ToolResult::failed(format!("the folder for {} could not be made: {e}", target.rel)))?;
    }
    std::fs::write(&target.abs, content).map_err(|e| ToolResult::failed(format!("{} could not be written: {e}", target.rel)))?;
    views(ctx).forget(&target.rel);
    let text = format!("{}: {} ({lines} lines)", target.rel, if exists { "replaced" } else { "created" });
    Ok(ToolResult { status: Status::Ok, text, changed: Some(target.rel) })
}

/// A failure the isolation caused reads like any other; these words tell them apart.
fn blocked(output: &str) -> bool {
    let low = output.to_lowercase();
    ["permission denied", "access is denied", "operation not permitted", "read-only file system", "액세스가 거부"]
        .iter()
        .any(|w| low.contains(w))
}

async fn run(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let cmd = text_arg(args, "command")?.trim();
    let decision = perm::check_command(ctx.mode, cmd);
    // A person said yes to a command that needs the network or a package store: it cannot work isolated.
    let outside = matches!(decision, Decision::Ask(_)) && perm::leaves_isolation(cmd);
    permit(ctx, decision, &format!("run `{cmd}`"))?;
    let sandbox = if outside { &sandbox::OFF } else { ctx.sandbox };
    let out = exec::shell_in(sandbox, cmd, ctx.ws.root(), ctx.command_timeout).await;
    let code = out.code.map_or("none".to_string(), |c| c.to_string());
    let mut text = format!("$ {cmd} -> exit {code}\n{}", out.text).trim_end().to_string();
    if sandbox.is_on() && !out.ok() && blocked(&out.text) {
        text.push('\n');
        text.push_str(ISOLATION_NOTE);
    }
    Ok(ToolResult { status: if out.ok() { Status::Ok } else { Status::Failed }, text, changed: None })
}

/// Run one tool. `name` is a canonical name. `run_tests`, `done`, `note` and `use_skill` belong to
/// the loop, which owns the gate and the session.
pub async fn run_tool(ctx: &Ctx<'_>, name: &str, args: &Map<String, Value>) -> ToolResult {
    let done = match name {
        "read_file" => read_file(ctx, args),
        "list_files" => list_files(ctx, args),
        "search" => search(ctx, args),
        "replace_lines" => replace_lines(ctx, args),
        "edit_file" => edit_file(ctx, args),
        "write_file" => write_file(ctx, args),
        "run" => run(ctx, args).await,
        other => Err(ToolResult::failed(format!("there is no tool named {other}."))),
    };
    done.unwrap_or_else(|refused| refused)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::perm::{AllowAll, DenyAll};
    use crate::testing::TempDir;

    fn args(v: Value) -> Map<String, Value> {
        v.as_object().expect("object").clone()
    }

    async fn call_seen(t: &TempDir, mode: Mode, approver: &dyn Approver, views: &Mutex<Views>, name: &str, a: Value) -> ToolResult {
        let ws = Workspace::new(t.path()).unwrap();
        let ctx = Ctx { ws: &ws, mode, approver, command_timeout: Duration::from_secs(30), views, sandbox: &sandbox::OFF };
        run_tool(&ctx, name, &args(a)).await
    }

    async fn call(t: &TempDir, mode: Mode, approver: &dyn Approver, name: &str, a: Value) -> ToolResult {
        call_seen(t, mode, approver, &Mutex::new(Views::default()), name, a).await
    }

    fn project() -> TempDir {
        let t = TempDir::new("tools");
        t.write("calc.py", "def add(a, b):\n    return a - b\n")
            .write("tests/test_calc.py", "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n");
        t.write(".env", "KEY=secret-value\n");
        t
    }

    #[tokio::test]
    async fn read_file_numbers_lines_and_pages() {
        let t = project();
        t.write("long.txt", &(1..=600).map(|i| format!("row {i}\n")).collect::<String>());
        let r = call(&t, Mode::Edit, &DenyAll, "read_file", json!({"path": "calc.py"})).await;
        assert_eq!(r.text, "calc.py lines 1-2 of 2\n1| def add(a, b):\n2|     return a - b");
        let r = call(&t, Mode::Edit, &DenyAll, "read_file", json!({"path": "long.txt"})).await;
        assert!(r.text.starts_with("long.txt lines 1-250 of 600\n1| row 1\n"));
        assert!(r.text.ends_with("[more: read_file with start=251]"));
        let r = call(&t, Mode::Edit, &DenyAll, "read_file", json!({"path": "long.txt", "start": "599", "limit": 10})).await;
        assert_eq!(r.text, "long.txt lines 599-600 of 600\n599| row 599\n600| row 600");
        let r = call(&t, Mode::Edit, &DenyAll, "read_file", json!({"path": "nope.py"})).await;
        assert_eq!(r.status, Status::Failed);
    }

    #[tokio::test]
    async fn secrets_and_outside_paths_are_refused() {
        let t = project();
        for (tool, a) in [
            ("read_file", json!({"path": ".env"})),
            ("read_file", json!({"path": "../x.txt"})),
            ("write_file", json!({"path": "../x.txt", "content": "x"})),
            ("write_file", json!({"path": ".env", "content": "x"})),
            ("edit_file", json!({"path": ".git/config", "old": "a", "new": "b"})),
            ("replace_lines", json!({"path": ".env", "start": 1, "end": 1, "new": "KEY=x"})),
            ("run", json!({"command": "cat .env"})),
        ] {
            let r = call(&t, Mode::Auto, &AllowAll, tool, a.clone()).await;
            assert_eq!(r.status, Status::Denied, "{tool} {a}");
            assert!(!r.text.contains("secret-value"));
        }
        let r = call(&t, Mode::Auto, &AllowAll, "list_files", json!({})).await;
        assert!(!r.text.contains(".env") && r.text.contains("calc.py"), "{}", r.text);
        let r = call(&t, Mode::Auto, &AllowAll, "search", json!({"pattern": "secret"})).await;
        assert!(r.text.contains("no matches"), "{}", r.text);
    }

    #[tokio::test]
    async fn edit_and_write_change_files_and_report_them() {
        let t = project();
        let r = call(&t, Mode::Edit, &DenyAll, "edit_file", json!({"path": "calc.py", "old": "return a - b", "new": "return a + b"})).await;
        assert_eq!((r.status, r.changed.as_deref()), (Status::Ok, Some("calc.py")));
        assert_eq!(r.text, "calc.py: lines 2-2 replaced (1 -> 1 lines)");
        assert_eq!(t.read("calc.py"), "def add(a, b):\n    return a + b\n");
        let r = call(&t, Mode::Edit, &DenyAll, "edit_file", json!({"path": "calc.py", "old": "return a * b", "new": "x"})).await;
        assert_eq!(r.status, Status::Failed);
        let r = call(&t, Mode::Edit, &DenyAll, "write_file", json!({"path": "pkg/new.py", "content": "x = 1"})).await;
        assert_eq!(r.text, "pkg/new.py: created (1 lines)");
        assert_eq!(t.read("pkg/new.py"), "x = 1\n");
        let r = call(&t, Mode::Edit, &DenyAll, "write_file", json!({"path": "pkg/new.py", "content": "x = 1\n"})).await;
        assert!(r.text.contains("unchanged") && r.changed.is_none());
    }

    #[tokio::test]
    async fn replace_lines_works_on_lines_that_were_read() {
        let t = project();
        let seen = Mutex::new(Views::default());
        let fix = json!({"path": "calc.py", "start": 2, "end": 2, "new": "    return a + b"});
        // Unread lines: nothing changes, the lines are shown, and the same call then works.
        let r = call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", fix.clone()).await;
        assert_eq!(r.status, Status::Failed);
        assert!(r.text.contains("have not read lines 2-2") && r.text.contains("2|     return a - b"), "{}", r.text);
        assert!(t.read("calc.py").contains("a - b"));
        let r = call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", fix).await;
        assert_eq!((r.status, r.changed.as_deref()), (Status::Ok, Some("calc.py")));
        assert_eq!(r.text, "calc.py: lines 2-2 replaced (1 -> 1 lines). Now:\n1| def add(a, b):\n2|     return a + b");

        // Appending after the last line; the result showed it, so no new read is needed.
        let more = json!({"path": "calc.py", "start": 3, "end": 2, "new": "\n\ndef sub(a, b):\n    return a - b"});
        let r = call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", more).await;
        assert!(r.text.starts_with("calc.py: 4 lines inserted at line 3. Now:\n1| def add(a, b):\n"), "{}", r.text);
        assert!(r.text.ends_with("5| def sub(a, b):\n6|     return a - b"), "{}", r.text);
        assert_eq!(t.read("calc.py"), "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n");

        // Line numbers copied into `new` are dropped; a missing `end` means one line.
        let r = call_seen(
            &t,
            Mode::Edit,
            &DenyAll,
            &seen,
            "replace_lines",
            json!({"path": "calc.py", "start": 6, "new": "6|     return b - a"}),
        )
        .await;
        assert_eq!(r.status, Status::Ok, "{}", r.text);
        assert!(t.read("calc.py").ends_with("    return b - a\n"));

        let r =
            call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", json!({"path": "calc.py", "start": 3, "end": 4, "new": ""})).await;
        assert!(r.text.starts_with("calc.py: lines 3-4 removed. Now:\n1| def add(a, b):"), "{}", r.text);
        assert_eq!(t.read("calc.py").lines().count(), 4);

        // The statement alone, without its leading spaces: it goes where the old one stood.
        let r = call_seen(
            &t,
            Mode::Edit,
            &DenyAll,
            &seen,
            "replace_lines",
            json!({"path": "calc.py", "start": 2, "end": 2, "new": "return b + a"}),
        )
        .await;
        assert!(
            r.text.starts_with(
                "calc.py: lines 2-2 replaced (1 -> 1 lines) (`new` had no indentation; it got that of the replaced lines). Now:"
            ),
            "{}",
            r.text
        );
        assert!(
            r.text.contains("2|     return b + a") && t.read("calc.py").starts_with("def add(a, b):\n    return b + a\n"),
            "{}",
            r.text
        );

        let r = call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", json!({"path": "calc.py", "start": 40, "end": 41, "new": "x"}))
            .await;
        assert!(r.status == Status::Failed && r.text.contains("has 4 lines"), "{}", r.text);
        let r = call_seen(
            &t,
            Mode::Edit,
            &DenyAll,
            &seen,
            "replace_lines",
            json!({"path": "calc.py", "start": 1, "end": 1, "new": "def add(a, b):"}),
        )
        .await;
        assert!(r.status == Status::Failed && r.text.contains("already read exactly"), "{}", r.text);
        let r = call_seen(&t, Mode::Read, &AllowAll, &seen, "replace_lines", json!({"path": "calc.py", "start": 1, "end": 1, "new": "x"}))
            .await;
        assert_eq!(r.status, Status::Denied);
    }

    #[tokio::test]
    async fn a_file_changed_by_another_tool_has_to_be_read_again() {
        let t = project();
        let seen = Mutex::new(Views::default());
        call_seen(&t, Mode::Edit, &DenyAll, &seen, "read_file", json!({"path": "calc.py"})).await;
        call_seen(
            &t,
            Mode::Edit,
            &DenyAll,
            &seen,
            "write_file",
            json!({"path": "calc.py", "content": "import os\n\ndef add(a, b):\n    return a - b\n"}),
        )
        .await;
        let r = call_seen(
            &t,
            Mode::Edit,
            &DenyAll,
            &seen,
            "replace_lines",
            json!({"path": "calc.py", "start": 2, "end": 2, "new": "    return a + b"}),
        )
        .await;
        assert_eq!(r.status, Status::Failed, "line 2 is not the line the model read");
        assert!(t.read("calc.py").contains("import os\n\ndef add"));
    }

    #[tokio::test]
    async fn line_ends_of_the_file_are_kept() {
        let t = TempDir::new("tools");
        t.write("w.txt", "a = 1\r\nb = 2\r\n");
        call(&t, Mode::Edit, &DenyAll, "edit_file", json!({"path": "w.txt", "old": "b = 2", "new": "b = 3\nc = 4"})).await;
        assert_eq!(t.read("w.txt"), "a = 1\r\nb = 3\r\nc = 4\r\n");
        let seen = Mutex::new(Views::default());
        call_seen(&t, Mode::Edit, &DenyAll, &seen, "read_file", json!({"path": "w.txt"})).await;
        call_seen(&t, Mode::Edit, &DenyAll, &seen, "replace_lines", json!({"path": "w.txt", "start": 1, "end": 1, "new": "a = 0"})).await;
        assert_eq!(t.read("w.txt"), "a = 0\r\nb = 3\r\nc = 4\r\n");
    }

    #[tokio::test]
    async fn existing_tests_need_a_yes_and_read_mode_changes_nothing() {
        let t = project();
        let edit = json!({"path": "tests/test_calc.py", "old": "== 5", "new": "== -1"});
        let r = call(&t, Mode::Auto, &DenyAll, "edit_file", edit.clone()).await;
        assert_eq!(r.status, Status::Denied);
        assert!(t.read("tests/test_calc.py").contains("== 5"));
        let r = call(&t, Mode::Auto, &AllowAll, "edit_file", edit).await;
        assert_eq!(r.status, Status::Ok);
        let r = call(&t, Mode::Read, &AllowAll, "write_file", json!({"path": "a.txt", "content": "x"})).await;
        assert_eq!(r.status, Status::Denied);
    }

    #[tokio::test]
    async fn search_finds_lines_and_falls_back_to_plain_text() {
        let t = project();
        let r = call(&t, Mode::Edit, &DenyAll, "search", json!({"pattern": "def \\w+"})).await;
        assert!(r.text.contains("calc.py:1: def add(a, b):") && r.text.contains("tests/test_calc.py:3: def test_add():"), "{}", r.text);
        let r = call(&t, Mode::Edit, &DenyAll, "search", json!({"pattern": "add(a", "path": "calc.py"})).await;
        assert!(r.text.contains("1 matches") && r.text.contains("calc.py:1:"), "{}", r.text);
    }

    #[tokio::test]
    async fn run_reports_the_exit_code() {
        let t = project();
        let r = call(&t, Mode::Edit, &DenyAll, "run", json!({"command": "echo hi"})).await;
        assert_eq!((r.status, r.text.as_str()), (Status::Ok, "$ echo hi -> exit 0\nhi"));
        let r = call(&t, Mode::Edit, &DenyAll, "run", json!({"command": "python make.py"})).await;
        assert_eq!(r.status, Status::Denied, "edit mode asks, and nobody is there to say yes");
        let r = call(&t, Mode::Edit, &DenyAll, "run", json!({"command": "rm -rf ."})).await;
        assert!(r.text.starts_with("Refused"));
    }

    #[test]
    fn names_and_arguments_of_other_agents_are_understood() {
        assert_eq!(canonical_name("Read"), Some("read_file"));
        assert_eq!(canonical_name("str-replace"), Some("edit_file"));
        assert_eq!(canonical_name("bash"), Some("run"));
        assert_eq!(canonical_name("edit_lines"), Some("replace_lines"));
        assert_eq!(canonical_name("attempt_completion"), Some("done"));
        assert_eq!(canonical_name("teleport"), None);
        let mut a = args(json!({"file_path": "a.py", "old_string": "x", "new_str": "y"}));
        normalize_args("edit_file", &mut a);
        assert_eq!(Value::Object(a), json!({"path": "a.py", "old": "x", "new": "y"}));
        let mut a = args(json!({"path": "a.py", "file": "b.py"}));
        normalize_args("read_file", &mut a);
        assert_eq!(a["path"], "a.py");
        let mut a = args(json!({"file": "a.py", "start_line": 3, "end_line": 4, "content": "x"}));
        normalize_args("replace_lines", &mut a);
        assert_eq!(Value::Object(a), json!({"path": "a.py", "start": 3, "end": 4, "new": "x"}));
    }

    #[test]
    fn a_small_model_gets_a_short_tool_list() {
        let names = |offer: Offer| specs(&offer).iter().map(|s| s.name.to_string()).collect::<Vec<_>>();
        assert_eq!(names(Offer::new(Mode::Read, false, Profile::Small)), ["read_file", "list_files", "search", "run"]);
        assert_eq!(
            names(Offer::new(Mode::Edit, true, Profile::Small)),
            ["read_file", "search", "replace_lines", "write_file", "run_tests", "run"]
        );
        assert_eq!(
            names(Offer { listing_cut: true, ..Offer::new(Mode::Edit, false, Profile::Small) }),
            ["read_file", "list_files", "search", "replace_lines", "write_file", "run"]
        );
        assert_eq!(
            names(Offer::new(Mode::Edit, true, Profile::Standard)),
            ["read_file", "list_files", "search", "replace_lines", "edit_file", "write_file", "run_tests", "run"]
        );
        let full = names(Offer { done: true, skills: true, ..Offer::new(Mode::Auto, true, Profile::Full) });
        assert_eq!(full[8..], ["note", "use_skill", "done"]);
        assert_eq!((Profile::for_window(8192), Profile::for_window(16_384)), (Profile::Small, Profile::Small));
        assert_eq!((Profile::for_window(32_768), Profile::for_window(128_000)), (Profile::Standard, Profile::Full));
        assert_eq!(Profile::parse("Full"), Some(Profile::Full));
        assert_eq!(Profile::parse("tiny"), None);
        // Every description of the short list is shorter than the long one or the same.
        let size = |p: Profile| specs(&Offer::new(Mode::Edit, true, p)).iter().map(|s| s.description.len()).sum::<usize>();
        assert!(size(Profile::Small) < size(Profile::Standard));
    }
}
