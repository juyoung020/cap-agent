"""@파일 · !명령 · /compact · /init · /review · /commit (가짜 모델 대본, API 호출 없음)."""

import asyncio

from cap import chat as C
from cap import tui
from cap.tui import Composer, ToolCell
from test_chat import BUGGY, FIX, call, script, session, ws  # noqa: F401  (ws 는 fixture)


def test_mentions_attach_file_and_folder_but_not_secrets_or_outside(ws):
    (ws / ".env").write_text("KEY=1", encoding="utf-8")
    extra = C.attach_mentions(ws, "고쳐줘 @m.py 그리고 @tests @.env @../x.py @nope.py")
    text = "\n".join(extra)
    assert "--- @m.py (2 lines) ---" in text and "return x + x + 1" in text
    assert "--- @tests (folder) ---" in text and "test_m.py" in text
    assert ".env" not in text and "x.py" not in text and "nope" not in text


def test_mention_goes_to_model_but_screen_shows_only_the_request(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": "봤습니다."}])
    s, _ = session(ws)
    r = s.run_turn("@m.py 설명해줘")
    user = [m for m in seen[0] if m["role"] == "user"][-1]["content"]
    assert "@m.py 설명해줘" in user and C.CONTEXT_MARK.strip() in user and "return x + x + 1" in user
    goal = r["values"]["turn_log"][-1]["goal"]
    assert C.shown_goal(goal) == "@m.py 설명해줘"
    s.close()


def test_shell_runs_directly_and_next_turn_sees_output(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": "실패 하나 보이네요."}])
    s, _ = session(ws)
    code, out = s.shell("python -m pytest -q")
    assert code != 0 and "1 failed" in out
    assert s.shell("rm -rf /")[1].startswith("Blocked")           # 하드 금지는 사람이 쳐도 막는다
    s.run_turn("왜 실패해?")
    user = [m for m in seen[0] if m["role"] == "user"][-1]["content"]
    assert "[The user ran `python -m pytest -q` themselves → exit 1]" in user and "1 failed" in user
    assert s.shell_notes == []                                      # 한 번 붙이면 비운다
    s.close()


def test_compact_command_replaces_history_with_summary(ws, monkeypatch):
    script(monkeypatch, [{"content": "첫 답"}, {"content": "둘째 답"}, {"content": "요약: 질문 두 개에 답함"},
                         {"content": "셋째 답"}])
    s, out = session(ws)
    s.run_turn("첫 질문")
    s.run_turn("두 번째 질문")
    C.handle_command(s, ws, "/compact")
    assert any(ln.startswith("Compacted context: ~") for ln in out)
    assert [m["role"] for m in s.values()["messages"]] == ["system"]
    r = s.run_turn("세 번째")
    first_user = [m for m in r["values"]["messages"] if m["role"] == "user"][0]["content"]
    assert "요약: 질문 두 개에 답함" in first_user and "세 번째" in first_user
    s.close()


def test_init_review_commit_return_prompts_and_review_is_read_only(ws, monkeypatch):
    s, out = session(ws)
    assert C.handle_command(s, ws, "/init")[2] == C.INIT_PROMPT
    assert C.handle_command(s, ws, "/commit")[2] == C.COMMIT_PROMPT
    assert C.handle_command(s, ws, "/review")[2] is None and "No changes" in out[-1]
    script(monkeypatch, FIX + [{"tool_calls": [call("edit_file", path="m.py", old="return x + x", new="return 2*x")]},
                               {"content": "버그 없음."}])
    s.approval["mode"] = "auto"
    s.run_turn("double 고쳐줘")
    _, _, text = C.handle_command(s, ws, "/review")
    assert text.startswith(C.REVIEW_PROMPT) and "-    return x + x + 1" in text and s.turn_mode == "chat"
    r = s.run_turn(text)
    assert (ws / "m.py").read_text() == "def double(x):\n    return x + x\n"     # 리뷰 턴은 편집이 막힌다
    assert s.approval["mode"] == "auto" and s.turn_mode is None                 # 다음 턴은 원래 모드
    assert "버그 없음." in s.last_answer(r["values"])
    s.close()


def test_help_lists_new_commands():
    h = C.help_text()
    for c in ("/init", "/compact", "/review", "/commit", "/mode", "@path", "!command"):
        assert c in h


# ---------------------------------------------------------------- 대화창

def run(coro):
    asyncio.run(coro)


async def until(pilot, cond, timeout=20.0):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("timeout")


def test_tui_at_file_popup_and_bang_command(ws, monkeypatch):
    script(monkeypatch, [{"content": "봤습니다."}])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 30)) as pilot:
            pop = app.query_one("#popup")
            comp = app.query_one(Composer)
            await pilot.press("@", "t")
            await pilot.pause(0.05)
            assert pop.display and pop.get_option_at_index(0).id == "@tests/test_m.py"
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert comp.text == "@tests/test_m.py " and not pop.display
            comp.load_text("")
            await pilot.press(*"!python -m pytest -q", "enter")
            await until(pilot, lambda: app.job is None and any(isinstance(c, ToolCell) for c in app.log_view.children))
            t = tui.plain_text(app.log_view.children[-2].content)
            assert "Bash  python -m pytest -q  → exit 1" in t and "1 failed" in t
            assert app.s.shell_notes
            comp.load_text("/init")
            await pilot.press("enter")
            await until(pilot, lambda: app.job is None and "봤습니다." in "".join(
                tui.plain_text(c.content) for c in app.log_view.children))
            texts = "".join(tui.plain_text(c.content) for c in app.log_view.children)
            assert "Explore this repository" not in texts                    # 숨긴 지시문은 대화에 안 찍힘
    run(go())


def test_absolute_paths_inside_workspace_are_shown_relative(ws):
    assert C.disp_path(str(ws / "m.py"), ws) == "m.py"
    assert C.short_call("edit_file", {"path": str(ws / "tests" / "test_m.py")}, ws) == "edit tests/test_m.py"
    outside = str(ws.parent / "other.py")
    assert C.disp_path(outside, ws) == outside and C.disp_path("m.py", ws) == "m.py"
