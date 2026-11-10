"""/fork: 대화를 새 세션으로 복사해 갈라지기."""
from cap import chat as C
from test_chat import FIX, script, session, ws  # noqa: F401


def test_fork_copies_conversation_and_both_continue(ws, monkeypatch):
    seen = script(monkeypatch, FIX + [{"content": "포크에서 이어 감"}, {"content": "원래 세션에서 이어 감"}])
    s, out = session(ws, approval="auto")
    s.run_turn("double 고쳐줘")
    old_sid = s.sid
    s2, quit_, _ = C.handle_command(s, ws, "/fork 실험 가지")
    assert s2.sid != old_sid and s2.meta["title"] == "실험 가지" and s2.meta["forked_from"] == old_sid
    r = s2.run_turn("방금 뭐 했어?")
    first = [m for m in seen[3] if m["role"] == "user"]
    assert any("double 고쳐줘" in m["content"] for m in first)            # 포크는 앞 대화를 안다
    assert r["values"]["turn_no"] == 2 and s2.undo().startswith("↩ Undid turn 2")
    s2.close()
    s3 = C.ChatSession(ws, old_sid, "auto", s.ui)                          # 원래 세션은 그대로
    r3 = s3.run_turn("이어서")
    assert r3["values"]["turn_no"] == 2 and not any("방금 뭐 했어" in str(m.get("content")) for m in r3["values"]["messages"])
    s3.close()
