//! The permission table: what a tool call may do, decided by code before anything runs.
//!
//! Paths arrive here already resolved (`None` = outside the project), so this file is pure.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Look only: no file changes; commands other than read-only ones need approval.
    Read,
    /// Edit files and run tests inside the project; other commands need approval.
    Edit,
    /// Like `Edit`, and ordinary commands run without asking.
    Auto,
}

impl Mode {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "read" => Some(Mode::Read),
            "edit" => Some(Mode::Edit),
            "auto" => Some(Mode::Auto),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Read => "read",
            Mode::Edit => "edit",
            Mode::Auto => "auto",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Decision {
    Allow,
    /// A person has to say yes.
    Ask(String),
    Deny(String),
}

/// Who answers an `Ask`. The terminal prompt implements this; without a person the answer is no.
pub trait Approver: Sync {
    fn approve(&self, action: &str, why: &str) -> bool;
}

pub struct DenyAll;
impl Approver for DenyAll {
    fn approve(&self, _: &str, _: &str) -> bool {
        false
    }
}

pub struct AllowAll;
impl Approver for AllowAll {
    fn approve(&self, _: &str, _: &str) -> bool {
        true
    }
}

const SECRET_NAMES: &[&str] = &[
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "auth.json",
    "credentials.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
];
const SECRET_EXTS: &[&str] = &[".pem", ".key", ".pfx", ".p12", ".keystore"];
const SECRET_DIRS: &[&str] = &[".ssh", ".aws", ".gnupg"];
const INSTRUCTION_FILES: &[&str] = &["cap.md", "cap.local.md", "agents.md", "claude.md"];

fn parts(rel: &str) -> Vec<String> {
    rel.replace('\\', "/").split('/').filter(|p| !p.is_empty()).map(str::to_lowercase).collect()
}

fn secret_name(name: &str) -> bool {
    (name.starts_with(".env") && name != ".env.example" && name != ".env.sample" && name != ".env.template")
        || SECRET_NAMES.contains(&name)
        || SECRET_EXTS.iter().any(|e| name.ends_with(e))
}

/// Files that hold keys or tokens. Never read, never written, in any mode.
pub fn is_secret(rel: &str) -> bool {
    let p = parts(rel);
    let Some((name, dirs)) = p.split_last() else { return false };
    secret_name(name) || dirs.iter().chain([name]).any(|d| SECRET_DIRS.contains(&d.as_str()))
}

pub fn in_git_dir(rel: &str) -> bool {
    parts(rel).iter().any(|p| p == ".git")
}

/// Files that steer the agent itself; a model must not rewrite its own instructions unasked.
pub fn is_instruction_file(rel: &str) -> bool {
    parts(rel).last().is_some_and(|n| INSTRUCTION_FILES.contains(&n.as_str()))
}

/// Test files, by the usual naming of the common languages.
pub fn is_test_file(rel: &str) -> bool {
    let p = parts(rel);
    let Some((name, dirs)) = p.split_last() else { return false };
    let stem = name.split('.').next().unwrap_or("");
    dirs.iter().any(|d| matches!(d.as_str(), "tests" | "test" | "__tests__" | "spec"))
        || stem.starts_with("test_")
        || stem.ends_with("_test")
        || name == "conftest.py"
        || name.contains(".test.")
        || name.contains(".spec.")
}

pub fn check_read(rel: Option<&str>) -> Decision {
    match rel {
        None => Decision::Deny("the path is outside the project".into()),
        Some(r) if is_secret(r) => Decision::Deny("this file holds secrets".into()),
        Some(r) if in_git_dir(r) => Decision::Deny(".git is managed by git".into()),
        Some(_) => Decision::Allow,
    }
}

/// `exists`: the file is already there (changing an existing test is different from adding one).
pub fn check_write(mode: Mode, rel: Option<&str>, exists: bool) -> Decision {
    let Some(rel) = rel else { return Decision::Deny("the path is outside the project".into()) };
    if is_secret(rel) {
        return Decision::Deny("this file holds secrets".into());
    }
    if in_git_dir(rel) {
        return Decision::Deny(".git is managed by git".into());
    }
    if mode == Mode::Read {
        return Decision::Deny("read mode: files are not changed".into());
    }
    if is_instruction_file(rel) {
        return Decision::Ask("this file holds the agent's instructions".into());
    }
    if exists && is_test_file(rel) {
        return Decision::Ask("this changes an existing test, and the tests are what judge the work".into());
    }
    Decision::Allow
}

const READ_ONLY: &[&str] = &[
    "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "rg", "findstr", "pwd", "cd", "echo", "which", "where", "tree", "file",
    "stat", "more",
];
const GIT_READ_ONLY: &[&str] = &["status", "diff", "log", "show", "branch", "blame", "ls-files", "rev-parse"];
const NETWORK: &[&str] =
    &["curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"];
const INSTALLERS: &[&str] = &["apt", "apt-get", "yum", "dnf", "pacman", "brew", "choco", "winget", "scoop", "snap"];

fn words(segment: &str) -> Vec<String> {
    segment.split_whitespace().map(|w| w.trim_matches(|c| c == '"' || c == '\'').to_lowercase()).collect()
}

fn base(word: &str) -> &str {
    let name = word.rsplit(['/', '\\']).next().unwrap_or(word);
    name.strip_suffix(".exe").or_else(|| name.strip_suffix(".cmd")).unwrap_or(name)
}

fn destructive(w: &[String]) -> Option<&'static str> {
    let has = |flag: &str| w.iter().skip(1).any(|a| a == flag);
    let short = |c: char| w.iter().skip(1).any(|a| a.starts_with('-') && !a.starts_with("--") && a.contains(c));
    match base(w.first()?) {
        "rm" if (short('r') || has("--recursive")) && (short('f') || has("--force")) => Some("rm -rf deletes without a way back"),
        "del" | "erase" if has("/s") || has("/q") => Some("del /s deletes without a way back"),
        "rd" | "rmdir" if has("/s") => Some("rmdir /s deletes a whole tree"),
        "remove-item" | "ri" if w.iter().any(|a| a.starts_with("-r")) => Some("Remove-Item -Recurse deletes a whole tree"),
        "format" | "mkfs" | "diskpart" | "fdisk" | "dd" | "shutdown" | "reboot" | "halt" | "poweroff" => {
            Some("this command acts on the whole machine")
        }
        "reg" if has("delete") || has("add") => Some("this changes the Windows registry"),
        "git" => match w.get(1).map(String::as_str) {
            Some("reset") if has("--hard") => Some("git reset --hard throws away uncommitted work"),
            Some("clean") => Some("git clean deletes untracked files"),
            Some("push") if has("--force") || has("-f") || has("--force-with-lease") => Some("a forced push rewrites the remote"),
            Some("checkout" | "restore") if has(".") || has("--") => Some("this throws away uncommitted work"),
            _ => None,
        },
        _ => None,
    }
}

