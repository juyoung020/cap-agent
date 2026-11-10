"""일반 코딩 에이전트(대화형) 모드를 가짜 모델 대본으로 돌린다. API 호출 없음."""

import builtins
import sys
import json

import pytest

from cap import chat as C
from cap import cli
from cap import compact as cmp
from cap import loop as L
from cap.policy import classify_mode, is_safe_command

BUGGY = "def double(x):\n    return x + x + 1\n"
TEST = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from m import double\n\ndef test_double():\n    for i in range(5):\n        assert double(i) == 2 * i\n")


def call(name, **args):
    return {"id": f"c_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**6}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def script(monkeypatch, replies):
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = next(it)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "m.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(TEST, encoding="utf-8")
    return tmp_path


def session(ws, approval="ask"):
    out = []
    ui = C.UI(out=out.append)
    ui.tty = ui.color = False
    return C.ChatSession(ws, None, approval, ui), out


def answers(*xs):
    it = iter(xs)
    return lambda payload: next(it)


FIX = [{"tool_calls": [call("read_file", path="m.py")]},
       {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
       {"content": "double 을 고쳤습니다."}]


def test_two_turns_keep_context_and_approval_resume(ws, monkeypatch):
    seen = script(monkeypatch, FIX + [{"content": "방금 m.py 의 double 에서 +1 을 뺐습니다."}])
    s, out = session(ws)
    asked = []
    r1 = s.run_turn("double 고쳐줘", ask=lambda p: asked.append(p) or "y")
    assert asked and asked[0]["calls"][0]["tool"] == "edit_file"          # ask 모드: 편집은 묻는다
    assert any("+    return x + x" in ln for ln in asked[0]["calls"][0]["preview"])   # 승인 질문에 diff 미리보기
    assert (ws / "m.py").read_text() == "def double(x):\n    return x + x\n"
    assert r1["values"]["status"] == "done" and r1["values"]["turn_changed"] == ["m.py"]
    assert any("Verified" in ln and "1 passed" in ln for ln in out)   # 하네스가 테스트를 직접 돌림
    assert any(ln.strip().startswith("• edit m.py +1 -1") for ln in out)  # 진행 표시 한 줄
    r2 = s.run_turn("방금 뭘 고쳤어?")
    last = seen[-1]
    texts = [m.get("content") or "" for m in last]
    assert any("double 고쳐줘" in t for t in texts) and any("double 을 고쳤습니다." in t for t in texts)
    assert r2["values"]["turn_no"] == 2 and s.last_answer(r2["values"]).startswith("방금 m.py")
    s.close()


def test_always_answer_skips_later_edit_questions(ws, monkeypatch):
    script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x  # a")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x  # a", new="return x + x")]},
        {"content": "ok"}])
    s, _ = session(ws)
    asked = []
    s.run_turn("fix", ask=lambda p: asked.append(p) or "a")
    assert len(asked) == 1 and (ws / "m.py").read_text().endswith("return x + x\n")
    s.close()


def test_deny_with_feedback_goes_to_model(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return 2 * x")]},
        {"content": "알겠습니다, 고치지 않겠습니다."}])
    s, _ = session(ws)
    r = s.run_turn("fix", ask=answers("아직 고치지 말고 설명만 해"))
    assert "x + x + 1" in (ws / "m.py").read_text()
    assert any(m["role"] == "tool" and "설명만 해" in m["content"] for m in seen[-1])
    assert r["values"]["status"] == "done"
    s.close()


def test_auto_mode_does_not_ask_edits_or_commands_but_asks_risky(ws, monkeypatch):
    (ws / "junk.txt").write_text("x", encoding="utf-8")
    script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"tool_calls": [call("run", cmd="python m.py")]},
        {"tool_calls": [call("run", cmd="rm junk.txt")]},
        {"content": "done"}])
    s, _ = session(ws, approval="auto-edit")          # 옛 이름은 auto 의 별칭
    assert s.approval["mode"] == "auto"
    asked = []
    s.run_turn("fix", ask=lambda p: asked.append(p) or "n")
    assert [c["args"]["cmd"] for p in asked for c in p["calls"]] == ["rm junk.txt"]
    s.close()


