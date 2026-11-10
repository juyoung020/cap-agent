//! Keeping commands inside the project folder with the operating system's own isolation.
//!
//! Linux uses Landlock: a command reads the system and the toolchains and writes only to the
//! project, the temp folders and the build caches. Windows uses an AppContainer: a command opens
//! only folders that were handed to the container (`cap sandbox setup`) and has no network.

use std::path::{Path, PathBuf};
use std::time::Duration;

#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(windows)]
pub mod windows;

/// What a command may touch besides the system itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Policy {
    /// The project folder: read and write.
    pub root: PathBuf,
    /// More folders or files to read.
    pub read: Vec<PathBuf>,
    /// More folders or files to read and write.
    pub write: Vec<PathBuf>,
}

impl Policy {
    pub fn new(root: &Path) -> Policy {
        Policy { root: root.to_path_buf(), read: Vec::new(), write: Vec::new() }
    }

    /// The project plus the folders the settings name and the build folder `CARGO_TARGET_DIR` points to.
    pub fn with_extras(root: &Path, read: &[PathBuf], write: &[PathBuf]) -> Policy {
        let mut policy = Policy { root: root.to_path_buf(), read: read.to_vec(), write: write.to_vec() };
        if let Some(target) = std::env::var_os("CARGO_TARGET_DIR").filter(|v| !v.is_empty()) {
            policy.write.push(PathBuf::from(target));
        }
        policy
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Sandbox {
    #[default]
    Off,
    On(Policy),
}

/// The one `Sandbox::Off` to borrow where no isolation is set.
pub static OFF: Sandbox = Sandbox::Off;

impl Sandbox {
    pub fn is_on(&self) -> bool {
        matches!(self, Sandbox::On(_))
    }

    /// The isolation in use: `off`, `landlock` or `appcontainer`.
    pub fn name(&self) -> &'static str {
        match self {
            Sandbox::Off => "off",
            Sandbox::On(_) => KIND,
        }
    }
}

#[cfg(target_os = "linux")]
const KIND: &str = "landlock";
#[cfg(windows)]
const KIND: &str = "appcontainer";
#[cfg(not(any(target_os = "linux", windows)))]
const KIND: &str = "off";

/// The `sandbox` setting.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Want {
    /// Isolate when this computer and this folder are ready for it.
    #[default]
    Auto,
    /// Isolate or refuse to start.
    On,
    Off,
}

impl Want {
    pub fn parse(text: &str) -> Option<Want> {
        match text.trim().to_ascii_lowercase().as_str() {
            "auto" => Some(Want::Auto),
            "on" | "true" | "yes" => Some(Want::On),
            "off" | "false" | "no" | "none" => Some(Want::Off),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Want::Auto => "auto",
            Want::On => "on",
            Want::Off => "off",
        }
    }
}

/// Why the isolation cannot be used here, before trying a command in it.
fn obstacle(policy: &Policy, want: Want) -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        let _ = (policy, want);
        (!linux::available()).then(|| "this kernel has no Landlock".to_string())
    }
    #[cfg(windows)]
    {
        (want == Want::Auto && !windows::is_set_up(&policy.root))
            .then(|| "this folder is not set up for it (run `cap sandbox setup`)".to_string())
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (policy, want);
        Some("cap has no isolation for this operating system".to_string())
    }
}

/// Pick the isolation for this run and check that a command really runs in it.
/// `Ok` carries a notice when `auto` fell back to no isolation; `Err` is `on` that cannot be had.
pub async fn choose(want: Want, policy: Policy) -> Result<(Sandbox, Option<String>), String> {
    if want == Want::Off {
        return Ok((Sandbox::Off, None));
    }
    let problem = match obstacle(&policy, want) {
        Some(why) => Some(why),
        None => {
            let root = policy.root.clone();
            let sandbox = Sandbox::On(policy);
            let out = crate::exec::shell_in(&sandbox, "echo ok", &root, Duration::from_secs(20)).await;
            if out.ok() && out.text.contains("ok") {
                return Ok((sandbox, None));
            }
            Some(format!("a test command did not run in it ({})", out.text.lines().last().unwrap_or("no output").trim()))
        }
    };
    let why = problem.unwrap_or_default();
    match want {
        Want::On => Err(format!("isolation is not available: {why}")),
        _ => Ok((Sandbox::Off, Some(format!("commands run without isolation: {why}")))),
    }
}

/// Hand the project folder (and tool folders to read) to the isolation. Windows only has work to do.
pub fn setup(root: &Path, tools: &[PathBuf]) -> Result<String, String> {
    #[cfg(windows)]
    {
        windows::setup(root, tools)
    }
    #[cfg(not(windows))]
    {
        let _ = (root, tools);
        Ok("Nothing to set up: on this system the isolation works without changes to the folder.".to_string())
    }
}

/// Take back what `setup` gave.
pub fn undo(root: &Path) -> Result<String, String> {
    #[cfg(windows)]
    {
        windows::undo(root)
    }
    #[cfg(not(windows))]
    {
        let _ = root;
        Ok("Nothing to undo: on this system the isolation makes no changes to the folder.".to_string())
    }
}

/// One paragraph on what the isolation can do here.
pub fn status(root: &Path) -> String {
    #[cfg(target_os = "linux")]
    {
        let _ = root;
        if linux::available() {
            "landlock: available. Commands read the system and write only to the project, temp folders and build caches.".to_string()
        } else {
            "landlock: this kernel does not have it. Commands run without isolation.".to_string()
        }
    }
    #[cfg(windows)]
    {
        windows::status(root)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = root;
        "cap has no isolation for this operating system. Commands run without isolation.".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_setting_is_read_and_off_needs_no_check() {
        assert_eq!(Want::parse("Auto"), Some(Want::Auto));
        assert_eq!(Want::parse("on"), Some(Want::On));
        assert_eq!(Want::parse("none"), Some(Want::Off));
        assert_eq!(Want::parse("maybe"), None);
        assert_eq!(Sandbox::Off.name(), "off");
        assert!(!Sandbox::Off.is_on());
        let p = Policy::new(Path::new("/x"));
        assert!(p.read.is_empty() && p.write.is_empty());
    }
}
