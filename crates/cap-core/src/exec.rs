//! Running a shell command with a time limit and a bounded, UTF-8 output.

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
        c
    }
}

async fn kill_tree(child: &mut tokio::process::Child) {
    #[cfg(windows)]
    if let Some(pid) = child.id() {
        // `cmd` starts the real program as a child; kill the whole tree.
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
    }
    let _ = child.kill().await;
}

/// Run `cmd` in `cwd` through the system shell.
pub async fn shell(cmd: &str, cwd: &Path, timeout: Duration) -> Output {
    let started = Instant::now();
    let mut c = command(cmd);
    c.current_dir(cwd)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = match c.spawn() {
        Ok(child) => child,
        Err(e) => {
            return Output { code: None, text: format!("could not start the command: {e}"), timed_out: false, seconds: 0.0 };
        }
    };
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
    let mut text = String::from_utf8_lossy(&out).replace("\r\n", "\n");
    let err = String::from_utf8_lossy(&err).replace("\r\n", "\n");
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
}