def test_undo_restores_files_and_drops_turn(ws, monkeypatch):
    script(monkeypatch, FIX)
    s, _ = session(ws)
    s.run_turn("double 고쳐줘", ask=answers("y"))
    assert "x + x + 1" not in (ws / "m.py").read_text()
    msg = s.undo()
    assert "m.py" in msg and (ws / "m.py").read_text() == BUGGY
    v = s.values()
    assert not v["turn_log"] and [m["role"] for m in v["messages"]] == ["system"]
    s.close()


def test_resume_session_from_new_process_object(ws, monkeypatch):
    script(monkeypatch, FIX + [{"content": "이어서 답함"}])
    s, _ = session(ws)
    s.run_turn("double 고쳐줘", ask=answers("y"))
    sid = s.sid
    s.close()
    s2, _ = session(ws)
    s2.close()
    assert C._pick_session(ws, "") == s2.sid       # 가장 최근
    s3 = C.ChatSession(ws, sid, "ask", s2.ui)
    assert s3.values()["turn_no"] == 1
    r = s3.run_turn("계속")
    assert r["values"]["turn_no"] == 2
    s3.close()


def test_preexisting_failure_is_not_blamed_on_this_turn(ws, monkeypatch):
    (ws / "other.py").write_text("X = 1\n", encoding="utf-8")
    script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="other.py", old="X = 1", new="X = 2")]},
        {"content": "X 를 2로 바꿨습니다."},          # 하네스: 실패는 턴 전부터 있던 것 → 한 번 알림
        {"content": "test_double 실패는 원래 있던 것이라 그대로 둡니다."}])
    s, out = session(ws, approval="auto-edit")
    r = s.run_turn("other.py 의 X 를 2로")
    v = r["values"]
    assert v["status"] == "done" and v["base_failures"] and "pre-existing failures" in v["verify_note"]
    s.close()


def test_interrupted_turn_is_marked_and_calls_paired(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "새 요청 처리"}])
    s, _ = session(ws)
    # 승인 대기에서 끊긴 상태(사용자가 Ctrl+C)를 만든다: interrupt 까지만 진행
    s.app.invoke({"goal": "fix"}, s.cfg)
    assert s.pending()
    r = s.run_turn("그만하고 설명만 해")
    last = seen[-1]
    assert any("interrupted the previous turn" in (m.get("content") or "") for m in last)
    tool_ids = {m["tool_call_id"] for m in last if m["role"] == "tool"}
    call_ids = {tc["id"] for m in last if m.get("tool_calls") for tc in m["tool_calls"]}
    assert call_ids <= tool_ids                       # 짝이 맞춰져 있다(Codex normalize)
    assert r["values"]["status"] == "done" and "x + x + 1" in (ws / "m.py").read_text()
    s.close()


