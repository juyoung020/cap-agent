"""`cap` 전체 화면 대화창 (Textual). 실제 터미널에서 `cap` 을 치면 이 화면이 뜬다.

화면 구성은 Codex TUI 공개 소스(codex-rs/tui: chatwidget·bottom_pane·history_cell·diff_render·status_indicator)와
Hermes TUI(ui-tui: theme.ts 팔레트·appLayout·prompts·thinking, tui_gateway 의 '에이전트 스레드가 승인 응답을 기다리며 막힘')를 따랐다.

  시작 화면  작은 CAP 로고 + 모델·폴더·모드(대화가 시작되면 사라짐)
  대화 기록  사용자(› 와 옅은 배경) / 에이전트 답(● 마크다운) /
             도구 줄(● 색 = 결과, 동사 Read·Search·Edit·Write·Run·Test + 대상, 아래 ⎿ 로 diff·출력, Ctrl+O 펼침)
  작업 중    스피너 + "Thinking… 12s · esc to interrupt", 명령 실행 중엔 실행 줄에 진행 표시
  승인       입력창 자리에 붙는 선택 목록(팝업 아님): ↑↓ 로 고르고 Enter, 숫자 바로, Esc = 거절
  입력창     회색 테두리, Enter 전송, Ctrl+J 줄바꿈, ↑/↓ 입력 기록, / 자동완성
  상태 줄    모드(Claude Code 식 이름, Shift+Tab 순환) · 힌트 · 맥락·호출 수
화면 문구는 코딩 에이전트에서 관례로 쓰는 영어 용어를 쓴다(모델의 답·명령 출력은 그대로).

기능은 chat.py 의 ChatSession(LangGraph 루프·interrupt/resume·세션 저장)과 handle_command(슬래시 명령)를 그대로 쓴다.
에이전트 턴은 백그라운드 스레드에서 돌고, 화면 갱신은 call_from_thread 로 넘긴다.
"""

from __future__ import annotations

import builtins
import contextlib
import ctypes
import difflib
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import compact as cmp
from . import llm
from .chat import (CONTINUE, PLAN_GO, SLASH_COMMANDS, project_ok, UI, ChatSession, _pick_session, handle_command, set_mode, short_call,
                   disp_path, doctor_lines, shown_goal, turn_summary)
from .policy import is_secret_path, next_mode
from .store import cap_dir, iter_files

# ---------------------------------------------------------------- 팔레트 (한곳에서 정의)
# Hermes ui-tui/theme.ts DARK_THEME(bg #101014, surface #1a1a2e, accent 호박색)을 바탕으로 어두운 테마 하나.
# Textual 쪽은 Theme 로 등록($background·$surface·$panel·$accent·$success…), Rich 셀은 같은 값을 P 로 쓴다.
P = {
    "bg": "#0f0f12", "surface": "#16161b", "panel": "#1d1d24", "border": "#2e2e38", "border2": "#4a4a58",
    "muted": "#8b8b99", "fg": "#e6e6eb", "accent": "#e6e6eb", "brand": "#8ab4f8", "info": "#8ab4f8",
    "success": "#6cc070", "warning": "#f28b82", "error": "#ef5350", "purple": "#b39ddb",
    "add": "#6cc070", "del": "#ef5350", "hunk": "#8ab4f8",
}
CAP_THEME = Theme(name="cap", primary=P["accent"], secondary=P["info"], accent=P["accent"], foreground=P["fg"],
                  background=P["bg"], surface=P["surface"], panel=P["panel"], boost="#ffffff0d",
                  success=P["success"], warning=P["warning"], error=P["error"], dark=True,
                  variables={"border": P["border2"], "border-blurred": P["border"], "footer-background": P["bg"],
                             "input-selection-background": "#8ab4f840", "block-cursor-background": P["fg"],
                             "block-cursor-foreground": P["bg"], "scrollbar": P["border"],
                             "scrollbar-hover": P["muted"], "scrollbar-active": P["border2"]})
