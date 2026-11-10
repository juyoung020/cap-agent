"""테스트 없이 편집만 이어지면 하네스가 테스트를 돌려 결과를 도구 결과 뒤에 붙인다(모델 호출은 늘지 않음)."""
from cap import loop as L
from test_chat import call, script, session, ws  # noqa: F401


def edits(n, start=0):
    """m.py 끝의 숫자만 바꾸는 편집 n 개(항상 틀린 답 — 테스트는 계속 실패)."""
    out, cur = [], "return x + x + 1"
    for i in range(start, start + n):
        new = f"return x + x + {i + 2}"
        out.append({"tool_calls": [call("edit_file", path="m.py", old=cur, new=new)]})
        cur = new
    return out, cur


def notes(seen_row):
    return [m["content"] for m in seen_row if m["role"] == "tool" and "edits without running the tests" in m["content"]]


def test_note_after_enough_untested_edits(ws, monkeypatch):
    monkeypatch.setattr(L, "UNTESTED_EDITS", 3)
    steps, _ = edits(3)
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py")]}] + steps + [{"content": "그만."}] * 4)
    s, _ = session(ws, approval="auto")
    s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert not notes(seen[3])                      # 편집 두 번까지는 조용하다
    got = notes(seen[4])                           # 세 번째 편집의 결과에 붙는다
    assert len(got) == 1 and "1 failed" in got[0] and "test_double" in got[0]
    assert "You made 3 edits" in got[0]
    s.close()


def test_running_tests_resets_the_count(ws, monkeypatch):
    monkeypatch.setattr(L, "UNTESTED_EDITS", 3)
    first, cur = edits(2)
    rest = [{"tool_calls": [call("edit_file", path="m.py", old=cur, new="return x + x + 9")]},
            {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 9", new="return x + x + 8")]}]
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py")]}] + first
                  + [{"tool_calls": [call("run_tests")]}] + rest + [{"content": "그만."}] * 4)
    s, _ = session(ws, approval="auto")
    s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    assert not any(notes(row) for row in seen)     # 2번 편집 → 테스트 → 2번 편집: 한도(3)에 닿지 않는다
    s.close()


def test_passing_tests_are_reported_too(ws, monkeypatch):
    monkeypatch.setattr(L, "UNTESTED_EDITS", 2)
    seen = script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x + 0")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 0", new="return x + x")]},
        {"content": "고쳤습니다."}])
    s, _ = session(ws, approval="auto")
    r = s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    got = notes(seen[3])
    assert len(got) == 1 and "1 passed" in got[0] and "Failing" not in got[0]
    assert r["values"]["status"] == "done"
    s.close()


def test_doc_edits_do_not_count(ws, monkeypatch):
    monkeypatch.setattr(L, "UNTESTED_EDITS", 2)
    seen = script(monkeypatch, [
        {"tool_calls": [call("write_file", path="NOTES.md", content="a\n")]},
        {"tool_calls": [call("write_file", path="NOTES.md", content="b\n")]},
        {"tool_calls": [call("write_file", path="NOTES.md", content="c\n")]},
        {"content": "적었습니다."}])
    s, _ = session(ws, approval="auto")
    s.run_turn("메모 남겨줘", ask=lambda p: "abort")
    assert not any(notes(row) for row in seen)
    s.close()
