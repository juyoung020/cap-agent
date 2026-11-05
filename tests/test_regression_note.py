"""요청 전에는 통과하던 테스트를 모델이 깨뜨리면, 테스트 결과 뒤에 그 사실과 이번 요청의 diff 를 붙인다."""
from test_chat import call, script, session, ws  # noqa: F401

GOOD = "def double(x):\n    return x + x\n"


def test_regression_note_after_model_breaks_a_passing_test(ws, monkeypatch):
    (ws / "m.py").write_text(GOOD, encoding="utf-8")                   # 요청 전에는 통과
    seen = script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x", new="return x + x + 1")]},
        {"tool_calls": [call("run_tests")]},
        {"content": "고쳤습니다."},
        {"content": "확인했습니다."},
        {"content": "끝."}])
    s, _ = session(ws, approval="auto")
    s.run_turn("double 에 1 을 더해줘", ask=lambda p: "abort")
    after_tests = [m for m in seen[3] if m["role"] == "tool"][-1]["content"]
    assert "PASSED before this request and FAIL now" in after_tests and "test_double" in after_tests
    assert "-    return x + x" in after_tests and "+    return x + x + 1" in after_tests      # 이번 요청의 diff
    s.close()


def test_no_regression_note_when_test_was_already_failing(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="def double(x):", new="def double(x):  # note")]},
        {"tool_calls": [call("run_tests")]},
        {"content": "그대로입니다."},
        {"content": "끝."},
        {"content": "끝."}])
    s, _ = session(ws, approval="auto")
    s.run_turn("주석 달아줘", ask=lambda p: "abort")
    after_tests = [m for m in seen[2] if m["role"] == "tool"][-1]["content"]
    assert "PASSED before this request" not in after_tests                          # 원래부터 실패하던 테스트
    s.close()
