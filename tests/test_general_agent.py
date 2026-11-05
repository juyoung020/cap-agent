"""일반 코딩 에이전트로서 갖춰야 할 장치(Hermes·Codex 대비) — 가짜 모델 대본, API 호출 없음.

반복 감지 · 빈 응답 재촉 · 예산 마무리 요약 · 덮어쓰기 보호 · 안전한 /undo · 줄 번호 · 경로·편집 힌트 ·
데이터 문법 검사 · 승인 전 드라이런 · 명령 앞부분 허용 · 끼어들기 · 남은 todo · 도구 결과 데이터 표시 · 점검
"""

import pytest

from cap import chat as C
from cap import loop as L
from cap.policy import classify_mode, command_prefix
from cap.tools import ToolBox, strip_line_numbers
from test_chat import BUGGY, call, script, session, ws  # noqa: F401  (ws 는 fixture)

FIXED = "def double(x):\n    return x + x\n"


def tool_msgs(seen):
    return [m["content"] for m in seen[-1] if m["role"] == "tool"]


def test_read_file_has_line_numbers_and_edit_strips_them(ws):
    box = ToolBox(ws)
    out = box.execute("read_file", {"path": "m.py"})["output"]
    assert "1| def double(x):" in out and "2|     return x + x + 1" in out
    r = box.execute("edit_file", {"path": "m.py", "old": "2|     return x + x + 1", "new": "2|     return x + x"})
    assert r["ok"] and (ws / "m.py").read_text() == FIXED
    assert strip_line_numbers("12| a\n13| b") == "a\nb" and strip_line_numbers("x = 1| 2") == "x = 1| 2"


def test_missing_path_suggests_similar_files(ws):
    out = ToolBox(ws).execute("read_file", {"path": "test_m.py"})["output"]
    assert "Did you mean" in out and "tests/test_m.py" in out


def test_edit_failure_shows_closest_lines_and_escalates_hint(ws):
    box = ToolBox(ws)
    outs = [box.execute("edit_file", {"path": "m.py", "old": "return x * 3", "new": "return 12345"})["output"]
            for _ in range(3)]
    assert "Closest lines" in outs[0] and "2|" in outs[0]
    assert "edit failure #3" in outs[2]


def test_stale_overwrite_is_refused(ws):
    box = ToolBox(ws)
    box.execute("read_file", {"path": "m.py"})
    (ws / "m.py").write_text("def double(x):\n    return 2 * x  # user edit\n", encoding="utf-8")
    r = box.execute("write_file", {"path": "m.py", "content": FIXED})
    assert not r["ok"] and r.get("stale_write_blocked") and "user edit" in (ws / "m.py").read_text()
    box.execute("read_file", {"path": "m.py"})
    assert box.execute("write_file", {"path": "m.py", "content": FIXED})["ok"]


def test_json_syntax_error_is_reported(ws):
    r = ToolBox(ws).execute("write_file", {"path": "cfg.json", "content": '{"a": 1,}'})
    assert r["ok"] and r["syntax_error"] and "문법" in r["output"]


def test_command_prefix_rules():
    assert command_prefix("python -m pytest -q tests/test_a.py") == "python -m pytest"
    assert command_prefix("npm run build -- --prod") == "npm run build"
    assert command_prefix("git log --oneline") == "git log"
    assert command_prefix("bash -c 'echo hi'") == "bash -c 'echo hi'"          # 너무 넓은 운반자는 전체만
    assert command_prefix("make test && rm x") == "make test && rm x"


def test_always_allow_covers_same_prefix(ws):
    d = classify_mode("run", {"cmd": "python tool.py --a 1"}, ws, "ask")
    assert d.verdict == "ask" and d.category == "exec:python tool.py"
    again = classify_mode("run", {"cmd": "python tool.py --b 2"}, ws, "ask", session={d.category})
    assert again.verdict == "allow"
    other = classify_mode("run", {"cmd": "python other.py"}, ws, "ask", session={d.category})
    assert other.verdict == "ask"


