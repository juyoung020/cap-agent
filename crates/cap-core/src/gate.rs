//! The test gate: a "done" claim counts only after the project's own tests pass.

use crate::exec;
use crate::sandbox::Sandbox;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::Path;
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    Pass,
    Fail {
        /// The end of the test output, shown to the model.
        tail: String,
        /// The same for two runs that fail the same way.
        signature: u64,
    },
    /// The command ran and found no tests to run.
    NoTests,
}

type CustomGate = Box<dyn Fn(&Path) -> Verdict + Send + Sync>;

pub enum Gate {
    /// The project has no tests the agent knows how to run.
    None,
    Command {
        cmd: String,
        timeout: Duration,
    },
    /// A check supplied by the caller (used in tests).
    Custom(CustomGate),
}

impl Gate {
    pub fn command(cmd: impl Into<String>) -> Self {
        Gate::Command { cmd: cmd.into(), timeout: Duration::from_secs(300) }
    }

    /// The command line, for display.
    pub fn describe(&self) -> Option<&str> {
        match self {
            Gate::Command { cmd, .. } => Some(cmd),
            Gate::Custom(_) => Some("(custom check)"),
            Gate::None => None,
        }
    }

    pub async fn run(&self, root: &Path) -> Verdict {
        self.run_in(root, &Sandbox::Off).await
    }

    /// Run the check; a test command runs inside `sandbox` when it is on.
    pub async fn run_in(&self, root: &Path, sandbox: &Sandbox) -> Verdict {
        match self {
            Gate::None => Verdict::NoTests,
            Gate::Custom(check) => check(root),
            Gate::Command { cmd, timeout } => {
                let out = exec::shell_in(sandbox, cmd, root, *timeout).await;
                judge(cmd, out.code, &out.text)
            }
        }
    }
}

/// Turn an exit code and output into a verdict.
pub fn judge(cmd: &str, code: Option<i32>, output: &str) -> Verdict {
    let low = output.to_lowercase();
    let is_pytest = cmd.contains("pytest");
    match code {
        Some(0) if is_pytest && low.contains("no tests ran") => Verdict::NoTests,
        Some(0) if low.contains("running 0 tests") && only_empty_cargo_runs(&low) => Verdict::NoTests,
        Some(0) => Verdict::Pass,
        // pytest exits with 5 when it collected nothing.
        Some(5) if is_pytest => Verdict::NoTests,
        _ => Verdict::Fail { tail: tail(output, 1800), signature: signature(output) },
    }
}

fn only_empty_cargo_runs(low: &str) -> bool {
    low.lines().filter(|l| l.starts_with("running ")).all(|l| l.starts_with("running 0 tests"))
}

fn tail(text: &str, max: usize) -> String {
    let n = text.chars().count();
    if n <= max {
        return text.to_string();
    }
    let cut: String = text.chars().skip(n - max).collect();
    // Start at a line boundary.
    cut.split_once('\n').map_or(cut.clone(), |(_, rest)| rest.to_string())
}

/// A hash of the lines that name failures, with numbers removed so timings and line numbers
/// do not make two runs of the same failure look different.
pub fn signature(output: &str) -> u64 {
    let marks = ["fail", "error", "panicked", "assert", "expected", "traceback"];
    let mut lines: Vec<String> = output
        .lines()
        .filter(|l| {
            let low = l.to_lowercase();
            marks.iter().any(|m| low.contains(m))
        })
        .map(|l| l.chars().map(|c| if c.is_ascii_digit() { '#' } else { c }).collect::<String>().trim().to_string())
        .collect();
    lines.sort();
    lines.dedup();
    let mut h = DefaultHasher::new();
    if lines.is_empty() {
        output.trim().hash(&mut h);
    } else {
        lines.hash(&mut h);
    }
    h.finish()
}

fn has_python_tests(root: &Path) -> bool {
    let is_test = |name: &str| name.ends_with(".py") && (name.starts_with("test_") || name.ends_with("_test.py"));
    let scan = |dir: &Path| std::fs::read_dir(dir).is_ok_and(|rd| rd.flatten().any(|e| e.file_name().to_str().is_some_and(is_test)));
    scan(root) || scan(&root.join("tests")) || scan(&root.join("test"))
}

