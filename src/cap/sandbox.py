"""명령 샌드박스(OS 수준 격리) — Codex 의 sandbox 와 같은 생각.

모델이 돌리는 명령(run·run_tests·백그라운드·관문의 테스트)은 격리 안에서 돈다:
쓰기는 작업 폴더와 임시 폴더만, 네트워크(TCP) 연결은 막힌다. 읽기는 어디든 된다.

backend:
- landlock: 리눅스. 커널의 Landlock 으로 자기 자신을 제한한 뒤 명령을 실행(landlock_exec.py). 관리자 권한 불필요.
- appcontainer: Windows 자체 격리(WSL 불필요, 관리자 권한 불필요). AppContainer 안에서 cmd.exe 로 돈다
            (appcontainer_exec.py). 파일은 허락한 폴더만 읽고 쓰고(작업 폴더는 읽기·쓰기, 파이썬은 읽기·실행),
            네트워크는 OS 가 막는다. /sandbox setup 을 한 번 하면 기본으로 켜진다.
- wsl:      Windows 에서 WSL 리눅스 안의 Landlock 으로. 명령이 리눅스 환경(WSL 의 python·도구)에서 돈다.
            /sandbox setup wsl 로 WSL 안에 CAP 용 파이썬(pytest)을 만든다.

설정(앞에 있는 것이 이김): /sandbox 명령 > 환경변수 CAP_SANDBOX > 사용자 설정 ~/.cap/settings.json "sandbox".
프로젝트 설정(.cap/settings.json)으로는 끄지 못한다(남의 저장소가 격리를 끌 수 있으므로).
값: on(가능한 것으로 켬) · off · auto(기본: 리눅스면 켬, Windows 는 /sandbox setup 을 했으면 켬) · appcontainer · wsl · landlock.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCHER = Path(__file__).with_name("landlock_exec.py")
APPC = Path(__file__).with_name("appcontainer_exec.py")
STATE: dict = {"mode": None}           # /sandbox 로 바꾼 값(세션 동안)
ESCALATE_CATEGORIES = {"install", "network", "git_push"}   # 사람이 허락하면 격리 밖에서(네트워크·홈 쓰기가 본질)
DENIAL_RX = re.compile(r"Permission denied|Operation not permitted|Read-only file system|Network is unreachable|"
                       r"Access is denied|액세스가 거부|PermissionError|WinError 5\b|getaddrinfo failed|WinError 1001[34]|"
                       r"gaierror|appcontainer_exec:|"
                       r"Temporary failure in name resolution|Could not resolve host|Failed to establish a new connection"
                       r"|landlock_exec:", re.I)
HINT = ("\n[sandbox] This command ran in the sandbox: it may write only inside the workspace and the temp folder, "
        "and has no network. If it failed because of that and truly needs more, run it again with escalate=true "
        "(the user will be asked).")


def setting() -> str:
    if STATE["mode"]:
        return STATE["mode"]
    env = os.environ.get("CAP_SANDBOX", "").strip().lower()
    if env:
        return env
    try:
        from .memory import user_home
        v = json.loads((user_home() / "settings.json").read_text(encoding="utf-8")).get("sandbox")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "auto"


@functools.lru_cache(maxsize=None)
def _landlock_native() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        r = subprocess.run([sys.executable, str(LAUNCHER), "--write", "/tmp", "--no-net", "--", "true"],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _wsl_cmd() -> bool:
    return os.name == "nt" and bool(shutil.which("wsl.exe") or shutil.which("wsl"))


@functools.lru_cache(maxsize=None)
def _wsl_ok() -> bool:
    if not _wsl_cmd():
        return False
    try:
        r = subprocess.run(["wsl.exe", "--exec", "python3", to_wsl(LAUNCHER), "--write", "/tmp", "--no-net", "--",
                            "true"], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


# ---------------------------------------------------------------- Windows 자체 격리(AppContainer)
def _appc_store() -> Path:
    from .memory import user_home
    return user_home() / "sandbox.json"


def _appc_state() -> dict:
    try:
        v = json.loads(_appc_store().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _appc_save(st: dict) -> None:
    p = _appc_store()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")


def appc_ready() -> bool:
    """/sandbox setup 을 했나(파이썬 폴더에 읽기·실행 권한을 줘 둠)."""
    return os.name == "nt" and bool(_appc_state().get("sid"))


def _python_dirs() -> list[str]:
    out = []
    for d in (sys.base_prefix, sys.prefix):
        if d and d not in out and Path(d).is_dir():
            out.append(str(Path(d).resolve()))
    return out


def appc_setup() -> tuple[bool, str]:
    """Windows 자체 격리 준비: CAP 전용 AppContainer 를 만들고 파이썬 폴더에 그 격리의 읽기·실행 권한만 준다."""
    if os.name != "nt":
        return False, "AppContainer is Windows only"
    from . import appcontainer_exec as A
    try:
        _, sid = A.container_sid()
    except OSError as exc:
        return False, f"could not create the AppContainer: {exc}"
    for d in _python_dirs():
        ok, out = A.grant(d, sid, "RX", every_file=True)
        if not ok:
            return False, f"could not give read access on {d}: {out[:200]}"
    st = _appc_state()
    st.update(sid=sid, rx=_python_dirs(), workspaces=st.get("workspaces", []))
    _appc_save(st)
    return True, (f"Windows sandbox ready (AppContainer {sid[:22]}…). Read+run access given on: "
                  + ", ".join(_python_dirs()) + ". Each workspace gets read+write the first time a command runs there.")


def appc_prepare(ws: Path) -> tuple[bool, str]:
    """이 작업 폴더에 격리의 읽기·쓰기 권한과 Low 무결성 레이블을 준다(폴더마다 한 번). 기록은 ~/.cap/sandbox.json."""
    st = _appc_state()
    key = str(ws.resolve())
    for w in st.get("workspaces", []):           # 이미 준비된 폴더 안(예: 기준선 사본 .cap/base-*)은 권한을 물려받는다
        if key != w and key.lower().startswith(w.lower().rstrip("\\/") + os.sep):
            return True, ""
    venv_note = _appc_project_venv(ws, st)
    if key in st.get("workspaces", []):
        if key not in st.setdefault("exe_medium", []):    # 이 수정 전에 준비된 폴더: 실행 파일 레이블을 한 번 되돌린다
            from . import appcontainer_exec as A
            A.medium_exes(key)
            st["exe_medium"].append(key)
            _appc_save(st)
        return True, venv_note
    st["workspaces"] = [w for w in st.get("workspaces", []) if Path(w).is_dir()]   # 지워진 폴더(실험 임시 폴더 등)는 뺀다
    from . import appcontainer_exec as A
    ok, out = A.grant(key, st["sid"], "M")
    if ok:
        ok, out = A.label(key, "low")
    if not ok:
        return False, out[:300]
    A.medium_exes(key)          # 실행 파일은 Medium 으로(사용자가 직접 실행할 때 Low 로 뜨지 않게)
    st.setdefault("exe_medium", []).append(key)
    st.setdefault("workspaces", []).append(key)
    _appc_save(st)
    return True, f"[sandbox] gave the Windows sandbox read+write access to {key} (undo: /sandbox undo)"


def _project_venvs(ws: Path) -> list[tuple[Path, str]]:
    """작업 폴더 안의 가상환경과 그 기본 파이썬 설치(pyvenv.cfg 의 home)."""
    out = []
    for name in (".venv", "venv"):
        cfg = ws / name / "pyvenv.cfg"
        if not cfg.is_file():
            continue
        home = ""
        for ln in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            k, _, v = ln.partition("=")
            if k.strip().lower() == "home":
                home = v.strip()
        out.append((ws / name, home))
    return out


def _appc_project_venv(ws: Path, st: dict) -> str:
    """프로젝트 .venv 를 격리 안에서 쓰게: ① 그 venv 의 기본 파이썬 설치(작업 폴더 밖)에 읽기·실행,
    ② venv 파일 하나하나에 권한(uv 는 캐시를 하드 링크해서 작업 폴더의 상속 권한을 받지 않는다).
    ②는 패키지가 바뀌었을 때(site-packages 시각)만 다시 한다."""
    from . import appcontainer_exec as A
    notes, changed = [], False
    for venv, home in _project_venvs(ws):
        if home and Path(home).is_dir() and home not in st.setdefault("rx", []):
            ok, _ = A.grant(home, st["sid"], "RX", every_file=True)
            if ok:
                st["rx"].append(home)
                changed = True
                notes.append(f"read access to the project venv's Python ({home})")
        site = next((p for p in (venv / "Lib" / "site-packages", venv / "lib") if p.is_dir()), venv)
        scripts = next((p for p in (venv / "Scripts", venv / "bin") if p.is_dir()), venv)
        stamp = f"{site.stat().st_mtime_ns}:{scripts.stat().st_mtime_ns}"
        if st.setdefault("venvs", {}).get(str(venv)) != stamp:
            ok, _ = A.grant(str(venv), st["sid"], "M", every_file=True)
            A.medium_exes(str(venv))    # 새로 깔린 실행 파일도 Medium 으로
            if ok:
                st["venvs"][str(venv)] = stamp
                changed = True
    if changed:
        _appc_save(st)
    return ("[sandbox] gave the Windows sandbox " + "; ".join(notes)) if notes else ""


def appc_undo(ws: Path) -> str:
    """/sandbox undo: 이 작업 폴더에 준 권한과 레이블을 되돌린다."""
    st = _appc_state()
    key = str(ws.resolve())
    if not st.get("sid") or key not in st.get("workspaces", []):
        return "Nothing to undo for this folder."
    from . import appcontainer_exec as A
    A.revoke(key, st["sid"])
    A.label(key, "medium")
    st["workspaces"] = [w for w in st["workspaces"] if w != key]
    _appc_save(st)
    return f"Removed the Windows sandbox's access to {key}."


def backend(mode: str | None = None) -> str:
    """지금 쓸 격리 방식. 없으면 ""(격리 없음)."""
    m = (mode or setting())
    if m in ("off", "none", "0", "false", "no"):
        return ""
    if m in ("landlock",) or (m in ("auto", "on") and sys.platform.startswith("linux")):
        return "landlock" if _landlock_native() else ""
    if m == "appcontainer" or (m in ("auto", "on") and os.name == "nt" and appc_ready()):
        return "appcontainer" if appc_ready() else ""
    if m == "wsl" or (m == "on" and os.name == "nt"):
        return "wsl" if _wsl_ok() else ""
    if m == "auto" and os.name == "nt" and _wsl_cmd() and wsl_has_venv():
        return "wsl" if _wsl_ok() else ""
    return ""


def describe() -> str:
    b = backend()
    s = setting()
    if b:
        extra = ""
        if b == "wsl":
            extra = (" (commands run inside WSL Linux with its own tools"
                     + ("" if wsl_has_venv() else "; run '/sandbox setup wsl' once to get python + pytest there") + ")")
        if b == "appcontainer":
            return (f"sandbox: on — Windows AppContainer (commands run in cmd.exe; reads and writes only in the "
                    f"workspace, Python and system folders are read-only, no network). Setting: {s}")
        return f"sandbox: on — {b}{extra}; writes only in the workspace and temp, no network. Setting: {s}"
    if os.name == "nt":
        why = ("run '/sandbox setup' once: commands then run in a Windows AppContainer (no WSL, no admin) and the "
               "sandbox is on by default")
    else:
        why = "Landlock is not available on this kernel"
    return f"sandbox: off (setting: {s}). {why}."


def to_wsl(p: Path | str) -> str:
    """C:\\Users\\a b → /mnt/c/Users/a b"""
    s = str(Path(p).resolve())
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        raise ValueError(f"not a drive path: {s}")
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def wrap(cmd: str, ws: Path, b: str, timeout: int | None = None) -> tuple[list[str] | None, str]:
    """격리해서 돌릴 실행 인자. (None, 오류) 면 격리를 쓸 수 없음."""
    if b == "landlock":
        return [sys.executable, str(LAUNCHER), "--write", str(ws.resolve()), "--write", "/tmp", "--no-net", "--",
                "bash", "-c", cmd], ""
    if b == "appcontainer":
        ok, note = appc_prepare(ws)
        if not ok:
            return None, f"could not prepare the Windows sandbox for this folder: {note}"
        return [sys.executable, str(APPC), "--cwd", str(ws.resolve()), "--sbx", str(ws.resolve() / ".cap" / "sbx"),
                "--shell", "--", cmd], ""
    if b == "wsl":
        try:
            w = to_wsl(ws)
        except ValueError as exc:
            return None, str(exc)
        inner = ["python3", to_wsl(LAUNCHER), "--write", w, "--write", "/tmp", "--no-net", "--"]
        if timeout:
            inner += ["timeout", "-k", "5", str(timeout)]
        return ["wsl.exe", "--cd", w, "--exec", *inner, "bash", "-c", WSL_PREFIX + cmd], ""
    return None, f"unknown sandbox backend {b}"


WSL_VENV = "$HOME/.cap/wsl-venv"
WSL_PREFIX = (f'[ -x "{WSL_VENV}/bin/python" ] && export PATH="{WSL_VENV}/bin:$PATH"; '
              'export PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8; ')


@functools.lru_cache(maxsize=None)
def wsl_has_venv() -> bool:
    try:
        r = subprocess.run(["wsl.exe", "--exec", "bash", "-c", f'[ -x "{WSL_VENV}/bin/python" ] && '
                            f'"{WSL_VENV}/bin/python" -m pytest --version'], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wsl_setup() -> tuple[bool, str]:
    """/sandbox setup: WSL 안에 CAP 용 파이썬 가상환경(pytest 포함)을 만든다. 사람이 직접 부르는 설정이라 격리 밖에서."""
    script = (f'python3 -m venv "{WSL_VENV}" && "{WSL_VENV}/bin/pip" install -q --disable-pip-version-check pytest '
              f'&& "{WSL_VENV}/bin/python" -m pytest --version')
    try:
        r = subprocess.run(["wsl.exe", "--exec", "bash", "-c", script], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"WSL setup failed: {exc}"
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    wsl_has_venv.cache_clear()
    return r.returncode == 0, out[-600:]


def note_if_blocked(code: int, out: str) -> str:
    return HINT if code != 0 and DENIAL_RX.search(out or "") else ""