fn always_ask(w: &[String]) -> Option<&'static str> {
    let first = base(w.first()?);
    let second = w.get(1).map(String::as_str).unwrap_or("");
    if NETWORK.contains(&first) {
        return Some("this command uses the network");
    }
    if INSTALLERS.contains(&first)
        || (matches!(first, "pip" | "pip3" | "uv" | "poetry" | "conda")
            && matches!(second, "install" | "add" | "pip" | "sync" | "uninstall" | "remove"))
        || (matches!(first, "npm" | "pnpm" | "yarn" | "bun")
            && matches!(second, "install" | "i" | "add" | "ci" | "remove" | "uninstall" | "publish"))
        || (first == "cargo" && matches!(second, "install" | "add" | "publish" | "remove"))
        || (first == "go" && matches!(second, "install" | "get"))
        || (matches!(first, "python" | "python3" | "py") && w.iter().any(|a| a == "pip"))
    {
        return Some("this command installs or removes packages");
    }
    if first == "git" && matches!(second, "push" | "pull" | "fetch" | "clone" | "commit" | "rebase" | "merge") {
        return Some("this changes git history or talks to a remote");
    }
    if matches!(first, "sudo" | "su" | "runas" | "doas") {
        return Some("this command asks for more rights");
    }
    None
}

