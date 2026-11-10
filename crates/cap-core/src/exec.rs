//! Running a shell command with a time limit and a bounded, UTF-8 output.
//!
//! A command that is dropped before it ends (time limit, Ctrl-C) takes every process it started with it.

use crate::sandbox::Sandbox;
use std::path::Path;
use std::process::Stdio;
use std::time::{Duration, Instant};
use tokio::io::AsyncReadExt;
use tokio::process::Command;

const HEAD: usize = 2000;
const TAIL: usize = 4000;

#[derive(Clone, Debug)]
pub struct Output {
    /// Exit code; `None` when the command was killed or never started.
    pub code: Option<i32>,
    /// stdout and stderr together, clipped.
    pub text: String,
    pub timed_out: bool,
    pub seconds: f32,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.code == Some(0)
    }

    fn not_started(why: String) -> Output {
        Output { code: None, text: why, timed_out: false, seconds: 0.0 }
    }
}

/// Keep the start and the end of a long output; the end is where test failures are summarised.
pub fn clip(text: &str, head: usize, tail: usize) -> String {
    let n = text.chars().count();
    if n <= head + tail {
        return text.to_string();
    }
    let start: String = text.chars().take(head).collect();
    let end: String = text.chars().skip(n - tail).collect();
    format!("{start}\n[... {} characters left out ...]\n{end}", n - head - tail)
}

fn command(cmd: &str) -> Command {
    #[cfg(windows)]
    {
        // `cmd /S /C "…"` runs the text between the outer quotes as typed.
        // `chcp 65001` makes console programs print UTF-8.
        let mut c = Command::new("cmd");
        c.raw_arg(format!("/S /C \"chcp 65001>nul & {cmd}\""));
        c
    }
    #[cfg(not(windows))]
    {
        let mut c = Command::new("sh");
        c.arg("-c").arg(cmd);
        // Its own process group: the shell and everything it starts can be stopped together.
        c.process_group(0);
        c
    }
}

/// The processes of a running command; dropping it while they run stops them all.
struct Tree(Option<u32>);

