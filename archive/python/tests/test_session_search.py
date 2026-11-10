"""session_search: 모델이 이 폴더의 지난 세션에서 요청·답을 찾는다(지금 세션은 빼고)."""
from test_chat import call, script, session, ws  # noqa: F401


def test_model_finds_earlier_session(ws, monkeypatch):
    script(monkeypatch, [{"content": "쿠폰 할인율은 10% 로 정했습니다."}])
    s1, _ = session(ws, approval="auto")
    s1.run_turn("쿠폰 할인율을 정해줘")
    s1.close()
    seen = script(monkeypatch, [{"tool_calls": [call("session_search", query="쿠폰 할인율")]},
                                {"content": "지난번에 10% 로 정했습니다."}])
    s2, _ = session(ws, approval="auto")
    s2.run_turn("지난번에 쿠폰 할인율 얼마로 했지?")
    tool_msg = [m for m in seen[1] if m["role"] == "tool"][-1]["content"]
    assert "asked: 쿠폰 할인율을 정해줘" in tool_msg            # 답(answered)은 실제 모델 호출 기록에서만 생긴다
    assert "지난번에 쿠폰" not in tool_msg                                 # 지금 세션은 빼고
    s2.close()
