"""실시간 출력(스트리밍) · 턴 중간 요약 압축 · 목표 모드(/goal) — API 호출 없음."""

import json
from types import SimpleNamespace as NS

from cap import llm
from cap import loop as L
from test_chat import call, session, ws  # noqa: F401  (ws 는 fixture)


def test_streaming_assembles_text_and_tool_calls(monkeypatch):
    chunks = [
        NS(usage=None, choices=[NS(delta=NS(content="Let me ", tool_calls=None), finish_reason=None)]),
        NS(usage=None, choices=[NS(delta=NS(content="read it.", tool_calls=None), finish_reason=None)]),
        NS(usage=None, choices=[NS(delta=NS(content=None, tool_calls=[
            NS(index=0, id="c1", function=NS(name="read_file", arguments='{"pa'))]), finish_reason=None)]),
        NS(usage=None, choices=[NS(delta=NS(content=None, tool_calls=[
            NS(index=0, id=None, function=NS(name=None, arguments='th": "m.py"}'))]), finish_reason="tool_calls")]),
        NS(usage=NS(model_dump=lambda: {"prompt_tokens": 50, "completion_tokens": 9}), choices=[]),
    ]

    class Stream(list):
        def close(self):
            pass

    fake = NS(chat=NS(completions=NS(create=lambda **kw: Stream(chunks) if kw.get("stream") else None)))
    monkeypatch.setattr(llm, "client", lambda: fake)
    seen = []
    r = llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "read_file"}}],
                 on_text=seen.append)
    assert r["content"] == "Let me read it." and seen[-1] == "Let me read it."
    assert r["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "m.py"}'}
    assert r["finish_reason"] == "tool_calls" and r["usage"]["prompt_tokens"] == 50


def test_midturn_compaction_summarizes_earlier_steps(ws, monkeypatch):
    monkeypatch.setattr(llm, "CONTEXT_WINDOW", 1200)
    big = "x = 1  # " + "pad " * 200 + "\n"
    (ws / "big.py").write_text(big * 3, encoding="utf-8")
    calls = {"n": 0}
    log = []

    def fake_chat(messages, **kw):
        log.append([dict(m) for m in messages])
        if messages[0]["content"].startswith("You compress"):
            return {"content": "1) read big.py three times\n4) left: answer", "tool_calls": [], "finish_reason": "stop",
                    "usage": {}, "budget_actions": [], "seconds": 0}
        calls["n"] += 1
        if calls["n"] <= 7:
            r = {"tool_calls": [call("search", pattern=f"pad{calls['n']}")] if calls["n"] > 1 else [call("read_file", path="big.py")]}
        else:
            r = {"content": "끝"}
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, out = session(ws, "auto")
    s.run_turn("big.py 봐")
    assert any(m[0]["content"].startswith("You compress") for m in log)
    later = [m for m in log if not m[0]["content"].startswith("You compress")][-1]
    assert any(x["role"] == "user" and "Summary of your earlier steps" in x["content"] for x in later)
    assert any("Compacted context" in ln for ln in out)
    s.close()


def test_goal_loop_uses_the_test_gate_then_the_judge(ws, monkeypatch):
    replies = iter([
        {"content": "봤는데 아직 안 고쳤습니다."},                                  # 1턴: 고치지 않음 → 관문 실패
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "double 을 고쳤고 테스트가 통과합니다."},                         # 2턴: 고침 → 관문 통과 → 심판
    ])
    prompts = []

    def fake_chat(messages, **kw):
        if kw.get("response_format"):
            prompts.append(messages[-1]["content"])
            return {"content": json.dumps({"verdict": "done", "reason": "tests pass and double is fixed"}),
                    "tool_calls": [], "finish_reason": "stop", "usage": {}, "budget_actions": [], "seconds": 0}
        prompts.append(messages[-1]["content"])
        r = next(replies)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    notes = []
    end = s.run_goal("make the tests pass", on_note=notes.append)
    assert end.startswith("goal done in 2 turn(s)")
    assert any("Gate: python -m pytest -q failed" in n for n in notes) and any("Judge: done" in n for n in notes)
    assert any("quality gate" in p and "must pass" in p for p in prompts)
    s.close()


def test_goal_stops_when_judge_says_blocked(ws, monkeypatch):
    (ws / "m.py").write_text("def double(x):\n    return x + x\n", encoding="utf-8")

    def fake_chat(messages, **kw):
        if kw.get("response_format"):
            return {"content": '{"verdict": "blocked", "reason": "needs the user to choose"}', "tool_calls": [],
                    "finish_reason": "stop", "usage": {}, "budget_actions": [], "seconds": 0}
        return {"content": "어느 쪽으로 할지 정해 주세요.", "tool_calls": [], "finish_reason": "stop", "usage": {},
                "budget_actions": [], "seconds": 0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    assert s.run_goal("refactor") == "goal blocked: needs the user to choose"
    s.close()
