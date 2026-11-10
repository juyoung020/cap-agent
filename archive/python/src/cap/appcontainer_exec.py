"""Windows 명령 격리(WSL 없이) — AppContainer 안에서 명령을 실행한다.

AppContainer 는 Edge·스토어 앱이 쓰는 Windows 기본 격리다. 관리자 권한이 필요 없다.
- 파일: 허락(ACL)받은 폴더만 읽고 쓴다. 작업 폴더·CAP 전용 임시 폴더는 읽기·쓰기, 파이썬·도구 폴더는 읽기·실행.
  사용자 프로필의 다른 파일(.ssh, 브라우저 자료, 다른 프로젝트)은 읽지도 못한다.
- 네트워크: 능력(capability)을 하나도 주지 않으므로 인터넷·사설망·루프백 모두 막힌다.
- 작업 개체(Job)로 묶어 이 실행기가 죽으면 안의 프로세스도 같이 죽는다.

쓰기:  python appcontainer_exec.py --grant-rw <폴더> ... --grant-rx <폴더> ... -- <명령 인자...>
권한 부여(--grant-*)는 한 번 해 두면 남는다(/sandbox setup). 실행 때는 이미 있는지만 확인한다.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes as wt

PROFILE = "cap.sandbox"
DISPLAY = "CAP command sandbox"

if os.name == "nt":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
else:                   # 다른 OS 에서는 불러오기만 된다(판정·배선 시험용). 격리 실행은 Windows 에서만
    k32 = userenv = advapi = None

PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
STARTF_USESTDHANDLES = 0x00000100
INFINITE = 0xFFFFFFFF
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
HRESULT_ALREADY_EXISTS = 0x800700B7 - (1 << 32)


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def container_sid() -> tuple[ctypes.c_void_p, str]:
    """CAP 전용 AppContainer 프로필(없으면 만듦)의 SID: (포인터, 문자열)."""
    sid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(wt.LPCWSTR(PROFILE), wt.LPCWSTR(DISPLAY), wt.LPCWSTR(DISPLAY),
                                           None, 0, ctypes.byref(sid))
    if hr == HRESULT_ALREADY_EXISTS or hr != 0:
        sid = ctypes.c_void_p()
        hr = userenv.DeriveAppContainerSidFromAppContainerName(wt.LPCWSTR(PROFILE), ctypes.byref(sid))
        if hr != 0:
            raise OSError(f"AppContainer profile failed (HRESULT {hr & 0xFFFFFFFF:#x})")
    s = wt.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(s)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sid, s.value


def grant(path: str, sid: str, rights: str, every_file: bool = False) -> tuple[bool, str]:
    """폴더에 이 AppContainer 의 권한을 준다(icacls, 하위에 상속). rights: M(읽기·쓰기) 또는 RX(읽기·실행).
    every_file: 이미 있는 파일 하나하나에도(uv·pipx 의 venv 는 캐시 파일을 하드 링크해서 상속을 받지 않는다)."""
    r = subprocess.run(["icacls", path, "/grant", f"*{sid}:(OI)(CI){rights}", "/Q"],
                       capture_output=True, text=True, encoding="mbcs", errors="replace")
    if r.returncode != 0 or not every_file:
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    # 하위 항목 하나하나에 명시적으로(상속 표시 (OI)(CI) 는 파일에 붙지 않으므로 그냥 권한만)
    r2 = subprocess.run(["icacls", os.path.join(path, "*"), "/grant", f"*{sid}:{rights}", "/Q", "/T", "/C"],
                        capture_output=True, text=True, encoding="mbcs", errors="replace")
    return r2.returncode == 0, (r2.stdout + r2.stderr).strip()


def label(path: str, level: str) -> tuple[bool, str]:
    """무결성 레이블(low·medium). 격리 프로세스는 Low 라서, Medium(기본) 폴더에는 DACL 이 허락해도 쓰지 못한다."""
    r = subprocess.run(["icacls", path, "/setintegritylevel", f"(OI)(CI){level}", "/Q"], capture_output=True,
                       text=True, encoding="mbcs", errors="replace")
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def medium_exes(path: str) -> tuple[bool, str]:
    """폴더 아래 실행 파일(*.exe)은 Medium 레이블로. Low 레이블을 물려받은 실행 파일은 사용자가 직접 실행해도
    Low 프로세스로 떠서(예: 프로젝트 .venv 의 python.exe 가 기본 파이썬을 못 열어 'permission denied') 망가진다.
    격리 안에서는 그대로 실행된다(Low 가 Medium 파일을 실행하면 Low 로 뜸). 고치지는 못한다."""
    if os.name != "nt":
        return True, ""
    r = subprocess.run(["icacls", os.path.join(path, "*.exe"), "/setintegritylevel", "medium", "/T", "/C", "/Q"],
                       capture_output=True, text=True, encoding="mbcs", errors="replace")
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def revoke(path: str, sid: str) -> tuple[bool, str]:
    r = subprocess.run(["icacls", path, "/remove:g", f"*{sid}", "/Q"], capture_output=True, text=True,
                       encoding="mbcs", errors="replace")
    return r.returncode == 0, (r.stdout + r.stderr).strip()


SITECUSTOMIZE = """# CAP 샌드박스 안의 파이썬: 볼 수 없는 폴더(드라이브 뿌리, Users …)는 '없음'으로 다룬다(3.11 pathlib 은 권한 오류를 냄)
import errno, pathlib
if hasattr(pathlib, "_IGNORED_ERRNOS"):
    pathlib._IGNORED_ERRNOS = tuple(pathlib._IGNORED_ERRNOS) + (errno.EACCES,)