fn python(root: &Path) -> String {
    for rel in [".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"] {
        if root.join(rel).is_file() {
            let native = rel.replace('/', std::path::MAIN_SEPARATOR_STR);
            return if cfg!(windows) { native } else { format!("./{native}") };
        }
    }
    // Linux and macOS ship the interpreter as `python3`; Windows installs it as `python`.
    if cfg!(windows) { "python" } else { "python3" }.to_string()
}

/// The test command a project of this shape uses, if one can be told from its files.
pub fn detect(root: &Path) -> Option<String> {
    let has = |name: &str| root.join(name).is_file();
    if has("Cargo.toml") {
        return Some("cargo test -q".into());
    }
    if has("pyproject.toml") || has("pytest.ini") || has("setup.py") || has("setup.cfg") || has("tox.ini") || has_python_tests(root) {
        return Some(format!("{} -m pytest -q -p no:cacheprovider", python(root)));
    }
    if has("package.json") {
        let text = std::fs::read_to_string(root.join("package.json")).unwrap_or_default();
        let json: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
        let script = json["scripts"]["test"].as_str().unwrap_or("");
        if !script.is_empty() && !script.contains("no test specified") {
            return Some("npm test --silent".into());
        }
    }
    if has("go.mod") {
        return Some("go test ./...".into());
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_codes_become_verdicts() {
        assert_eq!(judge("python -m pytest -q", Some(0), "3 passed in 0.02s"), Verdict::Pass);
        assert_eq!(judge("python -m pytest -q", Some(5), "no tests ran in 0.01s"), Verdict::NoTests);
        assert_eq!(judge("cargo test -q", Some(0), "running 0 tests\ntest result: ok. 0 passed"), Verdict::NoTests);
        assert_eq!(judge("cargo test -q", Some(0), "running 0 tests\n\nrunning 4 tests\ntest result: ok. 4 passed"), Verdict::Pass);
        assert!(matches!(judge("python -m pytest -q", Some(1), "FAILED tests/test_a.py::test_x"), Verdict::Fail { .. }));
        assert!(matches!(judge("npm test", None, "[stopped: no result within 300 s]"), Verdict::Fail { .. }));
    }

    #[test]
    fn the_same_failure_has_the_same_signature() {
        let a = "tests/test_a.py:12: AssertionError\nFAILED tests/test_a.py::test_add - assert 3 == 5\n1 failed in 0.03s";
        let b = "tests/test_a.py:14: AssertionError\nFAILED tests/test_a.py::test_add - assert 3 == 5\n1 failed in 0.11s";
        let c = "FAILED tests/test_a.py::test_sub - assert 1 == 2\n1 failed in 0.03s";
        assert_eq!(signature(a), signature(b));
        assert_ne!(signature(a), signature(c));
    }

    #[test]
    fn long_output_keeps_its_end() {
        let text = format!("{}\nFAILED last line", "line of noise\n".repeat(500));
        let t = tail(&text, 300);
        assert!(t.ends_with("FAILED last line") && t.chars().count() <= 300);
        assert!(t.starts_with("line of noise"));
    }

    #[test]
    fn the_test_command_is_detected_from_project_files() {
        let dir = std::env::temp_dir().join(format!("cap-gate-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("tests")).unwrap();
        assert_eq!(detect(&dir), None);
        std::fs::write(dir.join("tests/test_a.py"), "def test_a():\n    assert True\n").unwrap();
        let python = if cfg!(windows) { "python" } else { "python3" };
        assert_eq!(detect(&dir), Some(format!("{python} -m pytest -q -p no:cacheprovider")));
        std::fs::write(dir.join("Cargo.toml"), "[package]\n").unwrap();
        assert_eq!(detect(&dir).as_deref(), Some("cargo test -q"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[tokio::test]
    async fn a_command_gate_passes_and_fails_by_exit_code() {
        let dir = std::env::temp_dir();
        assert_eq!(Gate::command("echo ok").run(&dir).await, Verdict::Pass);
        assert!(matches!(Gate::command("echo boom failed & exit 1").run(&dir).await, Verdict::Fail { .. }));
        assert_eq!(Gate::None.run(&dir).await, Verdict::NoTests);
        assert_eq!(Gate::Custom(Box::new(|_| Verdict::Pass)).run(&dir).await, Verdict::Pass);
    }
}