fn read_only(w: &[String]) -> bool {
    let Some(first) = w.first().map(|f| base(f)) else { return true };
    if first == "git" {
        return w.get(1).is_some_and(|s| GIT_READ_ONLY.contains(&s.as_str()))
            && !w.iter().any(|a| a == "-d" || a == "-m" || a == "--delete");
    }
    if first == "find" {
        return !w.iter().any(|a| matches!(a.as_str(), "-delete" | "-exec" | "-execdir" | "-ok"));
    }
    READ_ONLY.contains(&first)
}

fn test_runner(w: &[String]) -> bool {
    let Some(first) = w.first().map(|f| base(f)) else { return false };
    let second = w.get(1).map(String::as_str).unwrap_or("");
    match first {
        "pytest" | "tox" | "nox" | "jest" | "vitest" | "mocha" | "ruff" | "mypy" | "eslint" | "tsc" => true,
        "python" | "python3" | "py" => w
            .windows(2)
            .any(|p| p[0] == "-m" && matches!(p[1].as_str(), "pytest" | "unittest" | "ruff" | "mypy" | "compileall" | "py_compile")),
        "cargo" => matches!(second, "test" | "check" | "build" | "clippy" | "fmt" | "nextest"),
        "go" => matches!(second, "test" | "build" | "vet"),
        "npm" | "pnpm" | "yarn" | "bun" => {
            second == "test" || (second == "run" && w.get(2).is_some_and(|s| matches!(s.as_str(), "test" | "lint" | "build" | "typecheck")))
        }
        "make" => matches!(second, "test" | "check" | "lint"),
        "dotnet" => matches!(second, "test" | "build"),
        _ => false,
    }
}

/// Split at `|`, `&&`, `||`, `;`, `&` so every command of a chain is judged.
fn segments(cmd: &str) -> Vec<&str> {
    cmd.split(['|', '&', ';', '\n']).map(str::trim).filter(|s| !s.is_empty()).collect()
}

