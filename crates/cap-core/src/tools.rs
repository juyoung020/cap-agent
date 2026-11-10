//! The tools a model can call. Every call passes the permission table before it touches anything.
//!
//! Results start with a one-line header (`path lines a-b of N`, `$ cmd -> exit 0`), which is
//! what remains when an old result is dropped from the context.

use crate::edit::{self, Eol, Matched};
use crate::exec;
use crate::model::ToolSpec;
use crate::perm::{self, Approver, Decision, Mode};
use crate::workspace::Workspace;
use serde_json::{Map, Value, json};
use std::time::Duration;

pub const READ_LINES: usize = 250;
pub const READ_CHARS: usize = 12_000;
pub const LIST_LIMIT: usize = 200;
pub const SEARCH_LIMIT: usize = 40;
const SEARCH_FILE_BYTES: u64 = 1_000_000;

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
    fn ok(text: impl Into<String>) -> Self {
        Self { status: Status::Ok, text: text.into(), changed: None }
    }
    fn failed(text: impl Into<String>) -> Self {
        Self { status: Status::Failed, text: text.into(), changed: None }
    }
    fn denied(text: impl Into<String>) -> Self {
        Self { status: Status::Denied, text: text.into(), changed: None }
    }
}

pub struct Ctx<'a> {
    pub ws: &'a Workspace,
    pub mode: Mode,
    pub approver: &'a dyn Approver,
    pub command_timeout: Duration,
}

/// The tools offered in this mode. Fewer tools means fewer wrong picks by a small model.
pub fn specs(mode: Mode, has_tests: bool) -> Vec<ToolSpec> {
    let obj = |props: Value, required: &[&str]| json!({"type": "object", "properties": props, "required": required});
    let mut out = vec![
        ToolSpec {
            name: "read_file",
            description: "Read a text file. Lines come back numbered as `12| text`. Long files come in parts: pass start to continue.",
            parameters: obj(
                json!({"path": {"type": "string"}, "start": {"type": "integer", "description": "first line, from 1"}, "limit": {"type": "integer", "description": "number of lines"}}),
                &["path"],
            ),
        },
        ToolSpec {
            name: "list_files",
            description: "List the files of the project or of one folder.",
            parameters: obj(json!({"path": {"type": "string", "description": "folder; leave out for the whole project"}}), &[]),
        },
        ToolSpec {
            name: "search",
            description: "Find lines that match a regular expression. Returns `path:line: text`.",
            parameters: obj(
                json!({"pattern": {"type": "string"}, "path": {"type": "string", "description": "file or folder to search in"}}),
                &["pattern"],
            ),
        },
    ];
    if mode != Mode::Read {
        out.push(ToolSpec {
            name: "edit_file",
            description: "Replace `old` with `new` in a file. `old` is the exact current text (no line numbers) and must name one place; keep it to the few lines that change.",
            parameters: obj(json!({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}), &["path", "old", "new"]),
        });
        out.push(ToolSpec {
            name: "write_file",
            description: "Create a file, or replace a whole file, with `content`. For a change inside an existing file use edit_file.",
            parameters: obj(json!({"path": {"type": "string"}, "content": {"type": "string"}}), &["path", "content"]),
        });
    }
    if has_tests {
        out.push(ToolSpec {
            name: "run_tests",
            description: "Run the project's tests and see what fails.",
            parameters: obj(json!({}), &[]),
        });
    }
    out.push(ToolSpec {
        name: "run",
        description: "Run a shell command in the project folder.",
        parameters: obj(json!({"command": {"type": "string"}}), &["command"]),
    });
    out
}

/// The tool a model meant by a name it made up or carried over from another agent.
pub fn canonical_name(name: &str) -> Option<&'static str> {
    let key = name.trim().to_lowercase().replace(['-', ' ', '.'], "_");
    Some(match key.as_str() {
        "read_file" | "read" | "cat" | "view" | "open" | "open_file" | "view_file" | "readfile" => "read_file",
        "list_files" | "ls" | "list" | "list_dir" | "list_directory" | "glob" | "listfiles" | "tree" => "list_files",
        "search" | "grep" | "find" | "search_files" | "search_file" | "rg" | "grep_search" => "search",
        "edit_file" | "edit" | "replace" | "str_replace" | "apply_edit" | "editfile" | "str_replace_editor" | "replace_in_file" => {
            "edit_file"
        }
        "write_file" | "write" | "create" | "create_file" | "writefile" | "save_file" => "write_file",
        "run_tests" | "test" | "tests" | "run_test" | "pytest" | "runtests" => "run_tests",
        "run" | "bash" | "shell" | "exec" | "run_command" | "execute" | "execute_command" | "terminal" | "cmd" => "run",
        _ => return None,
    })
}

const ARG_ALIASES: &[(&str, &[&str])] = &[
    ("path", &["file", "file_path", "filename", "filepath", "file_name", "target_file", "dir", "directory"]),
    ("old", &["old_string", "old_str", "old_text", "search", "find", "original"]),
    ("new", &["new_string", "new_str", "new_text", "replace", "replacement", "updated"]),
    ("content", &["text", "contents", "body", "file_text", "data"]),
    ("command", &["cmd", "script", "shell_command"]),
    ("pattern", &["query", "regex", "q", "search_term"]),
    ("start", &["offset", "start_line", "line", "from"]),
    ("limit", &["count", "max_lines", "num_lines"]),
];

