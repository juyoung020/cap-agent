"""전체 화면 대화창(Textual)을 pilot 으로 몬다. 가짜 모델 대본이라 API 호출 없음."""

import asyncio
import io
import json
import time

import pytest
from rich.console import Console

from cap import cli
from cap import loop as L
from cap import tui
from cap.tui import ChoiceCard, Composer, ExploreCell, ToolCell, WelcomeCell

BUGGY = "def double(x):\n    return x + x + 1\n"
TEST = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from m import double\n\ndef test_double():\n    for i in range(5):\n        assert double(i) == 2 * i\n")


def call(name, **args):
    return {"id": f"c_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**6}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def script(monkeypatch, replies, delay=0.0):
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = next(it)
        if r.get("sleep"):                       # 느린 모델 흉내: 잘게 자서 중단(비동기 예외)이 바로 먹히게
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < r["sleep"]:
                time.sleep(0.02)
        elif delay:
            time.sleep(delay)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "m.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(TEST, encoding="utf-8")
    return tmp_path


FIX = [{"tool_calls": [call("read_file", path="m.py")]},
       {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
       {"content": "double 을 고쳤습니다."}]


def plain(*renderables) -> str:
    con = Console(file=io.StringIO(), width=120, color_system=None)
    for r in renderables:
        con.print(r)
    return con.file.getvalue()


def text_of(app) -> str:
    return plain(*(c.content for c in app.log_view.children))


async def until(pilot, cond, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("시간 초과\n" + text_of(pilot.app)[-1500:])


def idle(app):
    return app.job is None


def run(coro):
    asyncio.run(coro)


async def send(pilot, app, text):
    comp = app.query_one(Composer)
    comp.load_text(text)
    await pilot.press("enter")


def test_turn_shows_tool_cells_and_approval_yes(ws, monkeypatch):
    script(monkeypatch, FIX, delay=0.05)

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(110, 40)) as pilot:
            assert any(isinstance(c, WelcomeCell) for c in app.log_view.children)       # 시작 화면
            await send(pilot, app, "double 고쳐줘")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            card = app.query_one(ChoiceCard).body_text()
            assert "Do you want to make this edit to m.py?" in card and "+    return x + x" in card and "1. Yes" in card
            assert "❯ 1. Yes" in card and not app.query_one(Composer).display     # 입력창 자리에 붙는 목록
            assert not any(isinstance(c, WelcomeCell) for c in app.log_view.children)   # 대화가 시작되면 사라짐
            assert app.running and not app.query_one("#busy").display
            await pilot.press("y")
            await until(pilot, lambda: idle(app))
            await pilot.pause(0.1)
            t = text_of(app)
            assert (ws / "m.py").read_text() == "def double(x):\n    return x + x\n"
            assert any(isinstance(c, ExploreCell) for c in app.log_view.children)          # read 묶음
            tools = [c for c in app.log_view.children if isinstance(c, ToolCell)]
            assert tools and "Edit  m.py  +1 -1" in t and "+    return x + x" in t         # 색 diff 셀
            assert "Read  m.py" in t
            assert "✓ Allowed  edit m.py" in t and "Verified " in t and "1 passed" in t
            assert "double 을 고쳤습니다." in t and "model call" in t
            assert app.query_one(Composer).display
            return card
    run(go())


def test_approval_always_and_deny_with_reason(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x  # a")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x  # a", new="return x + x")]},
        {"content": "ok"},
        {"tool_calls": [call("run", cmd="python m.py")]},
        {"content": "알겠습니다."}])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(110, 40)) as pilot:
            await send(pilot, app, "fix")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            await pilot.press("down", "enter")                      # ↓ Enter = 이 세션 동안: 두 번째 편집은 안 묻는다
            await until(pilot, lambda: idle(app))
            assert (ws / "m.py").read_text().endswith("return x + x\n")
            assert text_of(app).count("Allowed for this session") == 1
            await send(pilot, app, "실행해 봐")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            await pilot.press("n")                                  # 거절 → 카드 안 사유 입력칸
            inp = app.query_one(ChoiceCard).reason_input
            assert inp.display and inp.has_focus
            inp.value = "실행하지 말고 설명만 해"
            await pilot.press("enter")
            await until(pilot, lambda: idle(app))
            assert any(m["role"] == "tool" and "설명만 해" in m["content"] for m in seen[-1])
            assert "✗ Declined  run python m.py — 실행하지 말고 설명만 해" in text_of(app)
    run(go())


