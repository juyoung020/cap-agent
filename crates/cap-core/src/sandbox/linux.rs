//! Landlock (Linux 5.13 and later): a process gives up access to everything outside a list of paths.
//!
//! The list is built in the parent. The child only switches it on between `fork` and `exec`,
//! with two raw system calls.

use super::Policy;
use std::ffi::CString;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

const EXECUTE: u64 = 1 << 0;
const WRITE_FILE: u64 = 1 << 1;
const READ_FILE: u64 = 1 << 2;
const READ_DIR: u64 = 1 << 3;
const REMOVE_DIR: u64 = 1 << 4;
const REMOVE_FILE: u64 = 1 << 5;
const MAKE_DIR: u64 = 1 << 7;
const MAKE_REG: u64 = 1 << 8;
const MAKE_SOCK: u64 = 1 << 9;
const MAKE_FIFO: u64 = 1 << 10;
const MAKE_SYM: u64 = 1 << 12;
const REFER: u64 = 1 << 13;
const TRUNCATE: u64 = 1 << 14;
const IOCTL_DEV: u64 = 1 << 15;

const READ: u64 = EXECUTE | READ_FILE | READ_DIR;
const WRITE: u64 = READ | WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_SYM | REFER | TRUNCATE;
const DEVICES: u64 = READ_FILE | WRITE_FILE | READ_DIR | TRUNCATE | IOCTL_DEV;
/// The rights a rule on a single file may carry.
const FILE_RIGHTS: u64 = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV;

const RULE_PATH_BENEATH: libc::c_long = 1;
const CREATE_RULESET_VERSION: libc::c_long = 1;

/// Top-level folders that hold people's files, not the system.
const PRIVATE_TOP: &[&str] = &["home", "root", "Users", "mnt", "media", "lost+found", "dev", "tmp"];
const HOME_READ: &[&str] = &[
    ".cargo",
    ".rustup",
    ".local",
    ".nvm",
    ".pyenv",
    ".asdf",
    "go",
    ".bun",
    ".deno",
    ".volta",
    ".sdkman",
    ".gitconfig",
    ".config/git",
    ".npmrc",
];
const HOME_WRITE: &[&str] = &[
    ".cache",
    ".cargo/registry",
    ".cargo/git",
    ".cargo/.package-cache",
    ".cargo/.package-cache-mutate",
    ".cargo/.global-cache",
    ".npm",
    "go/pkg",
    ".gradle",
    ".m2",
];

/// The Landlock version of this kernel; below 1 there is none.
fn abi() -> i64 {
    // SAFETY: a version query takes no pointer.
    unsafe { libc::syscall(libc::SYS_landlock_create_ruleset, 0 as libc::c_long, 0 as libc::c_long, CREATE_RULESET_VERSION) as i64 }
}

pub fn available() -> bool {
    abi() >= 1
}

/// Every right this kernel can take away; rights it does not know stay as they are.
fn handled(abi: i64) -> u64 {
    let mut all = (1 << 13) - 1;
    if abi >= 2 {
        all |= REFER;
    }
    if abi >= 3 {
        all |= TRUNCATE;
    }
    if abi >= 5 {
        all |= IOCTL_DEV;
    }
    all
}

#[repr(C, packed)]
struct PathRule {
    allowed_access: u64,
    parent_fd: i32,
}

fn rules(policy: &Policy) -> Vec<(PathBuf, u64)> {
    let mut out: Vec<(PathBuf, u64)> = Vec::new();
    if let Ok(entries) = std::fs::read_dir("/") {
        for entry in entries.flatten() {
            if !PRIVATE_TOP.iter().any(|p| entry.file_name() == *p) {
                out.push((entry.path(), READ));
            }
        }
    }
    out.push(("/dev".into(), DEVICES));
    out.push(("/tmp".into(), WRITE));
    out.push(("/var/tmp".into(), WRITE));
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from).filter(|h| h.is_absolute()) {
        out.extend(HOME_READ.iter().map(|d| (home.join(d), READ)));
        out.extend(HOME_WRITE.iter().map(|d| (home.join(d), WRITE)));
    }
    out.extend(policy.read.iter().map(|p| (p.clone(), READ)));
    out.extend(policy.write.iter().map(|p| (p.clone(), WRITE)));
    out.push((policy.root.clone(), WRITE));
    out
}

/// A list of paths and rights, ready to be switched on in a child process.
pub struct Ruleset(OwnedFd);

impl Ruleset {
    pub fn new(policy: &Policy) -> io::Result<Ruleset> {
        let abi = abi();
        if abi < 1 {
            return Err(io::Error::new(io::ErrorKind::Unsupported, "this kernel has no Landlock"));
        }
        let handled = handled(abi);
        // The first field of the kernel's attribute structure; its size tells the kernel that is all there is.
        let attr: u64 = handled;
        // SAFETY: `attr` lives across the call and its size is passed with it.
        let fd = unsafe { libc::syscall(libc::SYS_landlock_create_ruleset, &raw const attr, 8 as libc::c_long, 0 as libc::c_long) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: the kernel just returned this descriptor to us.
        let set = Ruleset(unsafe { OwnedFd::from_raw_fd(fd as RawFd) });
        for (path, rights) in rules(policy) {
            set.allow(&path, rights & handled);
        }
        Ok(set)
    }

    /// A path that does not exist gets no rule.
    fn allow(&self, path: &Path, rights: u64) {
        let Ok(c_path) = CString::new(path.as_os_str().as_bytes()) else { return };
        // SAFETY: `c_path` is a NUL-terminated string.
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_PATH | libc::O_CLOEXEC) };
        if fd < 0 {
            return;
        }
        // SAFETY: `open` just returned this descriptor to us.
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        let is_dir = std::fs::metadata(path).map(|m| m.is_dir()).unwrap_or(false);
        let rights = if is_dir { rights } else { rights & FILE_RIGHTS };
        if rights == 0 {
            return;
        }
        let rule = PathRule { allowed_access: rights, parent_fd: fd.as_raw_fd() };
        // SAFETY: `rule` lives across the call and has the layout the kernel expects.
        unsafe {
            libc::syscall(
                libc::SYS_landlock_add_rule,
                self.0.as_raw_fd() as libc::c_long,
                RULE_PATH_BENEATH,
                &raw const rule,
                0 as libc::c_long,
            );
        }
    }

    pub fn fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}

/// Switch the rule set on for this process and every process it starts.
/// Called in the child between `fork` and `exec`: raw system calls only.
pub fn restrict(fd: RawFd) -> io::Result<()> {
    // SAFETY: both calls take plain numbers.
    unsafe {
        if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1 as libc::c_ulong, 0 as libc::c_ulong, 0 as libc::c_ulong, 0 as libc::c_ulong) != 0 {
            return Err(io::Error::last_os_error());
        }
        if libc::syscall(libc::SYS_landlock_restrict_self, fd as libc::c_long, 0 as libc::c_long) != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}
