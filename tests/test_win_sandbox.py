"""Windows 자체 격리(AppContainer): 판정·배선은 어디서나, 실제 격리는 Windows 에서만."""
import os
import sys
from pathlib import Path

import pytest

from cap import sandbox as S
from cap import tools as T

win = pytest.mark.skipif(os.name != "nt", reason="Windows only")


def test_auto_prefers_appcontainer_once_set_up(monkeypatch):
    monkeypatch.setattr(S.os, "name", "nt")
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(S, "_wsl_cmd", lambda: True)
    monkeypatch.setattr(S, "_wsl_ok", lambda: True)
    monkeypatch.setattr(S, "wsl_has_venv", lambda: True)
    monkeypatch.setattr(S, "appc_ready", lambda: False)
    assert S.backend("auto") == "wsl"                      # 준비 전: WSL 이 있으면 WSL
    monkeypatch.setattr(S, "appc_ready", lambda: True)
    assert S.backend("auto") == "appcontainer" and S.backend("on") == "appcontainer"   # 준비 뒤: WSL 없이
    assert S.backend("wsl") == "wsl" and S.backend("off") == ""


def test_wrap_appcontainer_runs_cmd_in_the_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "appc_prepare", lambda ws: (True, ""))
    args, err = S.wrap('python -c "print(1)"', tmp_path, "appcontainer")
    assert not err and args[1].endswith("appcontainer_exec.py") and "--shell" in args
    assert args[-1] == 'python -c "print(1)"' and str(tmp_path.resolve() / ".cap" / "sbx") in args
    monkeypatch.setattr(S, "appc_prepare", lambda ws: (False, "denied"))
    args, err = S.wrap("x", tmp_path, "appcontainer")
    assert args is None and "denied" in err


def test_windows_denials_get_the_escalate_hint():
    assert "escalate=true" in S.note_if_blocked(1, "PermissionError: [WinError 5] Access is denied")
    assert "escalate=true" in S.note_if_blocked(1, "socket.gaierror: [Errno 11001] getaddrinfo failed")


@win
def test_real_appcontainer_blocks_outside_writes_reads_and_network(tmp_path):
    ok, msg = S.appc_setup()
    assert ok, msg
    ws = tmp_path / "proj"
    (ws / "tests").mkdir(parents=True)
    (ws / "m.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "tests" / "test_m.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from m import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("private", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    (ws / "probe.py").write_text(
        "import socket\nopen('inside.txt','w').write('x')\n"
        f"for p, what in [({str(outside)!r}, 'write'), ({str(secret)!r}, 'read')]:\n"
        "    try:\n"
        "        open(p, 'w' if what == 'write' else 'r').write('x') if what == 'write' else open(p).read()\n"
        "        print(what, 'ALLOWED')\n"
        "    except OSError:\n"
        "        print(what, 'blocked')\n"
        "try:\n    socket.create_connection(('example.com', 443), timeout=5); print('net ALLOWED')\n"
        "except OSError:\n    print('net blocked')\n", encoding="utf-8")
    try:
        code, out = T.run_shell("python probe.py", ws, sandbox="appcontainer")
        assert code == 0, out
        assert "write blocked" in out and "read blocked" in out and "net blocked" in out
        assert (ws / "inside.txt").exists() and not outside.exists()
        for _ in range(2):        # 캐시까지 쓰는 그냥 pytest 를 두 번(임시 폴더 0o700 ACL 로 끝에서 멈추던 것)
            code, out = T.run_shell("python -m pytest -q", ws, timeout=60, sandbox="appcontainer")
            assert code == 0 and "1 passed" in out and "warning" not in out, out
        (ws / "tdir.py").write_text("import tempfile, os\nwith tempfile.TemporaryDirectory(dir='.') as d:\n"
                                    "    open(os.path.join(d, 'f'), 'w').write('x')\nprint('tempdir ok')\n",
                                    encoding="utf-8")
        code, out = T.run_shell("python tdir.py", ws, timeout=60, sandbox="appcontainer")
        assert code == 0 and "tempdir ok" in out, out
    finally:
        assert "Removed" in S.appc_undo(ws)


def test_grant_every_file_also_grants_existing_files(monkeypatch):
    """uv·pipx 의 venv 는 캐시 파일을 하드 링크해서 폴더의 상속 권한을 받지 않는다 → 파일마다 따로 준다."""
    from cap import appcontainer_exec as A
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(A.subprocess, "run", lambda args, **kw: calls.append(args) or R())
    A.grant(r"C:\venv", "S-1-15-2-1", "RX", every_file=True)
    assert calls[0][:4] == ["icacls", r"C:\venv", "/grant", "*S-1-15-2-1:(OI)(CI)RX"]
    assert calls[1][1].endswith("*") and "*S-1-15-2-1:RX" in calls[1] and "/T" in calls[1]
    calls.clear()
    A.grant(r"C:\ws", "S-1-15-2-1", "M")
    assert len(calls) == 1 and "/T" not in calls[0]


def test_project_venv_gets_its_base_python_and_file_grants(tmp_path, monkeypatch):
    """프로젝트 .venv: 기본 파이썬 설치(작업 폴더 밖)에 읽기·실행, venv 파일마다 권한(uv 하드 링크), 패키지가 바뀔 때만 다시."""
    from cap import appcontainer_exec as A
    base = tmp_path / "py311"
    base.mkdir()
    ws = tmp_path / "proj"
    (ws / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
    (ws / ".venv" / "pyvenv.cfg").write_text(f"home = {base}\nversion = 3.11\n", encoding="utf-8")
    grants = []
    monkeypatch.setattr(A, "grant", lambda path, sid, rights, every_file=False: grants.append((path, rights, every_file)) or (True, ""))
    st = {"sid": "S-1-15-2-1", "rx": []}
    monkeypatch.setattr(S, "_appc_save", lambda s: None)
    note = S._appc_project_venv(ws, st)
    assert (str(base), "RX", True) in grants and (str(ws / ".venv"), "M", True) in grants and str(base) in note
    grants.clear()
    assert S._appc_project_venv(ws, st) == "" and grants == []          # 바뀐 것 없음 → 다시 안 함