def test_ctrl_c_interrupts_turn_but_keeps_app(ws, monkeypatch):
    seen = script(monkeypatch, [{"sleep": 30}, {"content": "다음 답"}])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, app, "오래 걸리는 일")
            await until(pilot, lambda: app.query_one("#busy").display and seen)   # 모델 호출 도중에 누른다
            assert "esc to interrupt" in str(app.query_one("#busy").content)
            t0 = time.monotonic()
            await pilot.press("ctrl+c")
            await until(pilot, lambda: idle(app), timeout=10)
            assert time.monotonic() - t0 < 5
            assert "Interrupted" in text_of(app) and app.is_running
            await send(pilot, app, "계속")                           # 같은 세션으로 이어 감
            await until(pilot, lambda: idle(app) and "다음 답" in text_of(app))
            assert app.s.values()["turn_no"] == 2
            await pilot.press("ctrl+c")                             # 빈 입력에서 두 번 = 종료
            assert app.is_running
            await pilot.press("ctrl+c")
            await pilot.pause(0.2)
        assert not app.is_running
    run(go())


def test_escape_interrupts_too(ws, monkeypatch):
    seen = script(monkeypatch, [{"sleep": 30}])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, app, "오래")
            await until(pilot, lambda: app.running and seen)
            await pilot.press("escape")
            await until(pilot, lambda: idle(app), timeout=10)
            assert "Interrupted" in text_of(app)
    run(go())


def test_slash_popup_completion_and_commands(ws, monkeypatch):
    script(monkeypatch, [])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 34)) as pilot:
            pop = app.query_one("#popup")
            await pilot.press("/")
            await pilot.pause(0.05)
            assert pop.display and pop.option_count >= 10
            await pilot.press("d", "i")
            await pilot.pause(0.05)
            assert pop.option_count == 1 and pop.get_option_at_index(0).id == "/diff"
            await pilot.press("tab")                                # 완성
            await pilot.pause(0.05)
            assert app.query_one(Composer).text == "/diff " and not pop.display
            await pilot.press("enter")
            await until(pilot, lambda: idle(app) and "No files changed" in text_of(app))
            await pilot.press("/", "s", "t")
            await pilot.pause(0.05)
            await pilot.press("enter")                              # 팝업에서 Enter = 바로 실행
            await until(pilot, lambda: idle(app) and "context ~" in text_of(app))
            await pilot.press("/", "h", "e", "l", "p", "enter")
            await until(pilot, lambda: idle(app) and "interrupts the current turn" in text_of(app))
    run(go())


def test_mode_cycle_keys_and_bypass_warning(ws, monkeypatch):
    script(monkeypatch, [])

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 30)) as pilot:
            assert "shift+tab to cycle" in str(app.query_one("#mode").content)
            seen = []
            for key in ["shift+tab", "alt+m", "shift+tab", "shift+tab", "alt+m"]:
                await pilot.press(key)
                seen.append(app.s.approval["mode"])
                if seen[-1] == "bypass":                                # 경고는 하단 줄에만(빨강), 기록엔 안 남김
                    assert "bypass permissions" in str(app.query_one("#mode").content)
            assert seen == ["plan", "auto", "bypass", "chat", "ask"]
            assert "Bypass" not in text_of(app) and any(isinstance(c, WelcomeCell) for c in app.log_view.children)
            assert "ask before edits" in str(app.query_one("#mode").content)
    run(go())


