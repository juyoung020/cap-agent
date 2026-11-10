"""같은 요청 안에서 파일이 이미 시도한 내용으로 되돌아가면(A → B → A) 알린다."""
from test_chat import call, script, session, ws  # noqa: F401


def test_back_and_forth_edit_gets_a_note(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x * 2 + 1")]},   # A → B
        {"tool_calls": [call("edit_file", path="m.py", old="return x * 2 + 1", new="return x + x + 2")]},   # B → C
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 2", new="return x * 2 + 1")]},   # C → B (되돌림)
        {"content": "그만."}, {"content": "끝."}, {"content": "끝."}])
    s, _ = session(ws, approval="auto")
    s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    before = [m["content"] for m in seen[3] if m["role"] == "tool"]          # 세 번째 편집 전까지
    after = [m["content"] for m in seen[4] if m["role"] == "tool"]           # 되돌리는 편집의 결과까지
    assert not any("back to a version" in n for n in before)
    assert "back to a version you already tried" in after[-1]
    s.close()
