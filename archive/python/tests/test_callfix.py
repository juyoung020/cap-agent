"""작은 모델(9B)의 도구 호출·답 다듬기: 잘못된 도구·인자 이름 고치기, 무효 라운드 한도, '하겠습니다'만 하고 멈춘 답,
/goal 의 진전 없음 멈춤. API 호출 없음(가짜 모델 대본)."""

import json

from cap import loop as L
from test_chat import call, script, session, ws  # noqa: F401

FIXED = "def double(x):\n    return x + x\n"


def tool_msgs(seen):
    return [m["content"] for m in seen[-1] if m["role"] == "tool"]


# ------------------------------------------------------------ 1. 도구·인자 이름 고치기, 무효 라운드 한도
def test_fix_call_aliases_case_and_close_names_only_within_current_tools():
    props = {"read_file": {"path", "offset", "limit"}, "edit_file": {"path", "old", "new", "edits"},
             "run": {"cmd", "background", "escalate"}}
    assert L.fix_call("cat", {"file_path": "a.py"}, props)[:2] == ("read_file", {"path": "a.py"})
    assert L.fix_call("Read_File", {"path": "a.py"}, props)[0] == "read_file"
    assert L.fix_call("read_fil", {"path": "a.py"}, props)[0] == "read_file"
    assert L.fix_call("pytest", {}, props)[0] is None                    # run_tests 가 목록에 없으면 바꾸지 않는다
    name, args, notes = L.fix_call("str_replace_editor", {"command": "str_replace", "path": "m.py",
                                                          "old_str": "a", "new_str": "b"}, props)
    assert (name, args) == ("edit_file", {"path": "m.py", "old": "a", "new": "b"}) and any("ignored" in n for n in notes)
    assert L.fix_call("read_file", {"path": "a.py"}, props) == ("read_file", {"path": "a.py"}, [])


def test_str_replace_with_old_string_new_string_edits_the_file(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("str_replace", path="m.py", old_string="return x + x + 1", new_string="return x + x")]},
        {"content": "double 을 고쳤습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert (ws / "m.py").read_text() == FIXED
    assert r["values"]["status"] == "done" and "passed" in r["values"]["verify_note"]
    assert tool_msgs(seen)[0].startswith("[harness] Corrected your call (tool str_replace → edit_file; old_string → old")
    s.close()


def test_aliased_edit_passes_the_dry_run_before_asking(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old_string="return x + x + 1", new_string="return x + x")]},
        {"content": "고쳤습니다."}])
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("double 고쳐줘", ask=lambda p: asked.append(p) or "y")
    assert asked and asked[0]["calls"][0]["tool"] == "edit_file"        # 'old 가 비었다'로 돌려보내지 않고 묻는다
    assert (ws / "m.py").read_text() == FIXED
    assert not any("old 가 비었다" in t for t in tool_msgs(seen))
    s.close()