MODE_COLOR = {"chat": "#8b8b99", "ask": "#8ab4f8", "plan": "#b39ddb", "auto": "#6cc070", "bypass": "#ef5350"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
YES_WORDS = {"y", "yes", "a", "always", "ㅇ", "예", "네"}
HISTORY_MAX = 300
PLACEHOLDER = "Type a task, or / for commands"
# 모드 표시: Claude Code·Codex 에서 쓰는 이름
MODE_NAME = {"chat": "chat mode (read-only)", "ask": "ask before edits", "plan": "plan mode",
             "auto": "auto-accept edits", "bypass": "bypass permissions"}
MODE_ICON = {"chat": "◇", "ask": "?", "plan": "⏸", "auto": "⏵⏵", "bypass": "⚠"}
MODE_HELP = {"chat": "reads and answers only — no edits, no commands",
             "ask": "asks before every edit and command",
             "plan": "reads only and proposes a plan; runs it after you approve",
             "auto": "edits and runs commands; asks only for risky ones",
             "bypass": "runs everything without asking (hard limits still apply)"}
VERBS = {"read": "Read", "list": "List", "search": "Search", "edit": "Edit", "write": "Write", "run": "Run",
         "test": "Test", "todo": "Plan", "fetch": "Fetch", "websearch": "Web Search", "process": "Process", "glob": "Glob", "symbols": "Symbols", "skill": "Skill", "asked:": "Asked"}
LOGO = ["╭─╮╭─╮╭─╮", "│  ├─┤├─╯", "╰─╯╵ ╵╵  "]
UNITS = [(re.compile(r"(\d+)줄"), r"\1 lines"), (re.compile(r"(\d+)건"), r"\1 hits"), (re.compile("새 파일, "), "new, "),
         (re.compile("이미 적용됨, 변경 없음"), "no change"), (re.compile(r"— 실패$"), "— failed")]


def en(text: str) -> str:
    """줄 모드와 같이 쓰는 도구 요약(한국어 단위)을 화면 용어로."""
    for pat, rep in UNITS:
        text = pat.sub(rep, text)
    return text


# ---------------------------------------------------------------- 렌더 도우미

def plain_text(renderable: RenderableType, width: int = 100) -> str:
    """시험·접근성용: 렌더 결과를 글자만으로."""
    con = Console(file=io.StringIO(), width=width, color_system=None)
    con.print(renderable)
    return con.file.getvalue()


def gutter(mark: str, body: RenderableType, style: str = "") -> Table:
    """왼쪽 2칸 표식 + 본문."""
    g = Table.grid(expand=True)
    g.add_column(width=2, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(Text(mark, style=style), body)
    return g


def diff_text(lines: list[str]) -> Text:
    t = Text()
    for i, ln in enumerate(lines):
        if i:
            t.append("\n")
        if ln.startswith("@@"):
            t.append(ln, style=P["hunk"])
        elif ln.startswith("+"):
            t.append(ln, style=P["add"])
        elif ln.startswith("-"):
            t.append(ln, style=P["del"])
        else:
            t.append(ln, style=P["muted"])
    return t


def tool_head(head: str, ok: bool) -> Text:
    """'edit calc.py +1 -1' → '● Edit  calc.py  +1 -1' (점 색 = 결과, 동사 굵게, 대상 밝게, 결과 흐리게)."""
    verb, _, rest = en(head).partition(" ")
    label = VERBS.get(verb, verb.capitalize())
    color = P["success"]
    if not ok or (verb in ("test", "run") and re.search(r"\bfailed\b|\berror|exit [1-9]", rest)):
        color = P["error"]
    t = Text()
    t.append("● ", style=color)
    t.append(label, style=f"bold {P['fg']}")
    target, sep, result = rest.partition(" → ")
    if not sep:
        m = re.match(r"(.*?)\s+(\+\d+ -\d+|\(.*\))\s*$", target)
        if m:
            target, result, sep = m.group(1), m.group(2), " "
    t.append("  " + target, style=P["fg"])
    if sep:
        t.append(("  → " if sep.strip() else "  ") + result, style=P["muted"])
    return t


def full_diff(before: str | None, after: str, limit: int = 400) -> list[str]:
    if before is None:
        lines = ["+" + ln for ln in after.splitlines()]
    else:
        lines = [ln for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2)
                 if not ln.startswith(("---", "+++"))]
    return lines[:limit] + ([f"… +{len(lines) - limit} lines"] if len(lines) > limit else [])


def under(*body: RenderableType) -> Table:
    """도구 줄 아래 내용: '  ⎿  ' 들여쓰기(Claude Code 모양)."""
    g = Table.grid(expand=True)
    g.add_column(width=5, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(Text("  ⎿  ", style=P["border2"]), Group(*body))
    return g


EXPAND_HINT = "… ctrl+o to expand"


def tool_view(plain: UI, ws: Path, name: str, args: dict, res: dict, before: str | None) -> dict:
    """도구 결과 → 화면 조각. 접힌 모양은 줄 모드(UI.tool_lines)와 같은 내용, 펼친 모양은 전체 diff/출력."""
    ok = res.get("ok", True)
    lines = plain.tool_lines(name, args, res, before)
    head = lines[0][4:] if lines else name
    rest = [en(ln[6:] if ln.startswith("      ") else ln.strip()) for ln in lines[1:]]
    if name in ("read_file", "search"):
        return {"kind": "explore", "head": head, "ok": ok}
    if name == "todo":
        items = []
        for ln in (res.get("output") or "").splitlines():
            m = re.match(r"\[(x| )\]\s*(.*)", ln)
            if m:
                items.append((m.group(1) == "x", m.group(2)))
        return {"kind": "todo", "items": items}
    body: list[RenderableType] = []
    full_body: RenderableType | None = None
    if name in ("edit_file", "write_file"):
        diff_part = [ln for ln in rest if ln[:1] in "+-…"]
        other = [ln for ln in rest if ln[:1] not in "+-…"]
        if diff_part:
            body.append(diff_text(diff_part))
        for ln in other:
            body.append(Text(ln.replace("편집 뒤 문법 오류 — 모델에게 알림", "syntax error after edit — sent back to the model"),
                             style=P["warning"]))
        if ok and res.get("changed"):
            after = cmp.read_before(ws, name, args) or ""
            full = full_diff(before, after)
            if len(full) > len(diff_part):
                full_body = diff_text(full)
    elif name in ("run", "run_tests"):
        if rest:
            body.append(Text("\n".join(rest), style=P["muted"]))
        out = (res.get("output") or "").rstrip().splitlines()[-60:]
        if out and len(out) > len(rest):
            full_body = Text("\n".join(out), style=P["muted"])
    else:
        body += [Text(ln, style=P["muted"]) for ln in rest]
    h = tool_head(head, ok)
    if full_body is not None:
        body.append(Text(EXPAND_HINT, style=P["muted"]))
    collapsed = Group(h, under(*body)) if body else h
    expanded = Group(h, under(full_body)) if full_body is not None else None
    return {"kind": "tool", "collapsed": collapsed, "expanded": expanded, "ok": ok}


# ---------------------------------------------------------------- 셀(대화 기록 한 칸)

class Cell(Static):
    DEFAULT_CSS = "Cell { margin: 0 0 1 0; height: auto; }"


class UserCell(Cell):
    DEFAULT_CSS = "UserCell { background: $panel; padding: 0 1; }"


class AnswerCell(Cell):
    DEFAULT_CSS = "AnswerCell { padding: 0 1 0 0; }"


class NoteCell(Cell):
    DEFAULT_CSS = "NoteCell { color: $text-muted; padding: 0 1 0 0; }"


class SystemCell(Cell):
    DEFAULT_CSS = "SystemCell { padding: 0 1; color: $text-muted; }"


class ErrorCell(Cell):
    DEFAULT_CSS = "ErrorCell { padding: 0 1; background: $error 8%; }"


class PlanCell(Cell):
    DEFAULT_CSS = "PlanCell { padding: 0 1 0 0; }"


class WelcomeCell(Cell):
    DEFAULT_CSS = "WelcomeCell { padding: 1 1; margin: 0 0 1 0; }"


class ToolCell(Cell):
    DEFAULT_CSS = """
    ToolCell { padding: 0 1 0 0; }
    ToolCell.-expandable:hover { background: $boost; }
    """

    def __init__(self, collapsed: RenderableType, expanded: RenderableType | None = None, ok: bool = True, **kw):
        super().__init__(collapsed, **kw)
        self._collapsed, self._expanded, self.expanded = collapsed, expanded, False
        if not ok:
            self.add_class("-failed")
        if expanded is not None:
            self.add_class("-expandable")

    def toggle(self, value: bool | None = None) -> None:
        if self._expanded is None:
            return
        self.expanded = (not self.expanded) if value is None else value
        self.update(self._expanded if self.expanded else self._collapsed)

    def on_click(self) -> None:
        self.toggle()


class LiveCell(Cell):
    """명령·테스트를 실행하는 동안 보이는 진행 줄(끝나면 결과 줄로 바뀐다)."""
    DEFAULT_CSS = "LiveCell { padding: 0 1 0 0; }"

    def __init__(self, label: str, **kw):
        super().__init__("", **kw)
        self.label, self.t0 = label, time.perf_counter()

    def tick(self, k: int) -> None:
        verb, _, what = self.label.partition(": ")
        name = "Test" if ("테스트" in verb or "pytest" in what) else "Run"
        self.update(Text.assemble((SPIN[k % len(SPIN)] + " ", P["brand"]), (name, f"bold {P['fg']}"),
                                  ("  " + (what or verb), P["fg"]),
                                  (f"  · {time.perf_counter() - self.t0:.0f}s", P["muted"])))


class ExploreCell(Cell):
    """연속된 read/search 를 한 칸으로 묶는다."""
    DEFAULT_CSS = "ExploreCell { padding: 0 1 0 0; }"

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.items: list[tuple[str, bool]] = []

    def add(self, head: str, ok: bool) -> None:
        self.items.append((head, ok))
        self.update(Group(*(tool_head(h, k) for h, k in self.items)))


# ---------------------------------------------------------------- 선택 목록(승인·플랜·막힘) — 입력창 자리에 붙는다

class ChoiceCard(Vertical, can_focus=True):
    """승인·플랜 실행·막힘 질문. 팝업이 아니라 입력창 자리에 붙고(그동안 입력창은 숨김),
    ↑↓ 로 고르고 Enter, 숫자 키 바로, Esc 는 정해 둔 기본값(보통 거절)."""

    DEFAULT_CSS = """
    ChoiceCard { height: auto; max-height: 70%; display: none; margin: 0 1; padding: 0 1;
                 border: round $border-blurred; background: $surface; }
    ChoiceCard #card-body { height: auto; }
    ChoiceCard #card-input { display: none; margin: 1 0 0 0; border: round $border-blurred; background: $panel; }
    ChoiceCard #card-input:focus { border: round $border; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.title, self.body, self.options, self.sel = "", None, [], 0
        self.on_choose: Callable[[str], None] | None = None
        self.esc_key, self.note, self.color = "", "", P["fg"]
        self.on_reason: Callable[[str], None] | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="card-body")
        yield Input(id="card-input")

    @property
    def reason_input(self) -> Input:
        return self.query_one("#card-input", Input)

    def body_text(self) -> str:
        return plain_text(self.query_one("#card-body", Static).content, 90)

    def _composer_visible(self, on: bool) -> None:
        with contextlib.suppress(Exception):
            self.app.query_one("#composer").display = on
            if not on:
                self.app.query_one("#popup").display = False

    def open(self, title: str, body: RenderableType | None, options: list[tuple[str, str]],
             on_choose: Callable[[str], None], esc_key: str, color: str | None = None) -> None:
        self.title, self.body, self.options, self.sel = title, body, options, 0
        self.on_choose, self.esc_key, self.note = on_choose, esc_key, ""
        self.color = color or P["fg"]
        self.on_reason = None
        self.reason_input.display = False
        self.reason_input.value = ""
        self._composer_visible(False)
        self.display = True
        self._redraw()
        self.focus()
        with contextlib.suppress(Exception):
            self.app.query_one("#busy").display = False      # 선택지가 뜨면 작업 중 표시는 숨긴다
            self.app.log_view.scroll_end(animate=False)

    def close(self) -> None:
        self.display = False
        self.on_choose = self.on_reason = None
        self._composer_visible(True)
        with contextlib.suppress(Exception):
            self.app.query_one(Composer).focus()

    def ask_reason(self, note: str, on_reason: Callable[[str], None]) -> None:
        """목록 아래 입력칸에 사유/힌트를 받는다(Enter 제출, Esc 빈 값)."""
        self.note, self.on_reason = note, on_reason
        self._redraw()
        inp = self.reason_input
        inp.placeholder = note
        inp.display = True
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        cb, self.on_reason = self.on_reason, None
        if cb:
            cb(event.value)

    def _redraw(self) -> None:
        parts: list[RenderableType] = []
        if self.body is not None:
            parts += [self.body, Text("")]
        parts.append(Text(self.title, style=f"bold {self.color}"))
        rows = Text()
        typing = self.on_reason is not None
        for i, (k, label) in enumerate(self.options):
            cur = i == self.sel and not typing
            if i:
                rows.append("\n")
            rows.append("❯ " if cur else "  ", style=P["brand"])
            rows.append(f"{i + 1}. {label}", style=f"bold {P['fg']}" if cur else P["muted"])
            if k == self.esc_key or (self.esc_key == "esc" and k == "n"):
                rows.append("  (esc)", style=P["border2"])
        parts.append(rows)
        self.query_one("#card-body", Static).update(Group(*parts))

    def choose(self, key: str) -> None:
        cb = self.on_choose
        if cb:
            cb(key)

    def on_key(self, event: events.Key) -> None:
        if not self.display:
            return
        k = event.key
        if self.reason_input.has_focus:
            if k == "escape":
                event.stop()
                cb, self.on_reason = self.on_reason, None
                if cb:
                    cb("")
            return
        if not self.options or self.on_choose is None:
            return
        if k in ("up", "down", "tab"):
            step = 1 if k in ("down", "tab") else -1
            self.sel = (self.sel + step) % len(self.options)
            self._redraw()
        elif k == "enter":
            self.choose(self.options[self.sel][0])
        elif k == "escape":
            self.choose(self.esc_key)
        elif event.character and event.character.isdigit() and 1 <= int(event.character) <= len(self.options):
            self.choose(self.options[int(event.character) - 1][0])
        elif event.character and event.character.lower() in {o[0] for o in self.options}:
            self.choose(event.character.lower())
        else:
            return
        event.stop()
        event.prevent_default()


# ---------------------------------------------------------------- 입력창

class Composer(TextArea):
    """여러 줄 입력창. Enter 전송, Ctrl+J(또는 Shift/Alt/Ctrl+Enter, 줄 끝 \\ 뒤 Enter) 줄바꿈, ↑/↓ 입력 기록."""

    BINDINGS = [
        Binding("ctrl+j,shift+enter,alt+enter,ctrl+enter", "newline", "줄바꿈", show=False, priority=True),
        Binding("tab", "complete", "완성", show=False),
        Binding("escape", "composer_escape", "취소", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, history: list[str], **kw):
        super().__init__(soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False, **kw)
        self.inputs = history
        self.h_idx: int | None = None
        self.draft = ""

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        self.app.popup_complete(submit=False)

    def action_composer_escape(self) -> None:
        self.app.handle_escape()

    def action_cursor_up(self, select: bool = False) -> None:
        if self.app.popup_move(-1):
            return
        if not select and self.cursor_location[0] == 0 and self.inputs:
            if self.h_idx is None:
                self.draft, self.h_idx = self.text, len(self.inputs)
            if self.h_idx > 0:
                self.h_idx -= 1
                self._set(self.inputs[self.h_idx])
            return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if self.app.popup_move(1):
            return
        if not select and self.h_idx is not None and self.cursor_location[0] == self.document.line_count - 1:
            self.h_idx += 1
            if self.h_idx >= len(self.inputs):
                self.h_idx = None
                self._set(self.draft)
            else:
                self._set(self.inputs[self.h_idx])
            return
        super().action_cursor_down(select)

    def _set(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.app.popup_complete(submit=True):
                return
            row, col = self.cursor_location
            line = self.document[row]
            if col > 0 and line[:col].endswith("\\"):        # 줄 끝 \ + Enter = 줄바꿈(Shift+Enter 를 못 받는 터미널용)
                self.replace("\n", (row, col - 1), (row, col))
                return
            self.h_idx = None
            self.post_message(self.Submitted(self.text))
            return
        await super()._on_key(event)


# ---------------------------------------------------------------- 화면용 UI (ChatSession·루프가 부르는 쪽)

class TuiUI(UI):
    """chat.UI 와 같은 인터페이스(out·event·busy·tool_lines). 줄 대신 대화창 셀로 보낸다."""

    def __init__(self, ws: Path):
        # UI.__init__ 은 부르지 않는다(stdout tty 검사·os.system 을 피함)
        self.out = self._out
        self.tty = True
        self.color = True          # 색 코드는 셀에서 Text.from_ansi 로 바꾼다
        self.ws = ws
        self.app: CapApp | None = None
        self._buf: list[str] | None = None
        self.plain = UI(out=lambda s="": None)
        self.plain.tty = self.plain.color = False
        self.plain.ws = ws

    def _out(self, s: str = "") -> None:
        if self._buf is not None:
            self._buf.append(s)
        elif self.app is not None:
            self.app.post(self.app.add_system, [s])

    @contextlib.contextmanager
    def capture(self):
        self._buf = []
        try:
            yield self._buf
        finally:
            self._buf = None

    def flush(self) -> None:
        if self._buf and self.app is not None:
            self.app.post(self.app.add_system, list(self._buf))
            self._buf.clear()

    @contextlib.contextmanager
    def busy(self, label: str):
        app = self.app
        if app is None:
            yield
            return
        app.check_cancel()
        prev = app.busy_label
        app.busy_label = label
        live = label.startswith(("실행 중", "테스트 확인 중"))
        if live:
            app.post(app.start_live, label if ": " in label else f"{label}: {app.s.test_cmd}")
        try:
            yield
        finally:
            app.busy_label = prev
            if live:
                app.post(app.end_live)

    def stream(self, text: str) -> None:
        if self.app is not None and text.strip():
            self.app.post(self.app.show_stream, text)

    def event(self, kind: str, **d) -> None:
        app = self.app
        if app is None:
            return
        app.check_cancel()
        if kind == "tool":
            v = tool_view(self.plain, self.ws, d["name"], d["args"], d["res"], d.get("before"))
            app.post(app.add_tool, v)
        elif kind == "deny":
            app.post(app.add_cell, Text.assemble(("✗ Blocked  ", P["error"]), (short_call(d["tool"], d["args"], self.ws), P["fg"]),
                                                 (f"  — {d['reason']}", P["muted"])), ErrorCell)
        elif kind == "subagent_start":
            app.post(app.add_cell, Text.assemble(("◌ ", P["brand"]), ("Agent", f"bold {P['fg']}"),
                                                 (f"  {d['label']}", P["fg"]), (f"  {d['task'][:80]}", P["muted"])))
        elif kind == "subagent_done":
            ok = d["status"] == "done"
            head = Text.assemble(("● ", P["success"] if ok else P["error"]), ("Agent", f"bold {P['fg']}"),
                                 (f"  {d['label']}", P["fg"]), (f"  → {d['status']} · {d['calls']} calls · {d['seconds']:.0f}s", P["muted"]))
            lines = (d.get("summary") or "").splitlines()
            body = [Text("\n".join(lines[:8]), style=P["muted"])] + ([Text(EXPAND_HINT, style=P["muted"])] if len(lines) > 8 else [])
            if d.get("changed"):
                body.append(Text("changed " + ", ".join(d["changed"]), style=P["muted"]))
            full = Group(head, under(Text("\n".join(lines), style=P["muted"]))) if len(lines) > 8 else None
            app.post(app.add_tool, {"kind": "tool", "collapsed": Group(head, under(*body)), "expanded": full, "ok": ok})
        elif kind == "verify":
            ok = d["ok"]
            c = P["success"] if ok else P["error"]
            app.post(app.add_cell, Text.assemble(("✓ " if ok else "✗ ", c), ("Verified  " if ok else "Tests failed  ", c),
                                                 (d["text"], P["fg"])))
        elif kind == "note":
            app.post(app.add_cell, gutter("◦ ", Markdown(d["text"].strip(), code_theme="monokai"), P["muted"]),
                     NoteCell)
        elif kind == "compact":
            app.post(app.add_cell, Text(f"◦ Compacted context: ~{d['before']:,} → {d['after']:,} tokens",
                                        style=P["muted"]))


# ---------------------------------------------------------------- 앱

class CapApp(App):
    TITLE = "CAP"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; background: $background; }
    #log { height: 1fr; padding: 1 2 0 2; scrollbar-size-vertical: 1; }
    #busy { height: 1; padding: 0 2; display: none; }
    #popup { height: auto; max-height: 12; display: none; margin: 0 1; border: round $border-blurred;
             background: $surface; }
    #composer { height: auto; min-height: 3; max-height: 10; margin: 0 1; border: round $border-blurred;
                background: $background; padding: 0 1; }
    #composer:focus { border: round $border; }
    #bottom { height: 1; padding: 0 2; }
    #mode { width: auto; }
    #stats { width: 1fr; text-align: right; }
    """
    BINDINGS = [
        Binding("ctrl+c", "ctrl_c", "interrupt / quit", priority=True, show=False),
        Binding("escape", "app_escape", "interrupt", show=False),
        Binding("shift+tab,alt+m", "cycle_mode", "cycle mode", priority=True, show=False),
        Binding("ctrl+l", "clear_screen", "clear", priority=True, show=False),
        Binding("ctrl+o", "toggle_expand", "expand", priority=True, show=False),
        Binding("alt+v", "paste_image", "paste image", priority=True, show=False),
        Binding("pageup", "page(-1)", show=False, priority=True),
        Binding("pagedown", "page(1)", show=False, priority=True),
    ]

    def __init__(self, session: ChatSession, ui: TuiUI, resumed: bool = False):
        super().__init__()
        self.register_theme(CAP_THEME)
        self.theme = "cap"
        self.s, self.ui, self.ws, self.resumed = session, ui, session.ws, resumed
        ui.app = self
        self.busy_label = "생각 중"
        self.job: threading.Thread | None = None
        self.job_t0 = 0.0
        self.last_escape = 0.0
        self.job_cancel = False
        self.job_finished = False
        self.llm_call: threading.Thread | None = None
        self._llm_orig: Callable | None = None
        self.after_job: Callable[[], None] | None = None
        self.queue: list[str] = []
        self.reply_cb: Callable[[str], None] | None = None
        self.last_ctrl_c = 0.0
        self.ctx_est = 0
        self.spin = 0
        self.mode_flash = 0.0
        self.focus_known, self.app_focused = False, True
        self.live: LiveCell | None = None
        self.stream_cell: Static | None = None
        self.welcome: WelcomeCell | None = None
        self.main_tid = threading.get_ident()
        self.history = self._load_history()
        self.popup_kind = "slash"
        self._files: list[str] | None = None

    # -- 구성
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="log")
        yield Static(id="busy")
        yield OptionList(id="popup")
        yield ChoiceCard(id="card")
        yield Composer(self.history, id="composer", placeholder=PLACEHOLDER)
        with Horizontal(id="bottom"):
            yield Static(id="mode")
            yield Static(id="stats")

    def on_mount(self) -> None:
        self.main_tid = threading.get_ident()
        self._install_llm_guard()
        self.query_one("#log").can_focus = False
        self.query_one("#popup", OptionList).can_focus = False
        if self.resumed:
            self.replay_history()
        else:
            self.add_welcome()
        self.ctx_est = self._estimate()
        self.refresh_bars()
        self.set_interval(0.08, self._tick)
        self.tasks_told: set[int] = {n for n, _ in self._finished_tasks()}   # 시작 전에 끝나 있던 것은 알리지 않음
        self.set_interval(15, self._check_tasks)
        self.status_text = ""
        from .statusline import command as _sl_cmd
        self.status_cmd = _sl_cmd()
        if self.status_cmd:                                    # 상태 줄 명령(Claude Code statusLine 형식)
            self._refresh_status()
            self.set_interval(10, self._refresh_status)
        self.query_one(Composer).focus()
        # 다시 연 세션: 승인 대기면 바로 이어 묻고, 중간에 멈춘 턴이면 알린다
        if self.resumed and self.s.pending():
            self.add_cell(Text("Resuming a pending approval from last time.", style=P["muted"]))
            self.start_job(lambda: self._turn_job(None))
        elif self.resumed and self.s.stopped_midway():
            self.add_cell(Text(f"The last turn stopped midway (next step: {self.s.stopped_midway()}). "
                               "/continue picks it up from the last checkpoint.", style=P["warning"]))

    def _refresh_status(self) -> None:
        """상태 줄 명령은 별도 스레드에서(대화창이 멈추지 않게) 돌리고 결과만 반영한다."""
        if getattr(self, "_status_busy", False):
            return
        self._status_busy = True
        from .statusline import info, render
        data = info(self.ws, self.s.sid, self.s.approval["mode"], llm.MODEL, self.ctx_est, llm.CONTEXT_WINDOW)

        def work() -> None:
            text = render(self.ws, data, self.status_cmd)
            self._status_busy = False

            def apply() -> None:
                self.status_text = text
                self.refresh_bars()
            with contextlib.suppress(Exception):
                self.call_from_thread(apply)
        threading.Thread(target=work, daemon=True, name="cap-status").start()

    def _finished_tasks(self) -> list:
        try:
            from .tasks import finished
            return finished(self.ws)
        except Exception:  # noqa: BLE001 — 알림이 대화창을 깨면 안 된다
            return []

    def _check_tasks(self) -> None:
        """백그라운드 작업(/task)이 끝나면 한 줄로 알린다."""
        for n, res in self._finished_tasks():
            if n in self.tasks_told:
                continue
            self.tasks_told.add(n)
            ok = res.get("status") == "done"
            self.add_cell(Text(f"{'✓' if ok else '!'} Background task {n} finished — {res.get('status')} · "
                               f"{res.get('verify_note') or 'not verified'} · /tasks to see it",
                               style=P["success"] if ok else P["warning"]))
            with contextlib.suppress(Exception):
                self.bell()


    # -- 스레드 → 화면
    def post(self, fn: Callable, *args) -> None:
        if threading.get_ident() == self.main_tid:
            fn(*args)
            return
        # 작업 스레드는 화면을 기다리지 않는다(call_from_thread 는 결과를 기다리며 막힐 수 있음): 화면 루프에 순서대로 넣기만 한다.
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def run() -> None:
            try:
                with self._context():
                    fn(*args)
            except NoMatches:          # 앱이 닫히는 중
                pass
            except Exception as exc:  # noqa: BLE001 — 화면 갱신 하나가 실패해도 대화창은 산다
                with contextlib.suppress(Exception):
                    self.log.error("cap ui callback failed", exc)
        with contextlib.suppress(RuntimeError):     # 루프가 닫히는 중이면 버린다
            loop.call_soon_threadsafe(run)

    # -- 대화 기록
    @property
    def log_view(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def show_stream(self, text: str) -> None:
        """모델이 쓰는 중인 글자(Claude Code·Codex 처럼 오는 대로). 다음 칸이 붙으면 사라지고 제대로 된 칸이 대신한다."""
        body = gutter("● ", Text(text[-3000:], style=P["muted"]), P["muted"])
        if self.stream_cell is not None and self.stream_cell.parent is not None:
            self.stream_cell.update(body)
            self.log_view.scroll_end(animate=False)
            return
        self.stream_cell = AnswerCell(body)
        self._mount(self.stream_cell)

    def _mount(self, w: Static) -> None:
        log = self.log_view
        if self.stream_cell is not None and w is not self.stream_cell:
            with contextlib.suppress(Exception):
                self.stream_cell.remove()
            self.stream_cell = None
        if self.welcome is not None and not isinstance(w, WelcomeCell):
            self.welcome.remove()          # 대화가 시작되면 시작 화면은 사라진다
            self.welcome = None
        if self.live is not None and w is not self.live and self.live.parent is log:
            log.mount(w, before=self.live)
        else:
            log.mount(w)
        log.scroll_end(animate=False)

    def add_cell(self, renderable: RenderableType, cls: type[Cell] = Cell) -> None:
        self._mount(cls(renderable))

    def add_system(self, lines: list[str]) -> None:
        text = "\n".join(lines).rstrip("\n")
        if text.strip():
            self._mount(SystemCell(Text.from_ansi(text)))

    def add_user(self, text: str) -> None:
        self._mount(UserCell(Text.assemble(("› ", P["muted"]), (text, P["fg"]))))

    def add_answer(self, text: str) -> None:
        self._mount(AnswerCell(gutter("● ", Markdown(text, code_theme="monokai"), P["fg"])))

    def add_tool(self, v: dict) -> None:
        log = self.log_view
        kids = [c for c in log.children if c is not self.live]
        last = kids[-1] if kids else None
        if v["kind"] == "explore":
            if not isinstance(last, ExploreCell):
                last = ExploreCell()
                self._mount(last)
            last.add(v["head"], v["ok"])
            log.scroll_end(animate=False)
        elif v["kind"] == "todo":
            body = Text()
            for i, (done, item) in enumerate(v["items"]):
                if i:
                    body.append("\n")
                body.append("☒ " if done else "☐ ", style=P["success"] if done else P["muted"])
                body.append(item, style=f"{P['muted']} strike" if done else P["fg"])
            self._mount(PlanCell(Group(Text.assemble(("● ", P["success"]), ("Update Todos", f"bold {P['fg']}")),
                                       under(body))))
        else:
            self.end_live()
            self._mount(ToolCell(v["collapsed"], v["expanded"], v.get("ok", True)))

    def start_live(self, label: str) -> None:
        self.end_live()
        self.live = LiveCell(label)
        self._mount(self.live)
        self.live.tick(self.spin)

    def end_live(self) -> None:
        if self.live is not None:
            self.live.remove()
            self.live = None

    def add_welcome(self) -> None:
        m = self.s.approval["mode"]
        logo = Text("\n".join(LOGO), style=f"bold {P['brand']}")
        info = Text()
        info.append("CAP", style=f"bold {P['fg']}")
        info.append("  Code As Planned\n", style=P["muted"])
        info.append(llm.MODEL, style=P["fg"])
        info.append("  ·  " + _short_path(self.ws) + "\n", style=P["muted"])
        info.append(f"tests: {self.s.test_cmd or 'none'}", style=P["muted"])
        if self.s.inst_names:
            info.append("  ·  instructions: " + ", ".join(self.s.inst_names), style=P["muted"])
        head = Table.grid(padding=(0, 3))
        head.add_column(no_wrap=True)
        head.add_column()
        head.add_row(logo, info)
        bad = [ln for ln in doctor_lines(self.ws, self.s.test_cmd, ping=False) if ln.startswith("✗")]
        body = Group(head, Text(""), Text("\n".join(bad) + "\n/doctor for details", style=P["warning"])) if bad else head
        self.welcome = WelcomeCell(body)
        self._mount(self.welcome)

    def replay_history(self) -> None:
        v = self.s.values()
        log, msgs = v.get("turn_log") or [], v.get("messages") or []
        self.add_cell(Text(f"Resumed session {self.s.sid}", style=P["muted"]))
        for i, t in enumerate(log):
            self.add_user(shown_goal(t.get("goal", "")))
            start = t.get("msg_idx", -1)
            if start < 0:
                continue
            nxt = log[i + 1].get("msg_idx", -1) if i + 1 < len(log) else -1
            end = nxt if nxt >= 0 else len(msgs)
            for m in reversed(msgs[start:end]):
                if m.get("role") == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
                    self.add_answer(m["content"].strip())
                    break
        if log:
            self.add_cell(Text(f"── {len(log)} previous turn{'s' if len(log) > 1 else ''}", style=P["muted"]))

    def show_result(self, warn: str, ans: str, foot: list[str], est: int) -> None:
        self.end_live()
        if warn:
            self.add_cell(Text("⚠ " + warn, style=P["warning"]), ErrorCell)
        if ans:
            self.add_answer(ans)
        elif not warn:
            self.add_cell(Text("(no reply)", style=P["muted"]))
        self.add_cell(Text("── " + " · ".join(foot), style=P["muted"]))
        self.ctx_est = est
        self.refresh_bars()

    # -- 하단 줄
    def refresh_bars(self) -> None:
        try:
            self._refresh_bars()
        except NoMatches:
            pass

    def _refresh_bars(self) -> None:
        m = self.s.approval["mode"]
        color = MODE_COLOR[m]
        flashing = time.monotonic() < self.mode_flash
        line = Text.assemble((f"{MODE_ICON[m]} {MODE_NAME[m]}", f"bold {color}"))
        from . import sandbox as sbx
        if m != "bypass" and sbx.backend():
            line.append("  · sandboxed", style=P["success"])
        card = self.query_one(ChoiceCard)
        if flashing:
            line.append("  — " + MODE_HELP[m], style=color)
        elif card.display:
            line.append("  ·  ↑↓ select · enter confirm · esc cancel", style=P["muted"])
        elif self.running:
            line.append("  ·  type ahead — messages are queued", style=P["muted"])
        else:
            line.append("  (shift+tab to cycle)", style=P["muted"])
        self.query_one("#mode", Static).update(line)
        cw = llm.CONTEXT_WINDOW
        st = llm.STATS
        used = st.get("last_prompt") if self.running and st.get("last_prompt") else self.ctx_est
        frac = min(1.0, used / cw) if cw else 0.0
        mc = P["muted"] if frac < 0.6 else P["fg"] if frac < 0.85 else P["error"]
        stats = Text.assemble((f"{frac:.0%} context", mc), (f"  ·  {st['calls']} calls  ·  {st['seconds']:.0f}s", P["muted"]))
        if getattr(self, "status_text", ""):
            stats = Text.assemble((self.status_text, P["fg"]), ("  ·  ", P["muted"]), stats)
        self.query_one("#stats", Static).update(stats)

    def _estimate(self) -> int:
        try:
            msgs = self.s.values().get("messages") or []
            return llm.est_messages(msgs, None) if msgs else 0
        except Exception:  # noqa: BLE001
            return 0

    def _tick(self) -> None:
        try:
            self._tick_once()
        except NoMatches:          # 앱이 닫히는 중(위젯이 이미 내려감)
            pass

    def _tick_once(self) -> None:
        busy = self.query_one("#busy", Static)
        if self.job is not None and not self.job.is_alive():
            self._job_done()
        self.spin += 1
        if self.live is not None:
            self.live.tick(self.spin)
        card = self.query_one(ChoiceCard)
        if self.running and not card.display and self.reply_cb is None:
            dt = time.perf_counter() - self.job_t0
            b = self.busy_label
            label = ("Interrupting…" if self.job_cancel else "Running…" if b.startswith("실행") else
                     "Testing…" if b.startswith("테스트") else "Thinking…")
            busy.update(Text.assemble((SPIN[self.spin % len(SPIN)] + " ", P["brand"]), (label, P["fg"]),
                                      (f"  ({dt:.0f}s · esc to interrupt)", P["muted"])))
            busy.display = True
        else:
            busy.display = False
        if self.spin % 6 == 0:
            self.refresh_bars()

    # -- 작업(백그라운드 스레드)
    @property
    def running(self) -> bool:
        return self.job is not None and self.job.is_alive()

    def check_cancel(self) -> None:
        """작업 스레드가 화면 훅을 지날 때: 중단 요청이 있으면 그 자리에서 멈춘다."""
        if self.job_cancel and threading.current_thread() is self.job and not self.job_finished:
            raise KeyboardInterrupt

    def start_job(self, fn: Callable[[], None]) -> None:
        self.job_cancel, self.job_finished, self.busy_label = False, False, "생각 중"
        self.job_t0 = time.perf_counter()

        def report(renderable: RenderableType, cls: type[Cell] = Cell) -> None:
            self.post(self.add_cell, renderable, cls)

        def wrap():
            try:
                fn()
            except KeyboardInterrupt:
                report(Text("■ Interrupted · tell CAP what to do instead", style=P["muted"]))
            except RuntimeError as exc:
                report(Text(f"Error: {llm.scrub(str(exc))[:300]}{llm.error_hint(exc)}", style=P["error"]), ErrorCell)
            except Exception as exc:  # noqa: BLE001 — 화면은 살려 둔다
                report(Text(f"Error: {type(exc).__name__}: {llm.scrub(str(exc))[:300]}", style=P["error"]), ErrorCell)
            finally:
                self.job_finished = True

        self.job = threading.Thread(target=wrap, daemon=True, name="cap-turn")
        self.job.start()
        self.refresh_bars()

    def on_app_blur(self) -> None:           # 터미널이 창 포커스를 알려 주면(Windows Terminal 등) 알림을 그때만
        self.focus_known, self.app_focused = True, False

    def on_app_focus(self) -> None:
        self.focus_known, self.app_focused = True, True

    def _job_done(self) -> None:
        took = time.perf_counter() - self.job_t0
        if took > 20:          # 긴 작업이 끝나면 알림(창을 보고 있지 않아도)
            with contextlib.suppress(Exception):
                self.bell()
            away = (not self.app_focused) if getattr(self, "focus_known", False) else took > 60
            if away:           # 다른 창을 보고 있으면 Windows 알림
                from .extras import desktop_notify
                ans = " ".join(self.s.last_answer(self.s.values()).split())[:120] if self.s else ""
                with contextlib.suppress(Exception):
                    desktop_notify(f"CAP finished ({took:.0f}s)", ans or "The task is done.")
        self.job = None
        self.job_cancel = False
        self.reply_cb = None
        self.end_live()
        card = self.query_one(ChoiceCard)
        if card.on_choose is not None and card.display:
            card.close()
        self.ctx_est = self._estimate()
        self.refresh_bars()
        comp = self.query_one(Composer)
        comp.placeholder = PLACEHOLDER
        if not card.display:
            comp.focus()
        nxt, self.after_job = self.after_job, None
        if nxt:
            nxt()
        elif self.s.steer_box and not card.display:   # 턴이 끝날 때까지 못 넘긴 말은 다음 요청으로
            text = "\n".join(self.s.take_steer())
            self.start_job(lambda: self._turn_job(text))
        elif self.queue and not card.display:
            self.submit(self.queue.pop(0))

    def cancel_job(self) -> None:
        if not self.running or self.job_cancel:
            return
        self.job_cancel = True
        self.s.cancel_event.set()
        self.after_job = None
        card = self.query_one(ChoiceCard)
        if card.display:
            card.close()
        if self.reply_cb:
            cb, self.reply_cb = self.reply_cb, None
            cb("\x00cancel")
        call = self.llm_call
        if call is not None and call.is_alive():     # 기다리던 모델 호출은 버린다: 연결을 닫고 재시도도 막는다
            call.cap_abandoned = True
            threading.Thread(target=_close_llm_client, daemon=True).start()

    def _install_llm_guard(self) -> None:
        """턴 스레드의 모델 호출을 보조 스레드에서 돌리고, 기다리는 동안 중단 요청을 본다.
        중단되면 턴 스레드는 이 자리(잠금을 쥐지 않은 곳)에서 KeyboardInterrupt 로 빠진다 — 줄 모드 Ctrl+C 와 같은 효과.
        (다른 스레드에 비동기 예외를 심는 방식은 SQLite 잠금 안쪽에 떨어질 수 있어 쓰지 않는다.)"""
        orig = self._llm_orig = llm.chat

        def guarded(*a, **kw):
            if threading.current_thread() is not self.job:
                return orig(*a, **kw)
            self.check_cancel()
            box: dict = {}

            def call():
                try:
                    box["r"] = orig(*a, **kw)
                except BaseException as exc:  # noqa: BLE001
                    box["e"] = exc
            th = threading.Thread(target=call, daemon=True, name="cap-llm")
            self.llm_call = th
            th.start()
            while th.is_alive():
                th.join(0.05)
                if self.job_cancel:
                    th.cap_abandoned = True
                    raise KeyboardInterrupt
            self.llm_call = None
            if "e" in box:
                raise box["e"]
            return box["r"]
        llm.chat = guarded

    def _result_parts(self, r: dict) -> tuple[str, str, list[str]]:
        """턴 결과 → (경고, 답, 바닥줄)."""
        _, ans, _ = turn_summary(self.s, r)
        v = r["values"]
        status = v.get("status")
        if status == "failed":
            warn = "Couldn't finish: " + (v.get("verify_note") or "turn limit")
        elif status == "escalated":
            warn = "Stopped: " + (v.get("verify_note") or "you chose to stop")
        elif status == "blocked":
            warn = v.get("escalate_reason") or "Blocked by a hook"
        else:
            warn = ""
        foot = [f"{r['calls']} model call{'s' if r['calls'] != 1 else ''}", f"{r['seconds']:.0f}s"]
        if v.get("turn_changed"):
            foot.append("changed " + ", ".join(v["turn_changed"]))
        if (v.get("verify_note") or "").startswith("not verified"):
            foot.append(v["verify_note"])
        return warn, ans, foot

    def _turn_job(self, text: str | None) -> None:
        s = self.s
        r = s.run_turn(text, ask=self._ask_from_thread)
        warn, ans, foot = self._result_parts(r)
        msgs = r["values"].get("messages") or []
        est = llm.est_messages(msgs, None) if msgs else 0
        self.post(self.show_result, warn, ans, foot, est)
        if s.approval["mode"] == "plan":
            plan = s.last_plan(r["values"])
            if plan:
                self.after_job = lambda: self.offer_plan(plan)

    def _goal_job(self, objective: str) -> None:
        def on_turn(r):
            warn, ans, foot = self._result_parts(r)
            msgs = r["values"].get("messages") or []
            self.post(self.show_result, warn, ans, foot, llm.est_messages(msgs, None) if msgs else 0)
        end = self.s.run_goal(objective, ask=self._ask_from_thread, on_turn=on_turn,
                              on_note=lambda t: self.post(self.add_cell, Text(t, style=P["brand"])))
        self.post(self.add_cell, Text("◎ " + end, style=P["success"] if "done" in end else P["warning"]))

    def _command_job(self, line: str) -> None:
        cmd = line.split(" ", 1)[0]
        optional = cmd in ("/spec", "/graph", "/run")
        old = self.s
        with (contextlib.nullcontext([]) if optional else self.ui.capture()) as buf, self._redirect_io():
            s2, quit_, text = handle_command(self.s, self.ws, line)
            lines = list(buf)
        self.s = s2
        if s2 is not old:
            self.resumed = cmd in ("/resume", "/fork")
            self.post(self.reset_view, self.resumed)
        elif cmd == "/rewind" and line[len(cmd):].strip().isdigit():   # 되돌린 대화로 화면을 다시 그린다
            self.resumed = True
            self.post(self.reset_view, True)
        if lines:
            self.post(self.add_system, lines)
        self.post(self.refresh_bars)
        if quit_:
            self.post(self.exit)
        elif text == CONTINUE:          # /continue: 멈춘 턴을 마지막 체크포인트부터
            self.after_job = lambda: self.start_job(lambda: self._turn_job(None))
        elif text:
            builtin = cmd in {c for c, _, _ in SLASH_COMMANDS}
            self.after_job = lambda: self.submit(text, echo=cmd == "/plan" or not builtin)   # 사용자 명령은 펼친 요청을 보여 준다

    @contextlib.contextmanager
    def _redirect_io(self):
        """선택 기능(/spec /graph /run)이 쓰는 print·input 을 대화창으로 돌린다."""
        from . import cli
        old_out, old_input = cli._out, builtins.input
        cli._out = lambda s="": self.ui.out(s)
        builtins.input = self._input_from_thread
        try:
            yield
        finally:
            cli._out, builtins.input = old_out, old_input

    def _wait(self, box: dict, ev: threading.Event) -> str:
        while not ev.wait(0.1):
            if self.job_cancel:
                raise KeyboardInterrupt
        if box.get("answer") == "\x00cancel":
            raise KeyboardInterrupt
        return box["answer"]

    def _input_from_thread(self, prompt: str = "") -> str:
        self.ui.flush()
        box, ev = {}, threading.Event()
        self.post(self.ask_text, prompt, box, ev)
        return self._wait(box, ev)

    def _ask_from_thread(self, payload: dict) -> str:
        box, ev = {}, threading.Event()
        self.post(self.show_ask, payload, box, ev)
        return self._wait(box, ev)

    # -- 묻기 (화면 스레드)
    def _answer(self, box: dict, ev: threading.Event, value: str) -> None:
        box["answer"] = value
        ev.set()

    def ask_text(self, prompt: str, box: dict, ev: threading.Event) -> None:
        """선택 기능의 input() 질문: 입력창으로 답을 받는다."""
        self.add_cell(Text(prompt.strip() or "Input", style=P["fg"]))
        comp = self.query_one(Composer)
        comp.placeholder = "Type your answer and press enter (esc = empty)"

        def cb(text: str) -> None:
            if text != "\x00cancel":
                self.add_cell(Text("↳ " + (text or "(empty)"), style=P["muted"]))
            comp.placeholder = PLACEHOLDER
            self._answer(box, ev, text)
        self.reply_cb = cb
        comp.focus()

    def show_ask(self, payload: dict, box: dict, ev: threading.Event) -> None:
        card = self.query_one(ChoiceCard)
        if payload.get("kind") == "question":       # 모델이 묻는 질문(ask_user): 고르거나 직접 쓰거나
            opts = payload.get("options") or []

            def choose_q(k: str) -> None:
                def done(ans: str) -> None:
                    card.close()
                    self._record("↳ " + (ans or "(no answer — CAP decides)"), P["muted"])
                    self._answer(box, ev, ans)
                    self.refresh_bars()
                if k == "t":
                    card.ask_reason("Type your answer and press enter", lambda t: done(t.strip()))
                elif k.startswith("o"):
                    done(opts[int(k[1:])])
                else:
                    done("")
            card.open(payload.get("question", ""), None,
                      [(f"o{i}", o) for i, o in enumerate(opts)] + [("t", "Type something else")], choose_q, "esc",
                      color=P["brand"])
            self.refresh_bars()
            return
        if payload.get("kind") != "approval":       # 사람에게 넘김(escalate): 힌트를 주면 이어서, 아니면 중단
            out = (payload.get("output") or "").strip().splitlines()[-12:]
            body = Text("\n".join(out), style=P["muted"]) if out else None

            def choose_esc(k: str) -> None:
                if k == "h":
                    card.ask_reason("Type a hint and press enter (empty = stop)",
                                    lambda t: (card.close(), self._answer(box, ev, t.strip() or "abort")))
                else:
                    card.close()
                    self.add_cell(Text("■ Stopped", style=P["muted"]))
                    self._answer(box, ev, "abort")
                self.refresh_bars()
            card.open(f"CAP is stuck — {payload.get('reason', '')}", body,
                      [("h", "Give a hint and continue"), ("s", "Stop")], choose_esc, "s", color=P["error"])
            self.refresh_bars()
            return
        calls = payload.get("calls", [])
        parts: list[RenderableType] = []
        for c in calls:
            verb, _, target = short_call(c["tool"], c["args"], self.ws).partition(" ")
            kind = {"edit": "Edit file", "write": "Create file", "run": "Run command"}.get(verb, verb)
            parts.append(Text.assemble((kind, f"bold {P['fg']}"), ("  " + target, P["fg"])))
            prev = c.get("preview") or []
            if prev:
                parts.append(under(diff_text(prev)))
        edits = all(c["tool"] in ("edit_file", "write_file") for c in calls)
        cats = {c.get("category") for c in calls}
        project = ""                          # '이 프로젝트에서는 다시 묻지 않기'(명령 앞부분·MCP 서버 단위만)
        if len(calls) == 1 and calls[0]["tool"] in ("plugin_install", "skill_install"):
            kind_, ident = calls[0]["tool"].split("_")[0], calls[0]["args"].get("id", "")
            what = (f"Install plugin {ident}? It can add commands, hooks and MCP servers (checked before install)."
                    if kind_ == "plugin" else f"Install skill {ident}? Its instructions will guide CAP (checked before install).")
            always = ""                       # 설치는 늘 묻는다(세션 허용 없음)
        elif cats == {"sandbox_escape"}:
            what = "Run this command outside the sandbox (network and writes outside the folder allowed)?"
            always = "Yes, and don't ask again for sandbox escapes this session"
        elif all(c["tool"] == "run" for c in calls):
            prefixes = sorted({c["category"][5:] for c in calls if str(c.get("category", "")).startswith("exec:")})
            what = "Do you want to run this command?"
            always = (f"Yes, and don't ask again for `{prefixes[0]}` commands this session" if len(prefixes) == 1
                      else "Yes, and don't ask again for these commands this session")
            if project_ok(calls):
                project = (f"Yes, and don't ask again for `{prefixes[0]}` commands in this project" if len(prefixes) == 1
                           else "Yes, and don't ask again for these commands in this project")
        elif edits:
            paths = ", ".join(dict.fromkeys(disp_path(c["args"].get("path", ""), self.ws) for c in calls))
            creates = all(c["tool"] == "write_file" and not Path(self.ws, c["args"].get("path", "")).exists() for c in calls)
            what = f"Do you want to create {paths}?" if creates else f"Do you want to make this edit to {paths}?"
            always = "Yes, allow all edits during this session"
        else:
            what, always = "Do you want to allow this?", "Yes, and don't ask again this session"
            if project_ok(calls):
                project = "Yes, and don't ask again for this server in this project"
        summary = ", ".join(short_call(c["tool"], c["args"], self.ws) for c in calls)

        def choose(k: str) -> None:
            if k == "y":
                self._record(f"✓ Allowed  {summary}", P["success"])
                card.close()
                self._answer(box, ev, "y")
            elif k == "a":
                self._record(f"✓ Allowed for this session  {summary}", P["success"])
                card.close()
                self._answer(box, ev, "a")
            elif k == "p":
                self._record(f"✓ Allowed in this project  {summary}  (/permissions forget to undo)", P["success"])
                card.close()
                self._answer(box, ev, "p")
            elif k == "n":
                def reason(t: str) -> None:
                    card.close()
                    t = t.strip()
                    if t and t.lower() in YES_WORDS:
                        t = "declined: " + t
                    self._record(f"✗ Declined  {summary}" + (f" — {t}" if t else ""), P["error"])
                    self._answer(box, ev, t or "n")
                card.ask_reason("Tell CAP what to do differently (enter to send, empty to just decline)", reason)
            else:   # Esc: 그냥 거절
                card.close()
                self._record(f"✗ Declined  {summary}", P["error"])
                self._answer(box, ev, "n")
            self.refresh_bars()
        if payload.get("agent"):
            what = f"[{payload['agent']}] " + what
        card.open(what, Group(*parts),
                  [("y", "Yes")] + ([("a", always)] if always else []) + ([("p", project)] if project else [])
                  + [("n", "No, and tell CAP what to do differently")], choose, "esc")
        self.refresh_bars()

    def _record(self, text: str, color: str) -> None:
        self.add_cell(Text(text, style=color))

    def offer_plan(self, plan: list[str]) -> None:
        card = self.query_one(ChoiceCard)

        def choose(k: str) -> None:
            card.close()
            mode = {"y": "ask", "a": "auto", "b": "bypass"}.get(k)
            if mode:
                set_mode(self.s, mode)
                self.mode_flash = time.monotonic() + 1.5
                self.refresh_bars()
                self.add_cell(Text(f"› Carrying out the plan ({MODE_NAME[mode]})", style=P["muted"]))
                self.submit(PLAN_GO, echo=False)
            else:
                self.add_cell(Text("Still in plan mode — tell CAP what to change.", style=P["muted"]))
                self.refresh_bars()
        # 계획은 바로 위 'Update Todos' 에 보이므로 여기서는 묻기만 한다
        card.open(f"Would you like to proceed with this plan ({len(plan)} steps)?", None,
                  [("a", "Yes, and auto-accept edits"), ("y", "Yes, and manually approve edits"),
                   ("b", "Yes, and bypass permissions"), ("n", "No, keep planning")],
                  choose, "n", color=P["fg"])
        self.refresh_bars()

    # -- 입력
    def on_composer_submitted(self, msg: Composer.Submitted) -> None:
        comp = self.query_one(Composer)
        text = msg.text.strip()
        if self.reply_cb is not None:
            cb, self.reply_cb = self.reply_cb, None
            comp.clear()
            cb(text)
            return
        if not text:
            return
        comp.clear()
        self._remember(text)
        self.hide_popup()
        if self.running and not self.query_one(ChoiceCard).display and not text.startswith(("/", "!")) \
                and self.reply_cb is None:
            self.s.steer_box.append(text)          # Hermes 02 §7: 다음 단계 앞에 사용자 말로 끼워 넣는다
            self.add_cell(Text.assemble(("› ", P["muted"]), (text, P["fg"]), ("   sent to CAP mid-turn", P["muted"])),
                          UserCell)
            return
        if self.running or self.query_one(ChoiceCard).display:
            self.queue.append(text)
            self.add_cell(Text(f"⏳ Queued  {text}", style=P["muted"]))
            return
        self.submit(text)

    def submit(self, text: str, echo: bool = True) -> None:
        """echo=False: /init·/review·/commit·계획 실행처럼 CAP 이 만든 지시문은 사용자 말처럼 찍지 않는다."""
        if text.startswith("/"):
            cmd = text.split(" ", 1)[0]
            if cmd in ("/quit", "/exit", "/q"):
                self.exit()
                return
            if cmd == "/goal" and text[5:].strip():
                objective = text[5:].strip()
                self.add_user(text)
                self.start_job(lambda: self._goal_job(objective))
                return
            self.add_cell(Text("› " + text, style=P["muted"]))
            self.start_job(lambda: self._command_job(text))
            return
        if text.startswith("# ") and text[2:].strip():
            from .chat import quick_memory
            self.add_user(text)
            self.add_cell(Text(quick_memory(self.ws, text[2:]), style=P["success"]))
            return
        if text.startswith("!") and text[1:].strip():
            cmd = text[1:].strip()
            self.add_user(text)
            self.busy_label = "실행 중: " + cmd
            self.start_job(lambda: self._shell_job(cmd))
            return
        if echo:
            self.add_user(text)
        self.start_job(lambda: self._turn_job(text))

    def _shell_job(self, cmd: str) -> None:
        self.busy_label = "실행 중: " + cmd
        self.post(self.start_live, "실행 중: " + cmd)
        code, out = self.s.shell(cmd)
        lines = out.rstrip().splitlines()
        head = Text.assemble(("● ", P["success"] if code == 0 else P["error"]), ("Bash", f"bold {P['fg']}"),
                             ("  " + cmd, P["fg"]), (f"  → exit {code}", P["muted"]))
        tail = Text("\n".join(lines[-12:]) or "(no output)", style=P["muted"])
        body = [tail] + ([Text(EXPAND_HINT, style=P["muted"])] if len(lines) > 12 else [])
        full = Group(head, under(Text("\n".join(lines[-400:]), style=P["muted"]))) if len(lines) > 12 else None
        self.post(self.add_tool, {"kind": "tool", "collapsed": Group(head, under(*body)), "expanded": full,
                                  "ok": code == 0})
        self.post(self.add_cell, Text("CAP will see this output with your next message.", style=P["muted"]))

    def reset_view(self, resumed: bool) -> None:
        self.log_view.remove_children()
        self.welcome = self.live = None
        if resumed:
            self.replay_history()
        else:
            self.add_welcome()

    # -- 입력창 목록: '/' 명령, '@' 파일
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        if self.reply_cb is not None:
            self.hide_popup()
            return
        if text.startswith("/") and " " not in text and "\n" not in text:
            self.show_popup(text)
            return
        m = re.search(r"(?:^|\s)@([\w.\-/\\]*)$", text)
        if m:
            self.show_files(m.group(1))
        else:
            self.hide_popup()

    def _popup_rows(self, rows: list[tuple[str, RenderableType, str]]) -> None:
        pop = self.query_one("#popup", OptionList)
        pop.clear_options()
        if not rows:
            pop.display = False
            return
        for oid, left, desc in rows:
            row = Table.grid(padding=(0, 2))
            row.add_column(width=34, no_wrap=True)
            row.add_column()
            row.add_row(left, Text(desc, style=P["muted"]))
            pop.add_option(Option(row, id=oid))
        pop.highlighted = 0
        pop.display = True

    def show_popup(self, prefix: str) -> None:
        self.popup_kind = "slash"
        from .chat import all_commands
        self._popup_rows([(cmd, Text.assemble((cmd, f"bold {P['fg']}"), (f" {arg}" if arg else "", P["muted"])), desc)
                          for cmd, arg, desc in all_commands(self.ws) if cmd.startswith(prefix)])

    def workspace_files(self) -> list[str]:
        if self._files is None:
            files = []
            for p in iter_files(self.ws):
                r = p.relative_to(self.ws).as_posix()
                if not is_secret_path(r):
                    files.append(r)
                if len(files) >= 5000:
                    break
            self._files = sorted(files)
        return self._files

    def show_files(self, part: str) -> None:
        self.popup_kind = "file"
        q = part.replace("\\", "/").lower()
        files = self.workspace_files()
        starts = [f for f in files if f.lower().startswith(q) or f.lower().rsplit("/", 1)[-1].startswith(q)]
        rest = [f for f in files if q and q in f.lower() and f not in starts]
        hits = (starts + rest)[:12]
        self._popup_rows([("@" + f, Text("@" + f, style=P["fg"]), "") for f in hits])

    def hide_popup(self) -> None:
        self.query_one("#popup", OptionList).display = False

    def popup_move(self, step: int) -> bool:
        pop = self.query_one("#popup", OptionList)
        if not pop.display or not pop.option_count:
            return False
        pop.highlighted = ((pop.highlighted or 0) + step) % pop.option_count
        return True

    def _insert_choice(self, oid: str) -> None:
        comp = self.query_one(Composer)
        if self.popup_kind == "file":
            text = re.sub(r"@[\w.\-/\\]*$", "", comp.text)
            comp.load_text(text + oid + " ")
        else:
            comp.load_text(oid + " ")
        comp.move_cursor(comp.document.end)

    def popup_complete(self, submit: bool) -> bool:
        pop = self.query_one("#popup", OptionList)
        if not pop.display or pop.highlighted is None or not pop.option_count:
            return False
        oid = pop.get_option_at_index(pop.highlighted).id or ""
        self.hide_popup()
        if self.popup_kind == "slash":
            from .chat import all_commands
            arg = next((a for c, a, _ in all_commands(self.ws) if c == oid), "")
            if submit and (not arg or arg.startswith("[")):     # 인자가 없거나 선택이면 Enter 로 바로 실행
                self.query_one(Composer).post_message(Composer.Submitted(oid))
                return True
        self._insert_choice(oid)
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._insert_choice(event.option.id or "")
        self.hide_popup()
        self.query_one(Composer).focus()

    # -- 키
    def action_ctrl_c(self) -> None:
        if self.running:
            self.cancel_job()
            return
        card = self.query_one(ChoiceCard)
        if card.display and card.on_choose:
            card.choose(card.esc_key)
            return
        comp = self.query_one(Composer)
        if comp.text:
            comp.clear()
            return
        now = time.monotonic()
        if now - self.last_ctrl_c < 2.0:
            self.exit()
            return
        self.last_ctrl_c = now
        self.notify("Press Ctrl+C again to exit", timeout=2)

    def handle_escape(self) -> None:
        pop = self.query_one("#popup", OptionList)
        if pop.display:
            self.hide_popup()
        elif self.reply_cb is not None:
            cb, self.reply_cb = self.reply_cb, None
            self.query_one(Composer).clear()
            cb("")
        elif self.running:
            self.cancel_job()
        elif not self.query_one(Composer).text.strip():   # 쉬는 중 빈 입력창에서 Esc 두 번: 되돌릴 턴 고르기
            now = time.monotonic()
            if now - self.last_escape < 0.8:
                self.last_escape = 0.0
                self.offer_rewind()
            else:
                self.last_escape = now

    def offer_rewind(self) -> None:
        log = [t for t in (self.s.values().get("turn_log") or []) if t.get("sha")][-9:]
        if not log:
            self.notify("Nothing to rewind yet", timeout=2)
            return
        card = self.query_one(ChoiceCard)
        opts = [(str(t["n"]), f"Before turn {t['n']}: {shown_goal(t.get('goal', ''))[:60]}") for t in reversed(log)]

        def choose(k: str) -> None:
            card.close()
            self.refresh_bars()
            if k.isdigit():
                self.start_job(lambda: self._command_job(f"/rewind {k}"))
        card.open("Rewind files and conversation to…", None, opts + [("c", "Cancel")], choose, "c")
        self.refresh_bars()

    def action_app_escape(self) -> None:
        self.handle_escape()

    def action_cycle_mode(self) -> None:
        m = next_mode(self.s.approval["mode"])
        set_mode(self.s, m)                             # bypass 경고는 하단 줄(빨강)로만 — 대화 기록에 남기지 않는다
        self.mode_flash = time.monotonic() + 1.5       # 잠깐 강조하고 모드 설명을 보여 준다
        self.refresh_bars()

    def action_paste_image(self) -> None:
        """Alt+V: 클립보드의 그림을 저장하고 입력창에 @경로 로 붙인다(Claude Code 의 그림 붙여넣기)."""
        from .extras import clipboard_image

        def work() -> None:
            p = clipboard_image(self.ws)

            def apply() -> None:
                if p is None:
                    self.notify("No image in the clipboard (copy one, then Alt+V)", timeout=3)
                    return
                comp = self.query_one(Composer)
                comp.insert(f"@{p.relative_to(self.ws).as_posix()} ")
                comp.focus()
            with contextlib.suppress(Exception):
                self.call_from_thread(apply)
        threading.Thread(target=work, daemon=True, name="cap-paste").start()

    def action_clear_screen(self) -> None:
        self.log_view.remove_children()
        self.welcome = self.live = None

    def action_toggle_expand(self) -> None:
        cells = [c for c in self.log_view.children if isinstance(c, ToolCell) and c._expanded is not None]
        if cells:
            target = not any(c.expanded for c in cells)
            for c in cells:
                c.toggle(target)

    def action_page(self, step: int) -> None:
        log = self.log_view
        (log.scroll_page_down if step > 0 else log.scroll_page_up)(animate=False)

    # -- 입력 기록(작업 폴더 .cap/input_history.jsonl)
    def _hist_path(self) -> Path:
        return cap_dir(self.ws) / "input_history.jsonl"

    def _load_history(self) -> list[str]:
        try:
            return [json.loads(ln) for ln in self._hist_path().read_text(encoding="utf-8").splitlines() if ln][-HISTORY_MAX:]
        except (OSError, json.JSONDecodeError):
            return []

    def _remember(self, text: str) -> None:
        if self.history and self.history[-1] == text:
            return
        self.history.append(text)
        del self.history[:-HISTORY_MAX]
        with contextlib.suppress(OSError):
            with self._hist_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(text, ensure_ascii=False) + "\n")

    def on_unmount(self) -> None:
        if self._llm_orig is not None:
            llm.chat = self._llm_orig
        self.s.close()


def _short_path(p: Path) -> str:
    home, s = str(Path.home()), str(p)
    return "~" + s[len(home):] if s.startswith(home) else s


def _close_llm_client() -> None:
    c, llm._client = llm._client, None
    if c is not None:
        with contextlib.suppress(Exception):
            c.close()


@contextlib.contextmanager
def _utf8_console():
    """Windows 콘솔 코드 페이지를 UTF-8(65001)로 — 한글이 깨지지 않게. 끝나면 되돌린다."""
    if os.name != "nt":
        yield
        return
    k = ctypes.windll.kernel32
    old_out, old_in = k.GetConsoleOutputCP(), k.GetConsoleCP()
    k.SetConsoleOutputCP(65001)
    k.SetConsoleCP(65001)
    try:
        yield
    finally:
        k.SetConsoleOutputCP(old_out)
        k.SetConsoleCP(old_in)


def make_app(ws: Path, approval: str = "ask", resume: str | None = None) -> CapApp:
    ui = TuiUI(ws)
    sid = None
    if resume is not None:
        sid = _pick_session(ws, "" if resume == "last" else resume)
    s = ChatSession(ws, sid, approval, ui)
    return CapApp(s, ui, resumed=sid is not None)


def run_tui(ws: Path, approval: str = "ask", resume: str | None = None) -> int:
    app = make_app(ws, approval, resume)
    with _utf8_console():
        app.run()
    if llm.STATS["calls"]:
        print(f"[{llm.STATS['calls']} model calls, {llm.STATS['seconds']:.0f}s, "
              f"tokens in {llm.STATS['prompt_tokens']} / out {llm.STATS['completion_tokens']}]  session {app.s.sid} "
              f"(resume: cap --resume)")
    return 0
