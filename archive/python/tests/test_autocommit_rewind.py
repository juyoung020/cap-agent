"""/autocommit (Aider 방식)와 /rewind 의 code·chat 선택."""
import subprocess

from cap import chat as C
from test_chat import BUGGY, FIX, script, session, ws  # noqa: F401


def git(ws, *a):
    return subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def test_autocommit_commits_only_cap_files_after_verified_turn(ws, monkeypatch):
    git(ws, "init", "-q")
    git(ws, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(ws, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (ws / "notes.txt").write_text("my own work", encoding="utf-8")       # 사람 파일은 커밋하지 않는다
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    script(monkeypatch, FIX)
    s, out = session(ws, approval="auto")
    C.handle_command(s, ws, "/autocommit on")
    s.run_turn("double 고쳐줘")
    assert any(ln.startswith("✓ committed") and "cap: double 고쳐줘" in ln for ln in out)
    assert git(ws, "show", "--name-only", "--format=%s", "HEAD").splitlines() == ["cap: double 고쳐줘", "", "m.py"]
    assert "notes.txt" in git(ws, "status", "--porcelain")
    s.close()


def test_autocommit_skips_failed_or_non_git(ws, monkeypatch):
    script(monkeypatch, FIX)
    s, out = session(ws, approval="auto")
    s.autocommit = True
    s.run_turn("double 고쳐줘")
    assert any("not a git repository" in ln for ln in out)
    assert s.auto_commit({"status": "done", "turn_changed": ["m.py"], "verify_note": "1 failed"}) == ""
    s.close()


def test_rewind_code_only_and_chat_only(ws, monkeypatch):
    script(monkeypatch, [{"content": "첫 답"}] + FIX + [{"content": "첫 답"}] + FIX)
    s, _ = session(ws, approval="auto")
    s.run_turn("설명해줘")
    s.run_turn("double 고쳐줘")
    msg = s.rewind(2, "code")
    assert "files only" in msg and (ws / "m.py").read_text(encoding="utf-8") == BUGGY
    v = s.values()
    assert any("double 고쳐줘" in str(m.get("content")) for m in v["messages"])       # 대화는 남음
    assert "restored the files" in v["carry_note"]
    s2, _ = session(ws, approval="auto")
    s2.run_turn("설명해줘")
    s2.run_turn("double 고쳐줘")
    msg = s2.rewind(2, "chat")
    assert "conversation only" in msg and "return x + x\n" in (ws / "m.py").read_text(encoding="utf-8")   # 파일은 남음
    assert not any("double 고쳐줘" in str(m.get("content")) for m in s2.values()["messages"])
    s.close()
    s2.close()