def test_auto_compaction_when_history_is_large(ws, monkeypatch):
    monkeypatch.setattr(cmp, "COMPACT_AT", 50)
    seen = script(monkeypatch, [{"content": "첫 답"}, {"content": "1. 진행 상황과 결정: 없음"}, {"content": "둘째 답"}])
    s, out = session(ws)
    s.run_turn("첫 질문")
    r = s.run_turn("두 번째 질문")
    msgs = r["values"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert "이전 대화 요약" in msgs[1]["content"] and "첫 질문" in msgs[1]["content"] and "두 번째 질문" in msgs[1]["content"]
    assert any("Compacted context" in ln for ln in out)
    s.close()


def test_repl_slash_commands(ws, monkeypatch):
    script(monkeypatch, FIX)
    lines = iter(["double 고쳐줘", "y", "/status", "/diff", "/mode auto", "/undo", "/diff", "/bogus",
                  "/clear", "/resume", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))
    printed = []
    monkeypatch.setattr(C, "_print", lambda s="": printed.append(s))
    assert C.repl(ws) == 0
    text = "\n".join(printed)
    assert "CAP  Code As Planned" in text and "mode         ask" in text
    assert "-    return x + x + 1" in text and "+    return x + x" in text      # /diff
    assert "mode: auto" in text
    assert "↩ Undid" in text and "No files changed in this session." in text   # /undo 뒤 /diff
    assert "Unknown command: /bogus" in text and "New conversation: session" in text
    assert "1) " in text                                                       # /resume 목록
    assert (ws / "m.py").read_text() == BUGGY


def test_typed_text_never_starts_spec_flow(ws, monkeypatch):
    script(monkeypatch, [{"content": "안녕하세요"}])
    monkeypatch.setattr(cli.specmod, "draft", lambda *a, **k: pytest.fail("스펙 초안을 쓰면 안 된다"))
    lines = iter(["안녕", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))
    monkeypatch.setattr(C, "_print", lambda s="": None)
    assert C.repl(ws) == 0


def test_main_dispatch(ws, monkeypatch):
    got = []
    monkeypatch.setattr(C, "one_shot", lambda w, t, approval="ask", as_json=False, schema=None, events=False, resume=None, max_turns=None, out_file=None: got.append(("one", t, approval) + (("json",) if as_json else ())) or 0)
    monkeypatch.setattr(C, "repl", lambda w, approval="ask", resume=None: got.append(("repl", approval, resume)) or 0)
    monkeypatch.chdir(ws)
    cli.main(["mean 에 docstring 달아줘"])
    cli.main(["exec", "fix", "it"])
    cli.main(["-a", "auto-edit", "do"])
    cli.main(["--mode", "plan", "do"])
    cli.main(["--resume"])
    cli.main([])
    assert got == [("one", "mean 에 docstring 달아줘", "ask"), ("one", "fix it", "ask"), ("one", "do", "auto"), ("one", "do", "plan"),
                   ("repl", "ask", "last"), ("repl", "ask", None)]
    assert cli.main(["--approval", "nope", "x"]) == 2
    assert cli.main(["--mode", "full-auto", "--plain"]) == 0 and got[-1] == ("repl", "auto", None)
    cli.main(["exec", "--json", "fix", "it"])
    assert got[-1] == ("one", "fix it", "ask", "json")


def test_one_shot_runs_once(ws, monkeypatch, capsys):
    script(monkeypatch, FIX)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)     # 터미널에서 답하는 사용자
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    assert C.one_shot(ws, "double 고쳐줘") == 0
    out = capsys.readouterr().out
    assert "double 을 고쳤습니다." in out and "> double 고쳐줘" in out
    assert (ws / "m.py").read_text().endswith("return x + x\n")


def test_one_shot_without_answer_denies(ws, monkeypatch, capsys):
    script(monkeypatch, [FIX[1], {"content": "승인을 못 받아 못 고쳤습니다."}])

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", eof)
    C.one_shot(ws, "double 고쳐줘")
    assert (ws / "m.py").read_text() == BUGGY       # 답할 사람이 없으면 거절(fail-closed)