if hasattr(pathlib, "_IGNORED_WINERRORS"):
    pathlib._IGNORED_WINERRORS = tuple(pathlib._IGNORED_WINERRORS) + (5,)
# 새 파이썬은 Windows 에서 mkdir(mode=0o700)(임시 폴더)에 '현재 사용자만' ACL 을 건다. 격리의 SID 가 빠져
# 자기가 만든 폴더에 못 들어가고, pytest 캐시 정리는 그 오류를 끝없이 다시 시도한다. 격리 안에서는 기본 권한으로.
import os as _os
_mkdir = _os.mkdir
def _cap_mkdir(path, mode=0o777, *a, **k):
    return _mkdir(path, 0o777 if mode == 0o700 else mode, *a, **k)
_os.mkdir = _cap_mkdir
# 어떤 컴퓨터(GitHub Actions 의 Windows 등)는 격리 안에서 NUL 장치를 열지 못한다(pytest 가 시작할 때 연다).
# 그럴 때 os.devnull 을 임시 폴더의 파일로 바꾼다(subprocess.DEVNULL 도 이것을 쓴다).
try:
    open(_os.devnull, "rb").close()
except OSError:
    _os.devnull = _os.path.join(_os.environ.get("TEMP") or ".", "cap-devnull")
"""


def sandbox_env(base: dict, sbx: str) -> dict:
    """격리 안의 환경: 임시·홈 폴더는 작업 폴더 안(.cap/sbx), 파이썬 보정, 네트워크 도구는 오프라인 설정,
    PATH 앞에 유닉스 명령(ls·cat·grep …, winutils.py)을 둔 .cap/sbx/bin."""
    from pathlib import Path as _P
    try:
        from . import winutils
    except ImportError:              # 이 파일을 스크립트로 돌릴 때(sandbox.wrap)
        import winutils
    d = _P(sbx)
    for sub in ("tmp", "home", "site"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    # NUL 대신 쓰는 파일(sitecustomize·winutils 의 %TEMP%\cap-devnull)이 커지지 않게. Windows 는 격리 안의 TEMP 를
    # 격리 전용 폴더(%LOCALAPPDATA%\Packages\cap.sandbox\AC\Temp)로 바꿔 준다.
    junk = _P(os.environ.get("LOCALAPPDATA", "")) / "Packages" / PROFILE / "AC" / "Temp" / "cap-devnull"
    try:
        if junk.is_file() and junk.stat().st_size > 1 << 20:
            junk.unlink()
    except OSError:
        pass
    (d / "site" / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    py = getattr(sys, "_base_executable", "") or sys.executable     # venv 리디렉터를 건너뛴 진짜 파이썬(읽기·실행 허락됨)
    bin_dir = winutils.install(str(d / "bin"), py if os.path.isfile(py) else sys.executable)
    env = dict(base)
    env.update(TMP=str(d / "tmp"), TEMP=str(d / "tmp"), HOME=str(d / "home"),
               PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", npm_config_offline="true",
               PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(d / "site") + (";" + base["PYTHONPATH"] if base.get("PYTHONPATH") else "")
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = bin_dir + (";" + env[path_key] if env.get(path_key) else "")
    return env


def run(argv: list[str], cwd: str | None = None, env: dict | None = None, cmdline: str | None = None) -> int:
    """AppContainer 안에서 argv(또는 그대로 쓸 cmdline)를 실행하고 종료 코드를 돌려준다.
    표준 입출력은 이 프로세스의 것을 물려준다."""
    sid, _ = container_sid()
    caps = SECURITY_CAPABILITIES(sid.value, None, 0, 0)       # 능력 없음 = 네트워크 없음
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k32.UpdateProcThreadAttribute(buf, 0, ctypes.c_size_t(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES),
                                         ctypes.byref(caps), ctypes.sizeof(caps), None, None):
        raise ctypes.WinError(ctypes.get_last_error())
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    k32.GetStdHandle.restype = wt.HANDLE
    si.StartupInfo.hStdInput = k32.GetStdHandle(wt.DWORD(-10 & 0xFFFFFFFF))
    si.StartupInfo.hStdOutput = k32.GetStdHandle(wt.DWORD(-11 & 0xFFFFFFFF))
    si.StartupInfo.hStdError = k32.GetStdHandle(wt.DWORD(-12 & 0xFFFFFFFF))
    for h in (si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError):
        if h:
            k32.SetHandleInformation(wt.HANDLE(h), 1, 1)        # HANDLE_FLAG_INHERIT
    si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
    pi = PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(cmdline if cmdline is not None else subprocess.list2cmdline(argv))
    env_block = None
    if env is not None:
        env_block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in env.items()) + "\0\0")
    k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                   ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
    ok = k32.CreateProcessW(None, cmdline, None, None, True,
                            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
                            env_block, cwd, ctypes.byref(si), ctypes.byref(pi))
    k32.DeleteProcThreadAttributeList(buf)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    k32.CreateJobObjectW.restype = wt.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wt.HANDLE(job), JobObjectExtendedLimitInformation, ctypes.byref(info),
                                ctypes.sizeof(info))
    k32.AssignProcessToJobObject(wt.HANDLE(job), pi.hProcess)
    k32.ResumeThread(pi.hThread)
    k32.WaitForSingleObject(pi.hProcess, INFINITE)
    code = wt.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    for h in (pi.hThread, pi.hProcess):
        k32.CloseHandle(h)
    k32.CloseHandle(wt.HANDLE(job))
    return code.value


def main(args: list[str]) -> int:
    if "--" not in args:
        print("appcontainer_exec: usage: [--cwd DIR] [--grant-rw DIR]... [--grant-rx DIR]... -- command ...",
              file=sys.stderr)
        return 97
    k = args.index("--")
    opts, cmd = args[:k], args[k + 1:]
    cwd, rw, rx, sbx, shell = None, [], [], None, False
    i = 0
    while i < len(opts):
        if opts[i] == "--shell":           # 나머지 한 덩어리를 cmd.exe 에 그대로(따옴표 보존)
            shell = True
            i += 1
            continue
        if opts[i] == "--cwd":
            cwd = opts[i + 1]
        elif opts[i] == "--sbx":
            sbx = opts[i + 1]
        elif opts[i] == "--grant-rw":
            rw.append(opts[i + 1])
        elif opts[i] == "--grant-rx":
            rx.append(opts[i + 1])
        i += 2
    try:
        _, s = container_sid()
        for path, rights in [(p, "M") for p in rw] + [(p, "RX") for p in rx]:
            ok, out = grant(path, s, rights)
            if not ok:
                print(f"appcontainer_exec: could not grant {rights} on {path}: {out[:200]}", file=sys.stderr)
                return 97
        if not cmd:
            return 0
        env = sandbox_env(dict(os.environ), sbx) if sbx else dict(os.environ)
        if shell:
            return run([], cwd=cwd, env=env, cmdline='cmd.exe /d /s /c "' + " ".join(cmd) + '"')
        return run(cmd, cwd=cwd, env=env)
    except OSError as exc:
        print(f"appcontainer_exec: {exc}", file=sys.stderr)
        return 97


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