def test_repeated_identical_read_is_warned_then_blocked(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py")]}] * 4 + [{"content": "봤습니다."}])
    s, _ = session(ws, "auto")
    s.run_turn("m.py 설명해줘")
    outs = tool_msgs(seen)
    assert "Same read_file 3 times" in outs[2] and outs[3].startswith("<data") is False and "BLOCKED" in outs[3]
    s.close()


def test_same_failing_call_escalates_after_five(ws, monkeypatch):
    bad = {"tool_calls": [call("edit_file", path="m.py", old="nope nope", new="return 12345")]}
    script(monkeypatch, [bad] * 5 + [{"content": "끝"}])
    s, _ = session(ws, "auto")
    asked = []
    r = s.run_turn("고쳐", ask=lambda p: asked.append(p) or "abort")
    assert asked and asked[0]["kind"] == "escalate" and "failed 5 times" in asked[0]["reason"]
    assert r["values"]["status"] == "escalated"
    s.close()


def test_empty_reply_is_nudged(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": ""}, {"content": "이제 답합니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("안녕")
    assert s.last_answer(r["values"]) == "이제 답합니다."
    assert "reply was empty" in seen[-1][-1]["content"]
    s.close()


def test_turn_limit_asks_for_a_wrapup_without_tools(ws, monkeypatch):
    monkeypatch.setattr(L, "MAX_TURNS", 2)
    seen = script(monkeypatch, [{"tool_calls": [call("search", pattern="double")]},
                                {"tool_calls": [call("search", pattern="x + x")]},
                                {"content": "double 을 찾았지만 아직 못 고쳤습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("고쳐")
    assert r["values"]["status"] == "failed" and "Turn limit reached" in seen[-1][-1]["content"]
    assert s.last_answer(r["values"]) == "double 을 찾았지만 아직 못 고쳤습니다."
    s.close()


def test_unfinished_todos_are_asked_once(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("todo", items=[{"text": "a 하기", "done": True},
                                                                    {"text": "b 하기", "done": False}])]},
                                {"content": "다 했습니다."}, {"content": "b 는 필요 없었습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("일 해")
    assert "unfinished items: b 하기" in seen[-1][-1]["content"]
    assert s.last_answer(r["values"]) == "b 는 필요 없었습니다."
    s.close()


def test_unappliable_edit_is_returned_before_asking(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("edit_file", path="m.py", old="not in file", new="return 12345")]},
                                {"content": "다시 읽어 볼게요."}])
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("고쳐", ask=lambda p: asked.append(p) or "y")
    assert not asked and "Not applied (nothing was asked or changed)" in tool_msgs(seen)[0]
    s.close()


def test_tool_output_is_marked_as_data(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py")]}, {"content": "ok"}])
    s, _ = session(ws, "auto")
    s.run_turn("봐")
    assert tool_msgs(seen)[0].startswith('<data source="read_file">')
    s.close()


def test_steer_message_reaches_the_next_model_call(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py")]}, {"content": "알겠습니다."}])
    s, _ = session(ws, "auto")
    s.steer_box.append("테스트는 돌리지 마")
    s.run_turn("봐")
    assert any(m["role"] == "user" and m["content"] == "[User message during the turn] 테스트는 돌리지 마"
               for m in seen[0])
    s.close()


def test_undo_keeps_files_the_user_edited_afterwards(ws, monkeypatch):
    (ws / "other.py").write_text("y = 1\n", encoding="utf-8")
    script(monkeypatch, [{"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x"),
                                         call("edit_file", path="other.py", old="y = 1", new="y = 2")]},
                         {"content": "고쳤습니다."}])
    s, _ = session(ws, "auto")
    s.run_turn("고쳐")
    (ws / "other.py").write_text("y = 3  # user\n", encoding="utf-8")       # 사람이 그 뒤에 고침
    (ws / "notes.txt").write_text("my notes\n", encoding="utf-8")         # 사람이 새로 만든 파일
    msg = s.undo()
    assert (ws / "m.py").read_text() == BUGGY
    assert "user" in (ws / "other.py").read_text() and (ws / "notes.txt").exists()
    assert "kept your later edits: other.py" in msg
    s.close()


def test_doctor_offline_checks(ws):
    lines = C.doctor_lines(ws, "python -m pytest -q", ping=False)
    assert any(ln.startswith("✓ git") for ln in lines) and any("tests" in ln for ln in lines)


@pytest.mark.parametrize("files,cmd", [({"Cargo.toml": ""}, "cargo test"), ({"go.mod": ""}, "go test ./..."),
                                       ({"Makefile": "test:\n\tpytest\n"}, "make test")])
def test_test_command_detection(tmp_path, files, cmd):
    for n, t in files.items():
        (tmp_path / n).write_text(t, encoding="utf-8")
    assert C.detect_test_cmd(tmp_path) == cmd
