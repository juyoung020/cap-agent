//! AppContainer (Windows 8 and later): a process that opens only what its container was given
//! and has no network.
//!
//! A folder is handed to the container once (`cap sandbox setup`), which changes the folder's
//! access list; `cap sandbox undo` takes it back. What was handed over is recorded in
//! `%LOCALAPPDATA%\cap\sandbox.json`.

use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::time::Duration;
use windows_sys::Win32::Foundation::{
    CloseHandle, GENERIC_READ, HANDLE, HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE, LocalFree, SetHandleInformation, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::Isolation::{CreateAppContainerProfile, DeriveAppContainerSidFromAppContainerName};
use windows_sys::Win32::Security::{FreeSid, PSID, SECURITY_ATTRIBUTES, SECURITY_CAPABILITIES};
use windows_sys::Win32::Storage::FileSystem::{CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING, ReadFile};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectExtendedLimitInformation, SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, CreateProcessW, DeleteProcThreadAttributeList,
    EXTENDED_STARTUPINFO_PRESENT, GetExitCodeProcess, InitializeProcThreadAttributeList, LPPROC_THREAD_ATTRIBUTE_LIST,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, PROCESS_INFORMATION, ResumeThread,
    STARTF_USESTDHANDLES, STARTUPINFOEXW, UpdateProcThreadAttribute, WaitForSingleObject,
};

/// The container's name; its identity (SID) follows from the name.
pub const PROFILE: &str = "cap.sandbox";

fn wide(s: impl AsRef<OsStr>) -> Vec<u16> {
    s.as_ref().encode_wide().chain(Some(0)).collect()
}

/// `\\?\C:\x` reads `C:\x`; `cmd.exe` and `icacls` take the short form.
fn plain(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC") => PathBuf::from(rest),
        _ => path.to_path_buf(),
    }
}

struct Handle(HANDLE);
// SAFETY: a kernel handle is a number any thread may use.
unsafe impl Send for Handle {}
// SAFETY: the calls made on a shared handle here (terminate, wait) are thread-safe.
unsafe impl Sync for Handle {}

impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            // SAFETY: the handle is open and owned by this value.
            unsafe { CloseHandle(self.0) };
        }
    }
}

struct Sid(PSID);

impl Drop for Sid {
    fn drop(&mut self) {
        // SAFETY: the SID came from the profile functions, which ask for `FreeSid`.
        unsafe { FreeSid(self.0) };
    }
}

/// The container's SID; the profile is created the first time.
fn container_sid() -> io::Result<Sid> {
    let name = wide(PROFILE);
    let mut sid: PSID = null_mut();
    // SAFETY: the strings are NUL-terminated and `sid` receives an allocation we free.
    let created = unsafe { CreateAppContainerProfile(name.as_ptr(), name.as_ptr(), name.as_ptr(), null(), 0, &mut sid) };
    if created < 0 {
        // The profile exists already: its SID is derived from the name.
        // SAFETY: as above.
        let derived = unsafe { DeriveAppContainerSidFromAppContainerName(name.as_ptr(), &mut sid) };
        if derived < 0 {
            return Err(io::Error::other(format!("Windows gave no AppContainer profile (0x{derived:08x})")));
        }
    }
    Ok(Sid(sid))
}

/// The container's SID as text (`S-1-15-2-…`), the form `icacls` takes.
pub fn sid_string() -> io::Result<String> {
    let sid = container_sid()?;
    let mut text: *mut u16 = null_mut();
    // SAFETY: `sid` is valid and `text` receives a string we free with `LocalFree`.
    if unsafe { ConvertSidToStringSidW(sid.0, &mut text) } == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: `text` is a NUL-terminated string from the call above.
    let out = unsafe {
        let len = (0..).take_while(|&i| *text.add(i) != 0).count();
        let s = String::from_utf16_lossy(std::slice::from_raw_parts(text, len));
        LocalFree(text.cast());
        s
    };
    Ok(out)
}

/// A job object: closing it, or `kill`, ends every process started in it.
pub struct Job(Handle);