def test_plan_mode_then_approve_runs_plan(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("todo", items=[{"text": "m.py 의 double 에서 +1 제거", "done": False},
                                            {"text": "테스트로 확인", "done": False}])]},
        {"content": "계획: +1 을 빼고 테스트한다."},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "계획대로 고쳤습니다."}])

    async def go():
        app = tui.make_app(ws, approval="plan")
        async with app.run_test(size=(110, 40)) as pilot:
            await send(pilot, app, "double 고칠 계획 세워줘")
            await until(pilot, lambda: idle(app) and app.query_one(ChoiceCard).display)
            assert "Would you like to proceed with this plan (2 steps)?" in app.query_one(ChoiceCard).body_text()
            assert "Carry out" not in text_of(app)
            assert "m.py 의 double 에서 +1 제거" in text_of(app)              # todo 셀
            assert "[mode: plan]" in seen[0][-1]["content"]
            assert (ws / "m.py").read_text() == BUGGY                        # 플랜 중엔 안 고침
            await pilot.press("enter")                                       # 첫 항목 = auto-accept 로 실행
            await until(pilot, lambda: idle(app) and "계획대로 고쳤습니다." in text_of(app))
            assert app.s.approval["mode"] == "auto"
            assert (ws / "m.py").read_text().endswith("return x + x\n")
    run(go())


def test_composer_newline_history_and_queue(ws, monkeypatch):
    script(monkeypatch, [{"content": "하나"}, {"content": "둘"}], delay=0.3)

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(100, 30)) as pilot:
            comp = app.query_one(Composer)
            await pilot.press("a", "ctrl+j", "b")
            assert comp.text == "a\nb"
            comp.load_text("c\\")
            comp.move_cursor(comp.document.end)
            await pilot.press("enter")                                  # 줄 끝 \ + Enter = 줄바꿈
            assert comp.text == "c\n"
            comp.load_text("첫 질문")
            await pilot.press("enter")
            await until(pilot, lambda: app.running)
            comp.load_text("둘째 질문")
            await pilot.press("enter")                                  # 작업 중 입력은 끼어들기(다음 단계 또는 다음 요청)
            assert "sent to CAP mid-turn" in text_of(app) and not app.queue
            await until(pilot, lambda: idle(app) and not app.queue and "둘" in text_of(app), timeout=15)
            await until(pilot, lambda: idle(app))
            await pilot.press("up")
            assert comp.text == "둘째 질문"
            await pilot.press("up")
            assert comp.text == "첫 질문"
            await pilot.press("down", "down")
            assert comp.text == ""
            await pilot.press("ctrl+l")
            await pilot.pause(0.05)
            assert not app.log_view.children
    run(go())


def test_resume_replays_history(ws, monkeypatch):
    script(monkeypatch, FIX)
    from cap import chat as C
    ui = C.UI(out=lambda s="": None)
    ui.tty = ui.color = False
    s = C.ChatSession(ws, None, "auto", ui)
    s.run_turn("double 고쳐줘")
    s.close()

    async def go():
        app = tui.make_app(ws, resume="last")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            t = text_of(app)
            assert "double 고쳐줘" in t and "double 을 고쳤습니다." in t and "1 previous turn" in t
    run(go())


def test_cli_uses_tui_only_on_tty(ws, monkeypatch):
    got = []
    monkeypatch.setattr(tui, "run_tui", lambda w, approval="ask", resume=None: got.append(("tui", approval)) or 0)
    from cap import chat as C
    monkeypatch.setattr(C, "repl", lambda w, approval="ask", resume=None: got.append(("repl", approval)) or 0)
    monkeypatch.chdir(ws)
    monkeypatch.setattr(cli, "use_tui", lambda: True)
    cli.main(["--mode", "chat"])
    cli.main(["--plain"])
    monkeypatch.setattr(cli, "use_tui", lambda: False)
    cli.main([])
    assert got == [("tui", "chat"), ("repl", "ask"), ("repl", "ask")]
    monkeypatch.undo()
    assert cli.use_tui() is False                                      # pytest 의 stdin 은 TTY 가 아님