pub fn check_command(mode: Mode, cmd: &str) -> Decision {
    if cmd.trim().is_empty() {
        return Decision::Deny("the command is empty".into());
    }
    let low = cmd.to_lowercase();
    if low.contains(":(){") || low.contains("/dev/sd") || low.contains("/dev/nvme") {
        return Decision::Deny("this command acts on the whole machine".into());
    }
    let chain: Vec<Vec<String>> = segments(cmd).into_iter().map(words).collect();
    for w in &chain {
        if let Some(why) = destructive(w) {
            return Decision::Deny(why.into());
        }
        // A secret named in any command (`cat .env`, `type id_rsa`) is refused like a direct read.
        if w.iter().skip(1).any(|a| is_secret(a.trim_start_matches(['<', '>', '@']))) {
            return Decision::Deny("this command touches a file that holds secrets".into());
        }
    }
    let substitution = cmd.contains('`') || cmd.contains("$(");
    let redirect = cmd.contains('>');
    for w in &chain {
        if let Some(why) = always_ask(w) {
            return Decision::Ask(why.into());
        }
    }
    if substitution {
        return Decision::Ask("the command builds part of itself at run time".into());
    }
    if !redirect && chain.iter().all(|w| read_only(w)) {
        return Decision::Allow;
    }
    match mode {
        Mode::Auto => Decision::Allow,
        Mode::Edit if !redirect && chain.iter().all(|w| read_only(w) || test_runner(w)) => Decision::Allow,
        Mode::Edit => Decision::Ask("edit mode asks before commands other than tests and read-only ones".into()),
        Mode::Read => Decision::Ask("read mode asks before any command that can change something".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn deny(d: Decision) -> bool {
        matches!(d, Decision::Deny(_))
    }
    fn ask(d: Decision) -> bool {
        matches!(d, Decision::Ask(_))
    }

    #[test]
    fn secrets_are_recognised() {
        for p in [".env", ".env.local", "config/.ENV", "deploy/server.pem", "id_rsa", ".ssh/config", "a\\b\\auth.json", ".aws/credentials"]
        {
            assert!(is_secret(p), "{p}");
        }
        for p in [".env.example", "src/env.py", "keys.py", "docs/pem.md", "environment.yml"] {
            assert!(!is_secret(p), "{p}");
        }
    }

    #[test]
    fn test_files_are_recognised() {
        for p in ["tests/test_calc.py", "test_x.py", "pkg/x_test.go", "src/a.test.ts", "tests/common/mod.rs", "conftest.py"] {
            assert!(is_test_file(p), "{p}");
        }
        for p in ["src/calc.py", "contest.py", "latest.py", "testing.md"] {
            assert!(!is_test_file(p), "{p}");
        }
    }

    #[test]
    fn reads_stay_inside_the_project_and_away_from_secrets() {
        assert_eq!(check_read(Some("src/a.py")), Decision::Allow);
        assert!(deny(check_read(None)));
        assert!(deny(check_read(Some(".env"))));
        assert!(deny(check_read(Some(".git/config"))));
    }

    #[test]
    fn writes_follow_the_table() {
        assert_eq!(check_write(Mode::Edit, Some("src/a.py"), true), Decision::Allow);
        assert_eq!(check_write(Mode::Edit, Some("tests/test_new.py"), false), Decision::Allow);
        assert!(ask(check_write(Mode::Edit, Some("tests/test_a.py"), true)));
        assert!(ask(check_write(Mode::Auto, Some("tests/test_a.py"), true)), "auto mode still asks for tests");
        assert!(ask(check_write(Mode::Auto, Some("CAP.md"), false)));
        assert!(deny(check_write(Mode::Read, Some("src/a.py"), true)));
        for mode in [Mode::Read, Mode::Edit, Mode::Auto] {
            assert!(deny(check_write(mode, None, false)));
            assert!(deny(check_write(mode, Some(".env"), false)));
            assert!(deny(check_write(mode, Some(".git/hooks/pre-commit"), false)));
        }
    }

    #[test]
    fn destructive_commands_are_refused_in_every_mode() {
        for cmd in [
            "rm -rf build",
            "rm -fr .",
            "ls && rm -r -f x",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git push --force origin main",
            "del /s /q *",
            "rmdir /s /q src",
            "shutdown /s",
            "cat .env",
            "type config\\.env.local",
            "git checkout -- .",
        ] {
            for mode in [Mode::Read, Mode::Edit, Mode::Auto] {
                assert!(deny(check_command(mode, cmd)), "{cmd} in {mode:?}");
            }
        }
    }

    #[test]
    fn installs_network_and_git_writes_always_ask() {
        for cmd in [
            "pip install requests",
            "python -m pip install -e .",
            "npm i left-pad",
            "curl https://x.y",
            "git push",
            "git commit -m x",
            "cargo add serde",
            "echo `whoami`",
        ] {
            for mode in [Mode::Read, Mode::Edit, Mode::Auto] {
                assert!(ask(check_command(mode, cmd)), "{cmd} in {mode:?}");
            }
        }
    }

    #[test]
    fn read_only_and_test_commands() {
        for cmd in ["ls -la", "git status", "git diff HEAD", "grep -rn foo src | head -5", "cat src/a.py", "find . -name '*.py'"] {
            for mode in [Mode::Read, Mode::Edit, Mode::Auto] {
                assert_eq!(check_command(mode, cmd), Decision::Allow, "{cmd} in {mode:?}");
            }
        }
        for cmd in ["python -m pytest -q", "pytest tests/test_a.py -x", "cargo test", "npm test", "go test ./..."] {
            assert_eq!(check_command(Mode::Edit, cmd), Decision::Allow, "{cmd}");
            assert!(ask(check_command(Mode::Read, cmd)), "{cmd}");
        }
        assert!(ask(check_command(Mode::Edit, "python script.py")));
        assert!(ask(check_command(Mode::Edit, "echo x > out.txt")));
        assert!(ask(check_command(Mode::Edit, "find . -name '*.pyc' -delete")));
        assert_eq!(check_command(Mode::Auto, "python script.py"), Decision::Allow);
        assert!(deny(check_command(Mode::Auto, "  ")));
    }
}
