"""백그라운드 작업: 자기 git worktree·브랜치에서 돌고, 끝나면 결과·diff 를 보고, 정리한다(실제 모델 대신 작은 스크립트)."""
import json
import subprocess
import sys
import time
from pathlib import Path

from cap import tasks as T


def _git(ws, *a):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=ws, capture_output=True,
                          text=True).stdout.strip()


FAKE = '''import json, pathlib
pathlib.Path("feature.txt").write_text("made by the task\\n")
print(json.dumps({"status": "done", "answer": "added feature.txt", "changed": ["feature.txt"],
                  "verify_note": "harness ran tests -> 1 passed", "calls": 3, "seconds": 1.0, "session": "x"}))
'''


def test_task_runs_in_its_own_worktree(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "a.txt").write_text("a\n")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")
    (tmp_path / "fake.py").write_text(FAKE)
    ok, msg = T.start(ws, "add a feature", command=f'"{Path(sys.executable).as_posix()}" "{(tmp_path / "fake.py").as_posix()}"')
    assert ok and "branch cap/task-1" in msg
    for _ in range(100):
        lines = T.describe(ws)
        if "[finished]" in lines[0]:
            break
        time.sleep(0.1)
    text = "\n".join(lines)
    assert "[finished]" in text and "changed feature.txt" in text and "1 passed" in text
    assert not (ws / "feature.txt").exists()                       # 지금 폴더는 그대로
    assert "+made by the task" in T.diff(ws, 1)
    msg = T.commit(ws, 1)
    assert msg.startswith("committed on cap/task-1") and "git merge cap/task-1" in msg
    _git(ws, "merge", "-q", "cap/task-1")
    assert (ws / "feature.txt").read_text() == "made by the task\n"
    assert "cap/task-1" in _git(ws, "branch", "--list")
    assert T.remove(ws, 1).startswith("removed task 1")
    assert "cap/task-1" not in _git(ws, "branch", "--list") and not (ws / ".cap" / "worktrees" / "task-1").exists()


def test_task_needs_git(tmp_path):
    ok, msg = T.start(tmp_path, "x")
    assert not ok and "git" in msg


def test_tui_tells_when_a_task_finishes(tmp_path, monkeypatch):
    import asyncio
    from cap import tui
    from cap.tui import plain_text
    ws = tmp_path / "repo"
    ws.mkdir()
    done = [(3, {"status": "done", "verify_note": "harness ran tests -> 2 passed"})]
    calls = {"n": 0}

    def fake_finished(_ws):
        calls["n"] += 1
        return [] if calls["n"] == 1 else done          # 시작 때는 없고, 나중에 끝남
    monkeypatch.setattr(T, "finished", fake_finished)

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.2)
            app._check_tasks()
            await pilot.pause(0.2)
            text = "\n".join(plain_text(c.content) for c in app.log_view.children if hasattr(c, "content"))
            assert "Background task 3 finished" in text and "2 passed" in text
            app._check_tasks()                           # 두 번 알리지 않는다
            await pilot.pause(0.1)
            text2 = "\n".join(plain_text(c.content) for c in app.log_view.children if hasattr(c, "content"))
            assert text2.count("Background task 3 finished") == 1
    asyncio.run(go())