impl Job {
    pub fn new() -> io::Result<Job> {
        // SAFETY: no attributes, no name.
        let handle = unsafe { CreateJobObjectW(null(), null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = Job(Handle(handle));
        // SAFETY: an all-zero limit structure is valid; the size passed is its own.
        unsafe {
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(handle, JobObjectExtendedLimitInformation, (&raw const info).cast(), size_of_val(&info) as u32);
        }
        Ok(job)
    }

    pub fn kill(&self) {
        // SAFETY: the job handle is open.
        unsafe { TerminateJobObject(self.0.0, 1) };
    }
}

/// What a finished command left: exit code, raw output, and whether the time ran out.
pub struct Raw {
    pub code: Option<i32>,
    pub out: Vec<u8>,
    pub timed_out: bool,
}

struct Attributes(LPPROC_THREAD_ATTRIBUTE_LIST);

impl Drop for Attributes {
    fn drop(&mut self) {
        // SAFETY: the list was initialised before this guard was made.
        unsafe { DeleteProcThreadAttributeList(self.0) };
    }
}

fn container_dir() -> PathBuf {
    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_default();
    local.join("Packages").join(PROFILE).join("AC")
}

/// The environment of a contained command: the caller's, with the temp folder inside the container
/// and package tools told to stay offline.
fn environment() -> Vec<u16> {
    let temp = container_dir().join("Temp");
    let _ = std::fs::create_dir_all(&temp);
    // `nul` cannot be opened inside a container on every computer; this file stands in for it.
    let sink = temp.join("cap-devnull");
    if std::fs::metadata(&sink).is_ok_and(|m| m.len() > 1 << 20) {
        let _ = std::fs::remove_file(&sink);
    }
    let mut vars: BTreeMap<String, (OsString, OsString)> = BTreeMap::new();
    for (key, value) in std::env::vars_os() {
        vars.insert(key.to_string_lossy().to_uppercase(), (key, value));
    }
    let fixed: [(&str, &OsStr); 8] = [
        ("TEMP", temp.as_os_str()),
        ("TMP", temp.as_os_str()),
        ("PYTHONUTF8", OsStr::new("1")),
        ("PYTHONIOENCODING", OsStr::new("utf-8")),
        ("PYTHONDONTWRITEBYTECODE", OsStr::new("1")),
        ("PIP_NO_INDEX", OsStr::new("1")),
        ("PIP_DISABLE_PIP_VERSION_CHECK", OsStr::new("1")),
        ("npm_config_offline", OsStr::new("true")),
    ];
    for (key, value) in fixed {
        vars.insert(key.to_uppercase(), (key.into(), value.to_os_string()));
    }
    let mut block: Vec<u16> = Vec::new();
    for (key, value) in vars.values() {
        block.extend(key.encode_wide());
        block.push(u16::from(b'='));
        block.extend(value.encode_wide());
        block.push(0);
    }
    block.push(0);
    block
}

/// Run `cmd` through `cmd.exe` inside the container and wait for it. Blocks the calling thread.
pub fn run(cmd: &str, cwd: &Path, timeout: Duration, job: &Job) -> io::Result<Raw> {
    let sid = container_sid()?;
    let inherit =
        SECURITY_ATTRIBUTES { nLength: size_of::<SECURITY_ATTRIBUTES>() as u32, lpSecurityDescriptor: null_mut(), bInheritHandle: 1 };
    let (mut read, mut write): (HANDLE, HANDLE) = (null_mut(), null_mut());
    // SAFETY: both out-pointers are valid.
    if unsafe { CreatePipe(&mut read, &mut write, &inherit, 0) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let (read, write) = (Handle(read), Handle(write));
    // SAFETY: `read` is open; the child gets the write end only.
    unsafe { SetHandleInformation(read.0, HANDLE_FLAG_INHERIT, 0) };
    // SAFETY: the name is NUL-terminated.
    let nul = Handle(unsafe {
        CreateFileW(wide("NUL").as_ptr(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING, 0, null_mut())
    });
    if nul.0 == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }

    // No capabilities: no network, no libraries, no devices.
    let capabilities = SECURITY_CAPABILITIES { AppContainerSid: sid.0, Capabilities: null_mut(), CapabilityCount: 0, Reserved: 0 };
    let handles: [HANDLE; 2] = [nul.0, write.0];
    let mut size = 0usize;
    // SAFETY: the first call only reports the size the list needs.
    unsafe { InitializeProcThreadAttributeList(null_mut(), 2, 0, &mut size) };
    let mut storage = vec![0usize; size.div_ceil(size_of::<usize>()).max(1)];
    let list: LPPROC_THREAD_ATTRIBUTE_LIST = storage.as_mut_ptr().cast();
    // SAFETY: `storage` holds at least `size` bytes and outlives the list.
    if unsafe { InitializeProcThreadAttributeList(list, 2, 0, &mut size) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let _attributes = Attributes(list);
    // SAFETY: the values pointed to (`capabilities`, `handles`) live until the process is created.
    let updated = unsafe {
        UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
            (&raw const capabilities).cast(),
            size_of::<SECURITY_CAPABILITIES>(),
            null_mut(),
            null(),
        ) != 0
            && UpdateProcThreadAttribute(
                list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr().cast(),
                size_of_val(&handles),
                null_mut(),
                null(),
            ) != 0
    };
    if !updated {
        return Err(io::Error::last_os_error());
    }

    // SAFETY: an all-zero start-up structure is valid.
    let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = nul.0;
    startup.StartupInfo.hStdOutput = write.0;
    startup.StartupInfo.hStdError = write.0;
    startup.lpAttributeList = list;

    let shell = std::env::var_os("ComSpec")
        .map(PathBuf::from)
        .filter(|p| p.is_file())
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows\System32\cmd.exe"));
    let program = wide(&shell);
    // `/S /C "…"` runs the text between the outer quotes as typed; `chcp 65001` makes programs print UTF-8.
    let mut line = wide(format!("cmd.exe /D /S /C \"chcp 65001 >\"%TEMP%\\cap-devnull\" 2>&1 & {cmd}\""));
    let env = environment();
    let dir = wide(plain(cwd));
    // SAFETY: an all-zero structure is valid as an out-parameter.
    let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED | CREATE_NO_WINDOW;
    // SAFETY: every pointer is to a live, NUL-terminated buffer or a structure initialised above.
    let started = unsafe {
        CreateProcessW(
            program.as_ptr(),
            line.as_mut_ptr(),
            null(),
            null(),
            1,
            flags,
            env.as_ptr().cast(),
            dir.as_ptr(),
            &startup.StartupInfo,
            &mut info,
        )
    };
    if started == 0 {
        return Err(io::Error::last_os_error());
    }
    let (process, thread) = (Handle(info.hProcess), Handle(info.hThread));
    // The process starts suspended so that it is in the job before it can start anything itself.
    // SAFETY: all three handles are open.
    unsafe {
        AssignProcessToJobObject(job.0.0, process.0);
        ResumeThread(thread.0);
    }
    // Only the child holds the write end now: the pipe ends when the last process in the tree exits.
    drop(write);
    drop(nul);

    let reader = std::thread::spawn(move || {
        let read = read;
        let mut out = Vec::new();
        let mut chunk = [0u8; 8192];
        loop {
            let mut n = 0u32;
            // SAFETY: `chunk` is writable for its whole length.
            let ok = unsafe { ReadFile(read.0, chunk.as_mut_ptr(), chunk.len() as u32, &mut n, null_mut()) };
            if ok == 0 || n == 0 {
                return out;
            }
            out.extend_from_slice(&chunk[..n as usize]);
        }
    });

    let wait_ms = timeout.as_millis().min(u128::from(u32::MAX - 1)) as u32;
    // SAFETY: the process handle is open.
    let timed_out = unsafe { WaitForSingleObject(process.0, wait_ms) } == WAIT_TIMEOUT;
    if timed_out {
        job.kill();
        // SAFETY: as above.
        unsafe { WaitForSingleObject(process.0, 5000) };
    }
    let mut code = 0u32;
    // SAFETY: `code` is a valid out-pointer.
    let has_code = unsafe { GetExitCodeProcess(process.0, &mut code) } != 0;
    // Programs the command left running would keep the pipe open; they go with the job.
    job.kill();
    let out = reader.join().unwrap_or_default();
    Ok(Raw { code: (has_code && !timed_out).then_some(code as i32), out, timed_out })
}

// ---- handing folders to the container ----

fn record_path() -> PathBuf {
    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_default();
    local.join("cap").join("sandbox.json")
}

/// The record: project folder -> tool folders it was set up with.
fn load_record() -> Map<String, Value> {
    std::fs::read_to_string(record_path())
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn save_record(record: &Map<String, Value>) -> Result<(), String> {
    let path = record_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("could not create {}: {e}", dir.display()))?;
    }
    let text = serde_json::to_string_pretty(record).unwrap_or_default();
    std::fs::write(&path, text).map_err(|e| format!("could not write {}: {e}", path.display()))
}

fn key(root: &Path) -> String {
    let full = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    plain(&full).to_string_lossy().to_lowercase()
}

pub fn is_set_up(root: &Path) -> bool {
    load_record().contains_key(&key(root))
}

fn icacls(target: &Path, args: &[&str]) -> Result<(), String> {
    let out = std::process::Command::new("icacls").arg(target).args(args).output().map_err(|e| format!("icacls did not start: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!("icacls {} failed on {} (exit {})", args.first().unwrap_or(&""), target.display(), out.status.code().unwrap_or(-1)))
    }
}

/// Hand `root` (read and write) and `tools` (read and run) to the container.
pub fn setup(root: &Path, tools: &[PathBuf]) -> Result<String, String> {
    let root = plain(&std::fs::canonicalize(root).map_err(|e| format!("{}: {e}", root.display()))?);
    let sid = sid_string().map_err(|e| e.to_string())?;
    icacls(&root, &["/grant", &format!("*{sid}:(OI)(CI)M"), "/Q"])?;
    // Contained processes run at low integrity and write only to folders labelled low.
    icacls(&root, &["/setintegritylevel", "(OI)(CI)low", "/Q"])?;
    // A program file labelled low would start as a low process even outside the container.
    let _ = icacls(&root.join("*.exe"), &["/setintegritylevel", "medium", "/T", "/C", "/Q"]);
    let mut given: Vec<String> = Vec::new();
    for tool in tools {
        let tool = plain(&std::fs::canonicalize(tool).map_err(|e| format!("{}: {e}", tool.display()))?);
        icacls(&tool, &["/grant", &format!("*{sid}:(OI)(CI)RX"), "/Q"])?;
        if tool.join("pyvenv.cfg").is_file() {
            // Files hard-linked into a virtual environment keep their own access list.
            let _ = icacls(&tool.join("*"), &["/grant", &format!("*{sid}:RX"), "/Q", "/T", "/C"]);
        }
        given.push(tool.to_string_lossy().into_owned());
    }
    let mut record = load_record();
    let entry = key(&root);
    let mut all: Vec<String> = record
        .get(&entry)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default();
    for tool in given {
        if !all.iter().any(|t| t.eq_ignore_ascii_case(&tool)) {
            all.push(tool);
        }
    }
    let tool_count = all.len();
    record.insert(entry, json!(all));
    save_record(&record)?;
    Ok(format!(
        "{} is set up: commands in it run in the container {PROFILE} ({sid}), with {tool_count} tool folder(s) to read. `cap sandbox undo` takes this back.",
        root.display()
    ))
}

/// Take `root` and the tool folders no other project uses back from the container.
pub fn undo(root: &Path) -> Result<String, String> {
    let entry = key(root);
    let mut record = load_record();
    let Some(tools) = record.remove(&entry) else {
        return Ok(format!("{} was not set up.", root.display()));
    };
    let sid = sid_string().map_err(|e| e.to_string())?;
    let folder = PathBuf::from(&entry);
    if folder.is_dir() {
        icacls(&folder, &["/remove:g", &format!("*{sid}"), "/Q"])?;
        icacls(&folder, &["/setintegritylevel", "(OI)(CI)medium", "/Q"])?;
    }
    let still_used =
        |tool: &str| record.values().filter_map(Value::as_array).flatten().filter_map(Value::as_str).any(|t| t.eq_ignore_ascii_case(tool));
    for tool in tools.as_array().into_iter().flatten().filter_map(Value::as_str) {
        if !still_used(tool) && Path::new(tool).is_dir() {
            icacls(Path::new(tool), &["/remove:g", &format!("*{sid}"), "/Q"])?;
        }
    }
    save_record(&record)?;
    Ok(format!("{} is back to normal access.", folder.display()))
}

pub fn status(root: &Path) -> String {
    let record = load_record();
    let mut out = match record.get(&key(root)).and_then(Value::as_array) {
        Some(tools) => format!(
            "appcontainer: {} is set up ({} tool folder(s)). Commands in it open only these folders and have no network.",
            root.display(),
            tools.len()
        ),
        None => format!("appcontainer: {} is not set up. Commands run without isolation until `cap sandbox setup`.", root.display()),
    };
    for (folder, tools) in &record {
        let tools: Vec<&str> = tools.as_array().into_iter().flatten().filter_map(Value::as_str).collect();
        out.push_str(&format!("\n  {folder}{}", if tools.is_empty() { String::new() } else { format!(" + {}", tools.join(", ")) }));
    }
    out
}