/// Rename argument keys other agents use to the ones this tool takes. Known keys are left alone.
pub fn normalize_args(tool: &str, args: &mut Map<String, Value>) {
    let wanted: &[&str] = match tool {
        "read_file" => &["path", "start", "limit"],
        "list_files" => &["path"],
        "search" => &["pattern", "path"],
        "edit_file" => &["path", "old", "new"],
        "write_file" => &["path", "content"],
        "run" => &["command"],
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

fn read_file(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let raw = text_arg(args, "path")?;
    let target = ctx.ws.resolve(raw);
    permit(ctx, perm::check_read(target.as_ref().map(|t| t.rel.as_str())), &format!("read {raw}"))?;
    let target = target.expect("allowed paths resolve");
    let text = Eol::to_lf(&load(&target.abs, &target.rel)?);
    let lines: Vec<&str> = text.strip_suffix('\n').unwrap_or(&text).split('\n').collect();
    let total = if text.is_empty() { 0 } else { lines.len() };
    if total == 0 {
        return Ok(ToolResult::ok(format!("{} is empty", target.rel)));
    }
    let start = number_arg(args, "start").unwrap_or(1).max(1);
    if start > total {
        return Err(ToolResult::failed(format!("{} has {total} lines; start={start} is past the end.", target.rel)));
    }
    let limit = number_arg(args, "limit").unwrap_or(READ_LINES).clamp(1, READ_LINES);
    let mut body = String::new();
    let mut last = start - 1;
    for (i, line) in lines.iter().enumerate().skip(start - 1).take(limit) {
        if body.len() + line.len() > READ_CHARS && last >= start {
            break;
        }
        body.push_str(&format!("{}| {line}\n", i + 1));
        last = i + 1;
    }
    let mut out = format!("{} lines {start}-{last} of {total}\n{body}", target.rel);
    if last < total {
        out.push_str(&format!("[more: read_file with start={}]", last + 1));
    }
    Ok(ToolResult::ok(out.trim_end().to_string()))
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
    let applied = edit::apply_edit(&Eol::to_lf(&original), &Eol::to_lf(old), &Eol::to_lf(new))
        .map_err(|e| ToolResult::failed(e.message(&target.rel)))?;
    std::fs::write(&target.abs, eol.restore(&applied.text))
        .map_err(|e| ToolResult::failed(format!("{} could not be written: {e}", target.rel)))?;
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
    let text = format!("{}: {} ({lines} lines)", target.rel, if exists { "replaced" } else { "created" });
    Ok(ToolResult { status: Status::Ok, text, changed: Some(target.rel) })
}

async fn run(ctx: &Ctx<'_>, args: &Map<String, Value>) -> Result<ToolResult, ToolResult> {
    let cmd = text_arg(args, "command")?.trim();
    permit(ctx, perm::check_command(ctx.mode, cmd), &format!("run `{cmd}`"))?;
    let out = exec::shell(cmd, ctx.ws.root(), ctx.command_timeout).await;
    let code = out.code.map_or("none".to_string(), |c| c.to_string());
    let text = format!("$ {cmd} -> exit {code}\n{}", out.text).trim_end().to_string();
    Ok(ToolResult { status: if out.ok() { Status::Ok } else { Status::Failed }, text, changed: None })
}

/// Run one tool. `name` is a canonical name; `run_tests` belongs to the loop, which owns the gate.
pub async fn run_tool(ctx: &Ctx<'_>, name: &str, args: &Map<String, Value>) -> ToolResult {
    let done = match name {
        "read_file" => read_file(ctx, args),
        "list_files" => list_files(ctx, args),
        "search" => search(ctx, args),
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

    async fn call(t: &TempDir, mode: Mode, approver: &dyn Approver, name: &str, a: Value) -> ToolResult {
        let ws = Workspace::new(t.path()).unwrap();
        let ctx = Ctx { ws: &ws, mode, approver, command_timeout: Duration::from_secs(30) };
        run_tool(&ctx, name, &args(a)).await
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
    async fn line_ends_of_the_file_are_kept() {
        let t = TempDir::new("tools");
        t.write("w.txt", "a = 1\r\nb = 2\r\n");
        call(&t, Mode::Edit, &DenyAll, "edit_file", json!({"path": "w.txt", "old": "b = 2", "new": "b = 3\nc = 4"})).await;
        assert_eq!(t.read("w.txt"), "a = 1\r\nb = 3\r\nc = 4\r\n");
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
        assert_eq!(canonical_name("teleport"), None);
        let mut a = args(json!({"file_path": "a.py", "old_string": "x", "new_str": "y"}));
        normalize_args("edit_file", &mut a);
        assert_eq!(Value::Object(a), json!({"path": "a.py", "old": "x", "new": "y"}));
        let mut a = args(json!({"path": "a.py", "file": "b.py"}));
        normalize_args("read_file", &mut a);
        assert_eq!(a["path"], "a.py");
        let names: Vec<_> = specs(Mode::Read, false).iter().map(|s| s.name).collect();
        assert_eq!(names, ["read_file", "list_files", "search", "run"]);
        assert_eq!(specs(Mode::Edit, true).len(), 7);
    }
}
