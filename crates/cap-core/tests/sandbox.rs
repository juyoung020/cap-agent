//! The operating system's isolation, tried with real commands in a folder this test creates.
//!
//! Where the computer has no isolation to give (an old kernel, a locked-down account) the tests
//! say so and pass; with `CAP_REQUIRE_SANDBOX=1` they fail instead.

use cap_core::exec::shell_in;
use cap_core::sandbox::{self, Policy, Sandbox, Want};
use std::path::{Path, PathBuf};
use std::time::Duration;

const WAIT: Duration = Duration::from_secs(30);

/// Setting a folder up changes one shared record on Windows: one test at a time.
static ONE_AT_A_TIME: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// A project folder, and next to it a file the project's commands have no business with.
struct Site {
    base: PathBuf,
    root: PathBuf,
}

impl Site {
    fn new(tag: &str) -> Site {
        let base = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("sbx-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("proj");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(base.join("outside.txt"), "secret\n").unwrap();
        std::fs::write(root.join("inside.txt"), "hello\n").unwrap();
        Site { base, root }
    }

    /// The isolation for this project, or `None` where this computer has none to give.
    async fn isolation(&self) -> Option<Sandbox> {
        let ready = match sandbox::setup(&self.root, &[]) {
            Ok(_) => sandbox::choose(Want::On, Policy::new(&self.root)).await.map(|(sandbox, _)| sandbox),
            Err(why) => Err(why),
        };
        match ready {
            Ok(sandbox) if sandbox.is_on() => Some(sandbox),
            Ok(_) => unreachable!("`on` gives the isolation or an error"),
            Err(why) => {
                assert!(std::env::var_os("CAP_REQUIRE_SANDBOX").is_none(), "the isolation is required here: {why}");
                eprintln!("not checked on this computer: {why}");
                None
            }
        }
    }
}

impl Drop for Site {
    fn drop(&mut self) {
        let _ = sandbox::undo(&self.root);
        let _ = std::fs::remove_dir_all(&self.base);
    }
}

/// The same command in the spelling of this system's shell.
fn sh(windows: &'static str, unix: &'static str) -> &'static str {
    if cfg!(windows) { windows } else { unix }
}

#[tokio::test]
async fn a_command_works_inside_the_project_and_nowhere_else() {
    let _turn = ONE_AT_A_TIME.lock().await;
    let site = Site::new("files");
    let Some(sandbox) = site.isolation().await else { return };
    assert_eq!(sandbox.name(), sh("appcontainer", "landlock"));

    let read = shell_in(&sandbox, sh("type inside.txt", "cat inside.txt"), &site.root, WAIT).await;
    assert!(read.ok() && read.text.contains("hello"), "{read:?}");
    let write = shell_in(&sandbox, "echo made> made.txt", &site.root, WAIT).await;
    assert!(write.ok(), "{write:?}");
    assert_eq!(std::fs::read_to_string(site.root.join("made.txt")).unwrap().trim(), "made");

    let read_out = sh(r"type ..\outside.txt", "cat ../outside.txt");
    let peek = shell_in(&sandbox, read_out, &site.root, WAIT).await;
    assert!(!peek.ok() && !peek.text.contains("secret"), "{peek:?}");
    let write_out = sh(r"echo x> ..\escaped.txt", "echo x > ../escaped.txt");
    let escape = shell_in(&sandbox, write_out, &site.root, WAIT).await;
    assert!(!escape.ok(), "{escape:?}");
    assert!(!site.base.join("escaped.txt").exists());
    let overwrite = shell_in(&sandbox, sh(r"echo x> ..\outside.txt", "echo x > ../outside.txt"), &site.root, WAIT).await;
    assert!(!overwrite.ok(), "{overwrite:?}");
    assert_eq!(std::fs::read_to_string(site.base.join("outside.txt")).unwrap(), "secret\n");

    // The folder itself allows all of it: the refusals above came from the isolation.
    let free = shell_in(&Sandbox::Off, read_out, &site.root, WAIT).await;
    assert!(free.ok() && free.text.contains("secret"), "{free:?}");
}

#[tokio::test]
async fn a_command_that_hangs_in_the_isolation_is_stopped() {
    let _turn = ONE_AT_A_TIME.lock().await;
    let site = Site::new("hang");
    let Some(sandbox) = site.isolation().await else { return };
    // The marker appears only if the command lives past its time.
    let cmd = sh("powershell -NoProfile -Command Start-Sleep 3 & echo late> late.txt", "sleep 3; echo late > late.txt");
    let out = shell_in(&sandbox, cmd, &site.root, Duration::from_millis(900)).await;
    assert!(out.timed_out && out.code.is_none(), "{out:?}");
    assert!(out.seconds < 15.0 && out.text.contains("stopped"), "{out:?}");
    tokio::time::sleep(Duration::from_millis(5500)).await;
    assert!(!site.root.join("late.txt").exists(), "the command went on after its time ran out");
}

#[tokio::test]
async fn the_setting_decides_whether_commands_are_isolated() {
    let _turn = ONE_AT_A_TIME.lock().await;
    let site = Site::new("setting");
    let (off, notice) = sandbox::choose(Want::Off, Policy::new(&site.root)).await.unwrap();
    assert!(!off.is_on() && notice.is_none());

    // `auto` never fails: it isolates where it can and says so where it cannot.
    let (auto, notice) = sandbox::choose(Want::Auto, Policy::new(&site.root)).await.unwrap();
    assert_eq!(auto.is_on(), notice.is_none());
    if cfg!(windows) {
        // A folder nobody set up is left alone.
        assert!(!auto.is_on());
        assert!(notice.unwrap().contains("cap sandbox setup"));
        assert!(sandbox::status(&site.root).contains("is not set up"));
    }
    if let Some(on) = site.isolation().await {
        let (auto, notice) = sandbox::choose(Want::Auto, Policy::new(&site.root)).await.unwrap();
        assert!(auto.is_on() && notice.is_none());
        assert_eq!(auto, on);
        assert!(!sandbox::status(&site.root).contains("is not set up"));
    }
}

#[cfg(windows)]
#[tokio::test]
async fn a_command_in_the_container_reaches_no_network() {
    let _turn = ONE_AT_A_TIME.lock().await;
    let site = Site::new("net");
    let Some(sandbox) = site.isolation().await else { return };
    let ping = "ping -n 1 127.0.0.1";
    let inside = shell_in(&sandbox, ping, &site.root, WAIT).await;
    assert!(!inside.ok() && !inside.text.contains("TTL="), "{inside:?}");
    let free = shell_in(&Sandbox::Off, ping, &site.root, WAIT).await;
    assert!(free.ok() && free.text.contains("TTL="), "{free:?}");
}