def test_one_shot_without_terminal_declines_without_waiting(ws, monkeypatch, capsys):
    """스크립트·CI 에서 띄운 cap exec(터미널 없음): 표준 입력을 기다리지 않고 거절한다(새 사용자 시험에서 20분 멈춤)."""
    script(monkeypatch, [FIX[1], {"content": "승인을 못 받아 못 고쳤습니다."}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    def never(prompt=""):
        raise AssertionError("must not read stdin")
    monkeypatch.setattr(builtins, "input", never)
    C.one_shot(ws, "double 고쳐줘")
    out = capsys.readouterr().out
    assert "declined (no terminal to ask)" in out and "--mode auto" in out
    assert (ws / "m.py").read_text() == BUGGY


def test_exec_accepts_global_options_after_exec():
    from cap.cli import _hoist_exec_options as h
    assert h(["exec", "--mode", "auto", "고쳐줘"]) == ["--mode", "auto", "exec", "고쳐줘"]
    assert h(["exec", "--json", "--full-auto", "--max-turns", "3", "x"]) == ["--full-auto", "exec", "--json", "--max-turns", "3", "x"]
    assert h(["exec", "resume", "--last", "--mode=auto", "go"]) == ["--mode=auto", "exec", "resume", "--last", "go"]
    assert h(["exec", "고쳐줘", "--mode", "auto"]) == ["exec", "고쳐줘", "--mode", "auto"]     # 할 일 뒤는 그대로
    assert h(["fix", "exec", "bug"]) == ["fix", "exec", "bug"]


def test_project_instructions_loaded(ws, monkeypatch):
    (ws / "AGENTS.md").write_text("항상 한국어 주석을 쓴다.\n" + "x" * 5000, encoding="utf-8")
    seen = script(monkeypatch, [{"content": "ok"}])
    s, _ = session(ws)
    s.run_turn("hi")
    sysmsg = seen[0][0]["content"]
    assert "<INSTRUCTIONS>" in sysmsg and "항상 한국어 주석을 쓴다." in sysmsg and "생략" in sysmsg
    assert len(sysmsg) < 8000
    s.close()


def test_approval_modes_policy(tmp_path):
    ed = {"path": "a.py", "old": "x", "new": "y"}
    assert classify_mode("edit_file", ed, tmp_path, "ask").verdict == "ask"
    assert classify_mode("edit_file", ed, tmp_path, "ask", {"edit"}).verdict == "allow"
    assert classify_mode("edit_file", ed, tmp_path, "auto-edit").verdict == "allow"
    assert classify_mode("run", {"cmd": "python -m pytest -q"}, tmp_path, "ask").verdict == "allow"
    assert classify_mode("run", {"cmd": "python x.py"}, tmp_path, "full-auto").verdict == "allow"
    assert classify_mode("run", {"cmd": "rm junk.txt"}, tmp_path, "full-auto").verdict == "ask"
    assert classify_mode("run", {"cmd": "rm -rf /"}, tmp_path, "full-auto").verdict == "deny"
    assert classify_mode("write_file", {"path": "../out.py", "content": ""}, tmp_path, "full-auto").verdict == "deny"
    assert classify_mode("write_file", {"path": ".env", "content": ""}, tmp_path, "full-auto").verdict == "deny"
    assert classify_mode("write_file", {"path": "AGENTS.md", "content": ""}, tmp_path, "full-auto").verdict == "ask"
    k = classify_mode("run", {"cmd": "python x.py"}, tmp_path, "ask").category
    assert classify_mode("run", {"cmd": "python x.py"}, tmp_path, "ask", {k}).verdict == "allow"


def test_safe_commands():
    for c in ["ls -la", "git status", "git diff HEAD", "python -m pytest -q tests/test_a.py", "cat a.py | head -5",
              "rg divide", "pytest -x 2>&1"]:
        assert is_safe_command(c), c
    for c in ["python x.py", "ls > out.txt", "git commit -m x", "find . -delete", "echo $(rm x)", "npm install"]:
        assert not is_safe_command(c), c


def test_test_helpers():
    assert cmp.is_full_test_run("pytest -q", "python -m pytest -q")
    assert not cmp.is_full_test_run("python -m pytest tests/test_a.py", "python -m pytest -q")
    out = "FAILED tests/test_a.py::test_x - assert 1\n1 failed, 4 passed in 0.12s\n"
    assert cmp.test_summary(out, 1) == "1 failed, 4 passed"
    assert cmp.failing_tests(out, 1) == ["tests/test_a.py::test_x"]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [call("read_file", path="a"), call("search", pattern="b")]},
            {"role": "tool", "tool_call_id": call("read_file", path="a")["id"], "content": "x" * 3000}]
    n = cmp.shrink_old(cmp.normalize(msgs))
    assert [m["role"] for m in n] == ["system", "user", "assistant", "tool", "tool"]
    assert n[4]["content"] == cmp.ABORTED and len(n[3]["content"]) < 500


def test_repl_plan_mode_then_execute(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("todo", items=[{"text": "double 의 +1 제거", "done": False}])]},
        {"content": "계획을 세웠습니다."},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "고쳤습니다."}])
    lines = iter(["/plan double 고쳐", "y", "y", "/quit"])       # 계획 승인 y(승인 모드) → 편집 승인 y
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))
    printed = []
    monkeypatch.setattr(C, "_print", lambda s="": printed.append(s))
    assert C.repl(ws) == 0
    text = "\n".join(printed)
    assert "mode: plan" in text and "mode: ask" in text and "고쳤습니다." in text
    assert "[mode: plan]" in seen[0][-1]["content"] and "[mode: plan]" not in seen[2][-1]["content"]
    assert (ws / "m.py").read_text().endswith("return x + x\n")
