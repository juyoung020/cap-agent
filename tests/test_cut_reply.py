"""B16: 출력 한도에서 잘린 답(도구 호출 없음)은 '끝냈다'로 보지 않고, 잘린 생각을 남긴 채 바로 도구로 하라고 이어 간다."""
from cap import loop as L
from test_chat import call, session, ws  # noqa: F401


def _script(monkeypatch, replies):
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = next(it)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []),
                "finish_reason": r.get("finish", "stop"), "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


def test_cut_reply_is_continued_not_treated_as_done(ws, monkeypatch):
    seen = _script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"content": "분석: +1 이 문제다. 그러니 return 을 고치면", "finish": "length"},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "고쳤습니다."}])
    s, _ = session(ws, approval="auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    v = r["values"]
    assert v["status"] == "done" and "passed" in v["verify_note"]
    third = seen[2]
    assert third[-2] == {"role": "assistant", "content": "분석: +1 이 문제다. 그러니 return 을 고치면"}
    assert "cut off at the output limit" in third[-1]["content"]
    s.close()


def test_cut_retries_are_limited(ws, monkeypatch):
    _script(monkeypatch, [{"content": "생각 1", "finish": "length"}, {"content": "생각 2", "finish": "length"},
                          {"content": "생각 3", "finish": "length"}, {"content": "끝"}, {"content": "끝"}])
    s, _ = session(ws, approval="auto")
    r = s.run_turn("설명해줘", ask=lambda p: "abort")            # 두 번 이어 간 뒤에는 그대로 받아들인다
    assert r["values"]["status"] in ("done", "escalate", "failed")
    s.close()
