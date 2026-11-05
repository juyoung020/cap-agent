"""명령 샌드박스: 판정·배선(가짜 실행)과, 격리가 되는 환경에서는 실제 격리까지."""
import sys
import tempfile
from pathlib import Path

import pytest

from cap import sandbox as sbx
from cap import tools as T
from cap.policy import classify_mode
from test_chat import call, script, session, ws  # noqa: F401


def test_escalate_always_asks_except_bypass(tmp_path):
    a = {"cmd": "pip download x", "escalate": True}
    assert classify_mode("run", a, tmp_path, "auto").category == "sandbox_escape"
    assert classify_mode("run", a, tmp_path, "auto").verdict == "ask"
    assert classify_mode("run", a, tmp_path, "ask").verdict == "ask"
    assert classify_mode("run", a, tmp_path, "chat").verdict == "deny"
    assert classify_mode("run", a, tmp_path, "bypass").verdict == "allow"


def test_wrap_builds_the_right_launchers(tmp_path):
    args, _ = sbx.wrap("echo hi", tmp_path, "landlock")
    assert args[1].endswith("landlock_exec.py") and "--no-net" in args and args[-3:] == ["bash", "-c", "echo hi"]
    assert str(tmp_path.resolve()) in args
    assert sbx.wrap("echo hi", tmp_path, "seatbelt")[0] is None


@pytest.mark.skipif(sys.platform != "win32", reason="the wsl backend runs WSL from Windows (drive paths)")
def test_wrap_builds_the_wsl_launcher():
    args, _ = sbx.wrap("echo hi", Path("C:/Users/a b/ws"), "wsl", timeout=9)
    assert args[:3] == ["wsl.exe", "--cd", "/mnt/c/Users/a b/ws"] and ["timeout", "-k", "5", "9"] == args[-7:-3]
    assert args[-1].endswith("echo hi") and "wsl-venv/bin" in args[-1]
    assert sbx.to_wsl(Path("D:/x/y z")) == "/mnt/d/x/y z"


def test_windows_auto_turns_on_after_setup(monkeypatch):
    monkeypatch.setattr(sbx.os, "name", "nt")
    monkeypatch.setattr(sbx.sys, "platform", "win32")
    monkeypatch.setattr(sbx, "_wsl_cmd", lambda: True)
    monkeypatch.setattr(sbx, "_wsl_ok", lambda: True)
    monkeypatch.setattr(sbx, "wsl_has_venv", lambda: False)
    assert sbx.backend("auto") == "" and sbx.backend("wsl") == "wsl" and sbx.backend("on") == "wsl"
    assert "/sandbox setup" in sbx.describe() if sbx.setting() == "auto" else True
    monkeypatch.setattr(sbx, "wsl_has_venv", lambda: True)
    assert sbx.backend("auto") == "wsl" and sbx.backend("off") == ""
    monkeypatch.setattr(sbx, "_wsl_cmd", lambda: False)
    assert sbx.backend("auto") == ""


def test_denial_hint_only_on_failure():
    assert "escalate=true" in sbx.note_if_blocked(1, "bash: /home/a: Permission denied")
    assert sbx.note_if_blocked(0, "Permission denied") == ""
    assert sbx.note_if_blocked(1, "AssertionError") == ""


def test_project_settings_cannot_turn_the_sandbox_off(tmp_path, monkeypatch):
    monkeypatch.setitem(sbx.STATE, "mode", None)
    monkeypatch.delenv("CAP_SANDBOX", raising=False)
    (tmp_path / ".cap").mkdir()
    (tmp_path / ".cap" / "settings.json").write_text('{"sandbox": "off"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert sbx.setting() == "auto"                    # 사용자 설정·환경변수·/sandbox 만 본다
    monkeypatch.setenv("CAP_SANDBOX", "wsl")
    assert sbx.setting() == "wsl"


def _record_sandbox(monkeypatch, backend="landlock"):
    seen = []
    monkeypatch.setattr(sbx, "backend", lambda mode=None: backend)

    def fake_run_shell(cmd, cwd, timeout=180, sandbox=""):
        seen.append((cmd, sandbox))
        return 0, "ok"
    monkeypatch.setattr(T, "run_shell", fake_run_shell)
    monkeypatch.setattr(T, "run_yield", lambda cmd, cwd, sandbox="", limit=30: fake_run_shell(cmd, cwd, 0, sandbox) + ("",))
    import cap.loop as L
    monkeypatch.setattr(L, "run_shell", fake_run_shell)
    return seen


def test_wiring_sandboxed_by_default_unsandboxed_when_human_allows_install(ws, monkeypatch):
    seen = _record_sandbox(monkeypatch)
    script(monkeypatch, [{"tool_calls": [call("run", cmd="ls")]},
                         {"tool_calls": [call("run", cmd="pip install requests")]},
                         {"tool_calls": [call("run", cmd="python fetch.py", escalate=True)]},
                         {"tool_calls": [call("run_tests")]},
                         {"content": "끝"}])
    s, _ = session(ws, approval="auto")
    asked = []
    s.run_turn("해줘", ask=lambda p: asked.append(p) or "y")
    by = dict(seen)
    assert by["ls"] == "landlock"                                   # 승인 없이 돈 명령: 격리 안
    assert by["pip install requests"] == ""                          # 사람이 허락한 설치: 격리 밖
    assert by["python fetch.py"] == ""                               # escalate 를 사람이 허락
    assert any(c.startswith("python -m pytest") and sb == "landlock" for c, sb in seen)   # 테스트·관문도 격리 안
    assert [c["category"] for p in asked for c in p["calls"]] == ["install", "sandbox_escape"]
    s.close()


def test_bypass_runs_without_sandbox(ws, monkeypatch):
    seen = _record_sandbox(monkeypatch)
    script(monkeypatch, [{"tool_calls": [call("run", cmd="ls")]}, {"content": "끝"}])
    s, _ = session(ws, approval="bypass")
    s.run_turn("해줘")
    assert dict(seen)["ls"] == ""
    s.close()


@pytest.fixture(scope="module")
def real_backend():
    sbx.STATE["mode"] = "wsl" if sys.platform == "win32" else "landlock"
    b = sbx.backend()
    sbx.STATE["mode"] = "off"
    if not b:
        pytest.skip("no working sandbox on this machine (Linux Landlock or WSL)")
    return b


def test_real_sandbox_blocks_outside_writes_and_network(real_backend, tmp_path):
    ws = tmp_path
    code, out = T.run_shell("echo hi > a.txt && cat a.txt", ws, timeout=60, sandbox=real_backend)
    assert code == 0 and "hi" in out
    code, out = T.run_shell("echo x > ~/cap_sandbox_escape_test.txt", ws, timeout=60, sandbox=real_backend)
    assert code != 0 and "[sandbox]" in out
    code, out = T.run_shell("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 3)\"", ws,
                            timeout=60, sandbox=real_backend)
    assert code != 0
    code, out = T.run_shell("rm -rf ../cap_sb_should_not_exist_* /etc/hostname", ws, timeout=60, sandbox=real_backend)
    assert code != 0
