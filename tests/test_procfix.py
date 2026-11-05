"""명령·프로세스 실행: 프로젝트 파이썬, 조용한 환경, 백그라운드 새 출력만, 긴 run 은 백그라운드로."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from cap import extras as ext
from cap import tools as T

OWN = str(Path(sys.executable).parent)


def fake_venv(root: Path, name: str = ".venv") -> Path:
    d = root / name / ("Scripts" if os.name == "nt" else "bin")
    d.mkdir(parents=True)
    exe = d / ("python.exe" if os.name == "nt" else "python")
    exe.write_text("")
    exe.chmod(0o755)
    return d


@pytest.fixture(autouse=True)
def no_outer_venv(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


# ---------------------------------------------------------------- 1. 프로젝트 파이썬

def test_project_venv_first_and_cap_last_on_path(tmp_path):
    d = fake_venv(tmp_path)
    assert T.project_python_dir(tmp_path) == d.absolute()
    parts = T.shell_env(tmp_path)["PATH"].split(os.pathsep)
    assert parts[0] == str(d.absolute()) and parts[-1] == OWN


def test_venv_folder_named_venv_and_virtual_env(tmp_path, monkeypatch):
    d = fake_venv(tmp_path, "venv")
    assert T.project_python_dir(tmp_path) == d.absolute()
    other = tmp_path / "elsewhere"
    e = fake_venv(other, "env")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("VIRTUAL_ENV", str(other / "env"))
    assert T.project_python_dir(ws) == e.absolute()


def test_no_venv_keeps_todays_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "X" + os.pathsep + "Y")
    assert T.project_python_dir(tmp_path) is None
    assert T.shell_env(tmp_path)["PATH"] == OWN + os.pathsep + "X" + os.pathsep + "Y"
    assert T.shell_env()["PATH"] == OWN + os.pathsep + "X" + os.pathsep + "Y"


def test_settings_override_file_or_folder(tmp_path):
    fake_venv(tmp_path)                                 # 설정이 .venv 보다 먼저
    d = fake_venv(tmp_path / "tools", "py")
    exe = next(d.iterdir())
    (tmp_path / ".cap").mkdir()
    s = tmp_path / ".cap" / "settings.json"
    s.write_text(json.dumps({"python": str(exe)}), encoding="utf-8")
    assert T.project_python_dir(tmp_path) == d.absolute()
    s.write_text(json.dumps({"python": "tools/py"}), encoding="utf-8")       # 상대 경로·venv 뿌리 폴더
    assert T.project_python_dir(tmp_path) == d.absolute()
    s.write_text(json.dumps({"python": "nowhere/python"}), encoding="utf-8")  # 없는 경로면 자동 탐지로
    assert T.project_python_dir(tmp_path) == (tmp_path / ".venv" / d.name).absolute()


def test_all_shell_paths_use_the_same_order(tmp_path, monkeypatch):
    d = fake_venv(tmp_path)
    seen = []
    from cap import sandbox as sb
    monkeypatch.setattr(sb, "wrap", lambda inner, cwd, b, timeout=None: (seen.append(inner) or ["x"], ""))
    T.shell_args("python -V", tmp_path, "appcontainer")
    assert seen[-1].startswith(f'chcp 65001 >nul && set "PATH={d.absolute()};%PATH%;{OWN}"')
    if T.find_bash():
        T.shell_args("python -V", tmp_path, "landlock")
        assert seen[-1].startswith(f'export PATH="{T._bash_path(str(d.absolute()))}:$PATH:{T._bash_path(OWN)}"')
        args, shell, err = T.shell_args("python -V", tmp_path)
        assert args[-1] == seen[-1] and not shell and not err


@pytest.mark.skipif(not T.find_bash(), reason="needs bash")
def test_bash_finds_project_python_first(tmp_path):
    fake_venv(tmp_path)
    code, out = T.run_shell("command -v python", tmp_path, timeout=30)
    assert code == 0 and ".venv" in out


def test_doctor_says_which_python(tmp_path):
    from cap import chat as C
    assert any("python" in ln and "CAP's own" in ln for ln in C.doctor_lines(tmp_path, "x", ping=False))
    fake_venv(tmp_path)
    assert any("project's python" in ln for ln in C.doctor_lines(tmp_path, "x", ping=False))


# ---------------------------------------------------------------- 2. 조용한 환경

def test_quiet_noninteractive_env(monkeypatch):
    for k, v in {"FORCE_COLOR": "1", "CLICOLOR_FORCE": "1", "TERM": "xterm-256color", "COLORTERM": "truecolor",
                 "PAGER": "less", "MY_API_TOKEN": "s3cret-value"}.items():
        monkeypatch.setenv(k, v)
    env = T.shell_env()
    assert {k: env[k] for k in ("NO_COLOR", "TERM", "PAGER", "GIT_PAGER", "GH_PAGER", "COLORTERM")} == {
        "NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat", "COLORTERM": ""}
    assert "FORCE_COLOR" not in env and "CLICOLOR_FORCE" not in env and "MY_API_TOKEN" not in env


@pytest.mark.skipif(not T.find_bash(), reason="needs bash")
def test_shell_sees_dumb_term(tmp_path):
    code, out = T.run_shell("echo $TERM", tmp_path, timeout=30)
    assert code == 0 and out.strip() == "dumb"


# ---------------------------------------------------------------- 3. 백그라운드: 새 출력만, 기다림

def _wait_exit(ws, pid, s=20):
    v = ext._find(ws, pid)
    end = time.time() + s
    while v["proc"].poll() is None and time.time() < end:
        time.sleep(0.05)


def test_model_output_returns_only_new_output(tmp_path):
    code = "import time; print('first-line', flush=True); time.sleep(1.5); print('second-line', flush=True); time.sleep(5)"
    pid = ext.bg_start(tmp_path, f'"{sys.executable}" -u -c "{code}"')
    try:
        ok, one = ext.bg_output_new(tmp_path, pid, wait=15)
        assert ok and "first-line" in one and "running" in one and "new chars" in one and "total" in one
        if "second-line" not in one:
            ok, two = ext.bg_output_new(tmp_path, pid, wait=15)
            assert "second-line" in two and "first-line" not in two
        ok, three = ext.bg_output_new(tmp_path, pid, wait=0)
        assert "first-line" not in three and "(no new output)" in three
        assert "first-line" in ext.bg_output(tmp_path, pid)[1]           # 사람용·직접 호출은 꼬리 그대로
    finally:
        ext.bg_kill_all(tmp_path)


def test_model_output_on_exited_process_returns_at_once(tmp_path):
    pid = ext.bg_start(tmp_path, f'"{sys.executable}" -c "print(123)"')
    try:
        _wait_exit(tmp_path, pid)
        t0 = time.monotonic()
        ok, out = ext.bg_output_new(tmp_path, pid, wait=2)
        assert ok and "123" in out and "exited 0" in out
        ok, again = ext.bg_output_new(tmp_path, pid, wait=2)
        assert "123" not in again and time.monotonic() - t0 < 1.5
        assert ext.bg_output_new(tmp_path, "bg99")[0] is False
    finally:
        ext.bg_kill_all(tmp_path)


def test_utf8_cut_keeps_partial_characters_for_later():
    b = "가나".encode("utf-8")
    assert ext.utf8_cut(b) == len(b) and ext.utf8_cut(b[:-1]) == 3 and ext.utf8_cut(b"ab") == 2


def test_schemas_have_wait_and_timeout():
    assert "wait" in ext.PROCESS_SCHEMA["function"]["parameters"]["properties"]
    run = next(s for s in T.TOOL_SCHEMAS if s["function"]["name"] == "run")
    assert "timeout" in run["function"]["parameters"]["properties"]


# ---------------------------------------------------------------- 4. 긴 run 은 백그라운드로

def test_long_run_moves_to_background(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "YIELD_S", 1)
    box = T.ToolBox(tmp_path)
    t0 = time.monotonic()
    res = box.t_run(f'"{sys.executable}" -u -c "import time; print(\'serving\', flush=True); time.sleep(3)"')
    try:
        assert time.monotonic() - t0 < 2.8
        assert res["ok"] and res["exit"] is None and "still running after 1s as bg" in res["output"]
        pid = res["background"]
        assert pid in ext.bg_list(tmp_path) and f'id="{pid}"' in res["output"]
        _wait_exit(tmp_path, pid)
        ok, out = ext.bg_output_new(tmp_path, pid, wait=5)
        assert "exited 0" in out
        assert ("serving" in res["output"]) != ("serving" in out)       # 보여 준 부분 출력은 다시 보내지 않는다
    finally:
        ext.bg_kill_all(tmp_path)


def test_quick_run_same_output_and_changed_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "YIELD_S", 20)
    box = T.ToolBox(tmp_path)
    cmd = f'"{sys.executable}" -c "print(\'hi 한글\'); open(\'a.txt\', \'w\').write(\'x\')"'
    res = box.t_run(cmd)
    code, out = T.run_shell(cmd, tmp_path)
    assert res["output"] == f"[exit {code}]\n{out}" and res["exit"] == 0 and res["ok"]
    assert res["changed"] == ["a.txt"]
    assert ext.bg_list(tmp_path) == "(no background processes)"
    assert not list((tmp_path / ".cap" / "bg").glob("*.log"))


def test_run_keeps_exit_code_timeout_arg_and_no_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "YIELD_S", 1)
    box = T.ToolBox(tmp_path)
    res = box.t_run(f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    assert res["exit"] == 3 and not res["ok"] and res["output"].startswith("[exit 3]")
    res = box.t_run(f'"{sys.executable}" -c "import time; time.sleep(2); print(\'done\')"', timeout=20)
    assert res["exit"] == 0 and "done" in res["output"]               # timeout 이 그 호출만 기다림을 늘린다
    t0 = time.monotonic()
    res = box.t_run(f'"{sys.executable}" -c "input()"', timeout=20)
    assert res["exit"] == 1 and "EOFError" in res["output"] and time.monotonic() - t0 < 10   # 입력 없음 = 바로 EOF
    ext.bg_kill_all(tmp_path)