def test_bash_with_command_runs(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("bash", command="echo hello-cap")]}, {"content": "출력은 hello-cap."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("echo 해 봐", ask=lambda p: "y")
    out = tool_msgs(seen)[0]
    assert "tool bash → run; command → cmd" in out and "hello-cap" in out.split("\n", 1)[1]
    assert r["values"]["status"] == "done"
    s.close()


def test_edits_as_a_json_string_are_applied(ws, monkeypatch):
    edits = json.dumps([{"old": "return x + x + 1", "new": "return x + x"}])
    seen = script(monkeypatch, [{"tool_calls": [call("edit_file", path="m.py", edits=edits)]}, {"content": "고쳤습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert (ws / "m.py").read_text() == FIXED and r["values"]["status"] == "done"
    assert "edits decoded from a JSON string" in tool_msgs(seen)[0]
    s.close()


def test_three_rounds_of_unknown_tools_escalate(ws, monkeypatch):
    n = {"calls": 0}

    def fake_chat(messages, **kw):
        n["calls"] += 1
        return {"content": "", "tool_calls": [call("frobnicate_widget", x=n["calls"])], "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    asked = []
    r = s.run_turn("뭔가 해줘", ask=lambda p: asked.append(p) or "abort")
    assert n["calls"] <= 4
    assert asked and asked[-1]["kind"] == "escalate" and "could not run" in asked[-1]["reason"]
    assert r["values"]["status"] == "escalated"
    s.close()


def test_a_valid_round_resets_the_invalid_streak(ws, monkeypatch):
    script(monkeypatch, [{"tool_calls": [call("nope_tool")]}, {"tool_calls": [call("nope_tool", a=1)]},
                         {"tool_calls": [call("read_file", path="m.py")]}, {"tool_calls": [call("nope_tool", a=2)]},
                         {"content": "m.py 는 double 하나입니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("봐", ask=lambda p: "abort")
    assert r["values"]["status"] == "done" and r["values"]["invalid_streak"] == 1
    s.close()


# ------------------------------------------------------------ 2. '하겠습니다'만 하고 도구 없이 멈춘 답
def test_announced():
    for t in ("이제 m.py 를 고치겠습니다.", "테스트를 돌려 볼게요", "Let me fix that.", "Now I'll run the tests.",
              "Here is the plan:", "I found it. I will edit m.py next."):
        assert L.announced(t), t
    for t in ("x 는 2 입니다.", "알겠습니다.", "고치지 않겠습니다.", "Fixed double; tests pass.",
              "Done. Let me know if you need more.", ""):
        assert not L.announced(t), t


def test_announce_is_nudged_then_the_edit_happens(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"content": "이제 m.py 를 고치겠습니다."},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "double 을 고쳤고 테스트가 통과합니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert "did not call a tool" in seen[1][-1]["content"]
    assert (ws / "m.py").read_text() == FIXED and r["values"]["status"] == "done"
    assert "do it with a tool call in the same reply" in seen[0][0]["content"]
    s.close()


def test_plain_answer_gets_no_nudge(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": "x 는 2 입니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("x 는 뭐야?", ask=lambda p: "abort")
    assert len(seen) == 1 and r["values"]["status"] == "done"
    s.close()


def test_at_most_one_announce_nudge_per_turn(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": "고치겠습니다."}, {"content": "지금 고치겠습니다."},
                                {"content": "다음 턴: 고치겠습니다."}, {"content": "끝."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("고쳐", ask=lambda p: "abort")
    assert len(seen) == 2 and r["values"]["status"] == "done"
    r = s.run_turn("다시", ask=lambda p: "abort")                         # 새 사용자 턴에는 다시 한 번
    assert len(seen) == 4 and "did not call a tool" in seen[3][-1]["content"]
    s.close()


def test_announce_nudge_can_be_ablated(ws, monkeypatch):
    monkeypatch.setattr(L, "ABLATION", "no_announce")
    seen = script(monkeypatch, [{"content": "이제 m.py 를 고치겠습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert len(seen) == 1 and r["values"]["status"] == "done"
    assert "same reply" not in seen[0][0]["content"]
    s.close()


# ------------------------------------------------------------ 3. /goal: 진전 없으면 멈춤
def test_goal_stops_when_nothing_changes_and_the_gate_fails_the_same_way(ws, monkeypatch):
    n = {"agent": 0, "judge": 0}

    def fake_chat(messages, **kw):
        if kw.get("response_format"):
            n["judge"] += 1
            return {"content": '{"verdict": "continue", "reason": "keep going"}', "tool_calls": [],
                    "finish_reason": "stop", "usage": {}, "budget_actions": [], "seconds": 0}
        n["agent"] += 1
        return {"content": "m.py 를 살펴봤습니다.", "tool_calls": [], "finish_reason": "stop", "usage": {},
                "budget_actions": [], "seconds": 0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    notes = []
    end = s.run_goal("make the tests pass", on_note=notes.append, max_turns=8)
    assert end.startswith("goal blocked: no progress") and "failed the same way" in end
    assert sum("Goal turn" in x for x in notes) == 3 and n["agent"] == 3 and n["judge"] == 0
    s.close()


def test_goal_stops_when_the_judge_keeps_saying_continue_without_changes(ws, monkeypatch):
    (ws / "m.py").write_text(FIXED, encoding="utf-8")
    n = {"agent": 0}

    def fake_chat(messages, **kw):
        if kw.get("response_format"):
            return {"content": '{"verdict": "continue", "reason": "not sure yet"}', "tool_calls": [],
                    "finish_reason": "stop", "usage": {}, "budget_actions": [], "seconds": 0}
        n["agent"] += 1
        return {"content": "확인했습니다.", "tool_calls": [], "finish_reason": "stop", "usage": {},
                "budget_actions": [], "seconds": 0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    end = s.run_goal("refactor", max_turns=8)
    assert end.startswith("goal blocked: no progress") and n["agent"] == 3
    s.close()