def test_ask_user_question_card_pick_option(ws, monkeypatch):
    seen = script(monkeypatch, [{"tool_calls": [call("ask_user", question="Which name style?", options=["snake_case", "camelCase"])]},
                                {"content": "snake_case 로 갑니다."}], delay=0.05)

    async def go():
        app = tui.make_app(ws)
        async with app.run_test(size=(110, 40)) as pilot:
            await send(pilot, app, "이름 규칙 정해줘")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            card = app.query_one(ChoiceCard).body_text()
            assert "Which name style?" in card and "1. snake_case" in card and "Type something else" in card
            await pilot.press("down", "enter")                       # 두 번째 선택지
            await until(pilot, lambda: idle(app))
            t = text_of(app)
            assert "↳ camelCase" in t and "Asked" in t
    run(go())
    assert "The user answered: camelCase" in [m for m in seen[1] if m["role"] == "tool"][-1]["content"]


def test_double_escape_offers_rewind(ws, monkeypatch):
    script(monkeypatch, [{"content": "첫 답"}] + FIX)

    async def go():
        app = tui.make_app(ws, approval="auto")
        async with app.run_test(size=(110, 40)) as pilot:
            await send(pilot, app, "설명해줘")
            await until(pilot, lambda: idle(app) and "첫 답" in text_of(app))
            await send(pilot, app, "double 고쳐줘")
            await until(pilot, lambda: idle(app) and "고쳤습니다" in text_of(app))
            assert (ws / "m.py").read_text() == "def double(x):\n    return x + x\n"
            await pilot.press("escape", "escape")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            card = app.query_one(ChoiceCard).body_text()
            assert "Rewind files and conversation" in card and "Before turn 2: double 고쳐줘" in card
            await pilot.press("enter")                               # 맨 위 = 가장 최근 턴(2) 이전으로
            await until(pilot, lambda: idle(app) and (ws / "m.py").read_text() == BUGGY)
            await pilot.pause(0.2)
            t = text_of(app)
            assert "Back to before turn 2" in t and t.count("double 고쳐줘") == 1   # 화면도 되돌린 대화로(알림 한 줄만)
            assert "고쳤습니다" not in t and "첫 답" in t
    run(go())


def test_footer_shows_sandbox_state(ws, monkeypatch):
    from cap import sandbox as sbx
    monkeypatch.setattr(sbx, "backend", lambda mode=None: "landlock")

    async def go():
        app = tui.make_app(ws, approval="auto")
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.1)
            assert "sandboxed" in plain(app.query_one("#mode").render())
    run(go())



def test_install_approval_wording_has_no_session_allow(ws, monkeypatch):
    script(monkeypatch, [{"tool_calls": [call("plugin_install", id="commit-commands@demo")]}, {"content": "no"}])
    import cap.pluginhub as PH
    monkeypatch.setattr(PH, "install", lambda i, force=False: (False, "declined"))

    async def go():
        app = tui.make_app(ws, approval="auto")
        async with app.run_test(size=(120, 40)) as pilot:
            await send(pilot, app, "커밋 플러그인 설치해줘")
            await until(pilot, lambda: app.query_one(ChoiceCard).display)
            card = app.query_one(ChoiceCard).body_text()
            assert "Install plugin commit-commands@demo?" in card and "don't ask again" not in card
            await pilot.press("escape")
            await until(pilot, lambda: idle(app))
    run(go())


def test_resumed_tui_tells_about_a_turn_that_stopped_midway(ws, monkeypatch):
    """다시 연 대화창: 중간에 멈춘 턴이면 바로(15초 작업 확인을 기다리지 않고) /continue 를 알린다."""
    from cap import chat as C
    calls = iter([{"tool_calls": [{"id": "r1", "type": "function",
                                   "function": {"name": "read_file", "arguments": json.dumps({"path": "m.py"})}}]}])

    def chat2(messages, **kw):
        try:
            r = next(calls)
        except StopIteration:
            raise RuntimeError("HTTP 502")
        return {"content": "", "tool_calls": r["tool_calls"], "finish_reason": "stop", "usage": {},
                "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", chat2)
    ui = C.UI(out=lambda s="": None)
    ui.tty = ui.color = False
    s = C.ChatSession(ws, None, "auto", ui)
    with pytest.raises(RuntimeError):
        s.run_turn("double 고쳐줘")
    s.close()

    async def go():
        app = tui.make_app(ws, resume="last")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            assert "stopped midway (next step: agent)" in text_of(app) and "/continue" in text_of(app)
    run(go())