impl Tree {
    fn stop(pid: u32) {
        #[cfg(unix)]
        // SAFETY: a signal to the process group this module created for the command.
        unsafe {
            libc::kill(-(pid as libc::pid_t), libc::SIGKILL);
        }
        #[cfg(windows)]
        {
            // `cmd` starts the real program as a child; `taskkill /T` ends the whole tree.
            let _ = std::process::Command::new("taskkill")
                .args(["/T", "/F", "/PID", &pid.to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn();
        }
        #[cfg(not(any(unix, windows)))]
        let _ = pid;
    }

    fn ended(&mut self) {
        self.0 = None;
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        if let Some(pid) = self.0 {
            Tree::stop(pid);
        }
    }
}

async fn kill_tree(child: &mut tokio::process::Child) {
    if let Some(pid) = child.id() {
        #[cfg(windows)]
        {
            let _ = Command::new("taskkill")
                .args(["/T", "/F", "/PID", &pid.to_string()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .await;
        }
        #[cfg(not(windows))]
        Tree::stop(pid);
    }
    let _ = child.kill().await;
}

/// Turn what a command printed into the text the model reads.
fn finish(out: &[u8], err: &[u8], code: Option<i32>, timed_out: bool, timeout: Duration, started: Instant) -> Output {
    let mut text = String::from_utf8_lossy(out).replace("\r\n", "\n");
    let err = String::from_utf8_lossy(err).replace("\r\n", "\n");
    if !err.trim().is_empty() {
        if !text.is_empty() && !text.ends_with('\n') {
            text.push('\n');
        }
        text.push_str(&err);
    }
    if timed_out {
        text.push_str(&format!("\n[stopped: no result within {} s]", timeout.as_secs()));
    }
    Output { code, text: clip(text.trim_end(), HEAD, TAIL), timed_out, seconds: started.elapsed().as_secs_f32() }
}

/// Run `cmd` in `cwd` through the system shell, without isolation.
pub async fn shell(cmd: &str, cwd: &Path, timeout: Duration) -> Output {
    shell_in(&Sandbox::Off, cmd, cwd, timeout).await
}

/// Run `cmd` in `cwd` through the system shell, inside `sandbox` when it is on.
pub async fn shell_in(sandbox: &Sandbox, cmd: &str, cwd: &Path, timeout: Duration) -> Output {
    let started = Instant::now();
    #[cfg(windows)]
    if sandbox.is_on() {
        return contained(cmd, cwd, timeout, started).await;
    }
    let mut c = command(cmd);
    c.current_dir(cwd)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    // The rule set has to stay open until the child has switched it on.
    #[cfg(target_os = "linux")]
    let _rules = match sandbox {
        Sandbox::On(policy) => match crate::sandbox::linux::Ruleset::new(policy) {
            Ok(rules) => {
                let fd = rules.fd();
                // SAFETY: `restrict` makes two raw system calls and allocates nothing on success.
                unsafe { c.pre_exec(move || crate::sandbox::linux::restrict(fd)) };
                Some(rules)
            }
            Err(e) => return Output::not_started(format!("could not start the isolation: {e}")),
        },
        Sandbox::Off => None,
    };
    #[cfg(not(any(windows, target_os = "linux")))]
    let _ = sandbox;

    let mut child = match c.spawn() {
        Ok(child) => child,
        Err(e) => return Output::not_started(format!("could not start the command: {e}")),
    };
    let mut tree = Tree(child.id());
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let read_out = tokio::spawn(async move {
        let mut buf = Vec::new();
        let _ = stdout.read_to_end(&mut buf).await;
        buf
    });
    let read_err = tokio::spawn(async move {
        let mut buf = Vec::new();
        let _ = stderr.read_to_end(&mut buf).await;
        buf
    });

    let (code, timed_out) = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(Ok(status)) => (status.code(), false),
        Ok(Err(_)) => (None, false),
        Err(_) => {
            kill_tree(&mut child).await;
            (None, true)
        }
    };
    tree.ended();
    // After a kill a grandchild can still hold the pipe open; do not wait for it forever.
    let grace = Duration::from_secs(if timed_out { 2 } else { 10 });
    let collect = |h: tokio::task::JoinHandle<Vec<u8>>| async move {
        let abort = h.abort_handle();
        match tokio::time::timeout(grace, h).await {
            Ok(Ok(buf)) => buf,
            _ => {
                abort.abort();
                Vec::new()
            }
        }
    };
    let (out, err) = (collect(read_out).await, collect(read_err).await);
    finish(&out, &err, code, timed_out, timeout, started)
}

/// The Windows container path: the command runs on a blocking thread inside a job object.
#[cfg(windows)]
async fn contained(cmd: &str, cwd: &Path, timeout: Duration, started: Instant) -> Output {
    use crate::sandbox::windows::{Job, run};
    use std::sync::Arc;

    /// Ends every process of the job unless the command ended by itself.
    struct Guard(Arc<Job>, bool);
    impl Drop for Guard {
        fn drop(&mut self) {
            if !self.1 {
                self.0.kill();
            }
        }
    }

    let job = match Job::new() {
        Ok(job) => Arc::new(job),
        Err(e) => return Output::not_started(format!("could not start the isolation: {e}")),
    };
    let mut guard = Guard(job.clone(), false);
    let (cmd, cwd) = (cmd.to_string(), cwd.to_path_buf());
    let result = tokio::task::spawn_blocking(move || run(&cmd, &cwd, timeout, &job)).await;
    guard.1 = true;
    match result {
        Ok(Ok(raw)) => finish(&raw.out, &[], raw.code, raw.timed_out, timeout, started),
        Ok(Err(e)) => Output::not_started(format!("could not start the command in the container: {e}")),
        Err(e) => Output::not_started(format!("could not start the command: {e}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clip_keeps_both_ends() {
        assert_eq!(clip("short", 10, 10), "short");
        let long: String = (0..1000).map(|i| char::from(b'a' + (i % 26) as u8)).collect();
        let c = clip(&long, 10, 20);
        assert!(c.starts_with(&long[..10]) && c.ends_with(&long[980..]));
        assert!(c.contains("970 characters left out"));
    }

    #[tokio::test]
    async fn output_and_exit_code_are_captured() {
        let dir = std::env::temp_dir();
        let ok = shell("echo hello", &dir, Duration::from_secs(20)).await;
        assert!(ok.ok(), "{ok:?}");
        assert_eq!(ok.text.trim(), "hello");
        let bad = shell("exit 3", &dir, Duration::from_secs(20)).await;
        assert_eq!(bad.code, Some(3));
    }

    #[tokio::test]
    async fn a_command_that_hangs_is_stopped() {
        let cmd = if cfg!(windows) { "ping -n 30 127.0.0.1 >nul" } else { "sleep 30" };
        let out = shell(cmd, &std::env::temp_dir(), Duration::from_millis(700)).await;
        assert!(out.timed_out && out.code.is_none());
        assert!(out.seconds < 15.0, "{out:?}");
        assert!(out.text.contains("stopped"));
    }

    #[tokio::test]
    async fn a_dropped_command_takes_its_processes_with_it() {
        let dir = crate::testing::TempDir::new("drop");
        // The marker appears only if the command lives past its wait.
        let cmd = if cfg!(windows) { "ping -n 4 127.0.0.1 >nul & echo late> late.txt" } else { "sleep 2; echo late > late.txt" };
        let run = shell(cmd, dir.path(), Duration::from_secs(30));
        assert!(tokio::time::timeout(Duration::from_millis(400), run).await.is_err());
        tokio::time::sleep(Duration::from_millis(4500)).await;
        assert!(!dir.path().join("late.txt").exists(), "the command went on after it was dropped");
    }
}
