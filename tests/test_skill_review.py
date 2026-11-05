"""save_skill(모델이 절차를 스킬로), `# 기억` 단축, /review 대상(세션·커밋 안 한 것·브랜치)."""
import subprocess

from cap import chat as C
from cap import memory as M
from cap.policy import classify_mode
from test_chat import call, script, session, ws  # noqa: F401


def test_save_skill_asks_and_is_listed_next_session(ws, monkeypatch):
    import json
    args = {"name": "Run Release", "description": "how to cut a release", "steps": "1. bump version\n2. run tests\n3. tag"}
    tc = {"id": "c_skill", "type": "function", "function": {"name": "save_skill", "arguments": json.dumps(args)}}
    script(monkeypatch, [{"tool_calls": [tc]},
                         {"content": "저장했습니다."}])
    s, _ = session(ws, approval="auto")
    asked = []
    s.run_turn("이 절차 스킬로 저장해줘", ask=lambda p: asked.append(p) or "y")
    assert asked and asked[0]["calls"][0]["category"] == "memory"          # auto 에서도 사람에게
    sk = M.find_skills(ws)
    assert "run-release" in sk and sk["run-release"]["description"] == "how to cut a release"
    s.close()
    assert classify_mode("save_skill", {}, ws, "chat").verdict == "deny"
    assert classify_mode("save_skill", {}, ws, "bypass").verdict == "allow"
    assert not M.save_skill(ws, "x", "", "steps")[0]


def test_hash_line_goes_to_project_memory(ws):
    assert C.quick_memory(ws, "use tabs in this repo").startswith("✓ Remembered")
    assert "use tabs in this repo" in (ws / ".cap" / "memory.md").read_text(encoding="utf-8")


def _git(ws, *a):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=ws, capture_output=True,
                          text=True, encoding="utf-8")


def test_review_targets(ws, monkeypatch):
    s, out = session(ws)
    _, _, text = C.handle_command(s, ws, "/review uncommitted")
    assert text is None and any("not a git repository" in ln for ln in out)
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")
    _git(ws, "checkout", "-qb", "feature")
    (ws / "m.py").write_text("def double(x):\n    return 2 * x\n", encoding="utf-8")
    (ws / "new.py").write_text("x = 1\n", encoding="utf-8")
    _, _, text = C.handle_command(s, ws, "/review uncommitted")
    assert "uncommitted changes" in text and "+    return 2 * x" in text and "untracked files: new.py" in text
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat")
    branch = _git(ws, "branch", "--list").stdout
    base = "master" if "master" in branch else "main"
    _, _, text = C.handle_command(s, ws, f"/review {base}")
    assert f"compared with {base}" in text and "+    return 2 * x" in text and s.turn_mode == "chat"
    s.turn_mode = None
    _, _, text = C.handle_command(s, ws, "/review nope-branch")
    assert text is None and any("no such branch or commit" in ln for ln in out)
    s.close()
