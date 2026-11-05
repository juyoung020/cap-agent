"""`cap` 대화형 = 일반 코딩 에이전트 (Hermes Agent CLI / Codex CLI 처럼).

`cap` → 배너 → `>` 에 할 일을 쓰면 현재 폴더에서 바로 코딩 루프(LangGraph, loop.build_loop mode="chat")를 돈다.
- 같은 세션의 다음 입력은 같은 thread 에 이어 붙는다(SQLite 체크포인터, `cap --resume`/`/resume` 로 재개).
- 도구 호출마다 한 줄 진행 표시, 위험 행동은 그 자리에서 [y]/[a]/[n] 으로 묻고 Command(resume=…) 로 계속.
- 작업 폴더의 AGENTS.md / CLAUDE.md / .cap/instructions.md 를 시스템 프롬프트에 넣는다.
- Ctrl+C 는 지금 턴만 멈춘다(상태는 체크포인트에 남고, 다음 입력에서 '사용자가 중단함' 표식을 붙여 이어 감).
- `cap "할 일"` / `cap exec "할 일"` 은 같은 것을 한 번만 돌리고 끝낸다.
모드 5개(대화·승인·플랜·오토·바이패스, policy.MODE_TABLE)는 `--mode` 로 고르고, TUI 에서는 Shift+Tab(Alt+M)으로 바꾼다.
스펙·그래프 흐름은 선택 기능(`/spec`, `/graph`, `/run`, 또는 기존 하위 명령)으로만 남는다.
실제 터미널에서는 전체 화면 대화창(tui.py)이 이 모듈의 ChatSession·handle_command 를 그대로 쓴다.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from langgraph.types import Command

from . import compact as cmp
from . import llm
from .evalcheck import collect_test_files
from .loop import build_loop, fail_signature
from .policy import APPROVAL_HELP, BYPASS_WARNING, MODE_TABLE, classify_command, is_secret_path, normalize_mode
from .store import Ledger, Shadow, cap_dir, file_hash, iter_files, now_id, rel
from .tools import run_shell, spill

# Codex(AGENTS.md, AGENTS.override.md)·Claude Code(CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md)·CAP 의 지침 파일
INSTRUCTION_FILES = ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md",
                     ".cap/instructions.md")
IMPORT_RX = re.compile(r"(?<![\w`@])@((?:~/|\.{1,2}/|/)?[\w.\-/]+\.[A-Za-z0-9]+)")
INSTRUCTIONS_MAX = 4000      # Codex 12 §7 표: AGENTS.md 상한 4 KiB(9B·16k)
RECURSION = 500              # 사용자 턴 하나에 도구 라운드(노드 5개) × 최대 30턴 + 검증
SUBAGENT_CONCURRENCY = 2     # 하위 에이전트 동시 실행 수(수업 서버 한 대 — Codex 09 §14-3)
GOAL_MAX_TURNS = 8           # /goal 턴 예산(Hermes 기본 20, 9B·느린 서버라 줄임)
GOAL_SAME_LIMIT = 3          # /goal: 파일 변경 없이 같은 결과로 끝난 턴이 연속 3번이면 멈춤
GOAL_JUDGE = ("You judge whether a coding agent has reached the user's goal. done = the reply shows the goal is "
              "achieved with concrete evidence (changed code, test results); explaining why it cannot be done is NOT "
              "done. blocked = impossible, out of scope, or the next step needs the user. continue = there is a next "
              "step the agent can do now (choose this when unsure). Reply as JSON.")
GOAL_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["done", "continue", "blocked"]},
                                                "reason": {"type": "string"}},
               "required": ["verdict", "reason"], "additionalProperties": False}

# 사용자 요청 뒤에 붙는 덧붙임(@파일 내용, !명령 결과) 구분자 — 화면에서는 이 앞까지만 보인다
CONTEXT_MARK = "\n\n[context from the user]\n"
MENTION_RX = re.compile(r"(?<![\w@])@([\w.\-/\\]*[\w/])")
MENTION_LINES, MENTION_CHARS, MENTION_TOTAL = 200, 6000, 12000
SHELL_NOTE_MAX = 3000
REVIEW_DIFF_MAX = 8000


# ---------------------------------------------------------------- 화면

class UI:
    def __init__(self, out=None):
        self.out = out or _print
        self.tty = sys.stdout.isatty()
        self.color = self.tty and not os.environ.get("NO_COLOR")
        if self.color and os.name == "nt":
            os.system("")          # Windows 콘솔 ANSI 켜기
        self.ws: Path | None = None

    def c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def dim(self, s): return self.c("2", s)
    def green(self, s): return self.c("32", s)
    def red(self, s): return self.c("31", s)
    def yellow(self, s): return self.c("33", s)
    def cyan(self, s): return self.c("36", s)
    def bold(self, s): return self.c("1", s)

    @contextlib.contextmanager
    def busy(self, label: str):
        """TTY 일 때만 한 줄 스피너(9B 는 응답에 수 초~수십 초 걸린다). 파이프 출력에는 아무것도 안 찍는다."""
        if not self.tty:
            yield
            return
        label = label if len(label) <= 60 else label[:59] + "…"
        stop = threading.Event()
        t0 = time.perf_counter()

        def spin():
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            k = 0
            while not stop.wait(0.12):
                sys.stdout.write(f"\r  {self.dim(frames[k % len(frames)] + ' ' + label + f' {time.perf_counter() - t0:.0f}s')}\033[K")
                sys.stdout.flush()
                k += 1
        th = threading.Thread(target=spin, daemon=True)
        th.start()
        try:
            yield
        finally:
            stop.set()
            th.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    # -- 루프 이벤트
    def event(self, kind: str, **d) -> None:
        if kind == "tool":
            for ln in self.tool_lines(d["name"], d["args"], d["res"], d.get("before")):
                self.out(ln)
        elif kind == "deny":
            self.out(self.red(f"  ✗ Blocked {short_call(d['tool'], d['args'], self.ws)} — {d['reason']}"))
        elif kind == "subagent_start":
            self.out(self.dim(f"  ↳ {d['label']} started: {d['task'][:90]}"))
        elif kind == "subagent_done":
            self.out(self.cyan(f"  ↳ {d['label']} {d['status']}") + self.dim(f" · {d['calls']} calls · {d['seconds']:.0f}s"))
            for ln in (d.get("summary") or "").splitlines()[:6]:
                self.out(self.dim("      " + ln[:140]))
        elif kind == "verify":
            mark = self.green("  ✓ Verified ") if d["ok"] else self.red("  ✗ Tests failed ")
            self.out(mark + d["text"])
        elif kind == "note":
            t = " ".join(d["text"].split())
            self.out(self.dim("  ◦ " + (t[:200] + "…" if len(t) > 200 else t)))
        elif kind == "compact":
            self.out(self.dim(f"  ◦ Compacted context: ~{d['before']:,} → {d['after']:,} tokens"))

    def tool_lines(self, name: str, args: dict, res: dict, before: str | None) -> list[str]:
        ok = res.get("ok", True)
        out = res.get("output", "") or ""
        bullet = self.cyan("  • ") if ok else self.red("  ✗ ")
        lines: list[str] = []
        if name == "read_file":
            m = re.match(r"\[(.+?) 줄 (\d+)-(\d+) / 전체 (\d+)\]", out)
            if out.startswith("(폴더)"):
                head = f"list {args.get('path')}"
            elif m:
                head = f"read {disp_path(m.group(1), self.ws)} ({m.group(2)}-{m.group(3)} / {m.group(4)} lines)"
            else:
                head = f"read {args.get('path')} — {out[:80]}"
            lines.append(bullet + head)
        elif name == "search":
            n = 0 if out.startswith("(일치 없음)") else len(out.splitlines())
            lines.append(bullet + f'search "{args.get("pattern")}" → {n} hits')
        elif name in ("edit_file", "write_file"):
            path = disp_path(args.get("path", ""), self.ws)
            verb = "edit" if name == "edit_file" else "write"
            if not ok:
                lines.append(bullet + f"{verb} {path} — {out.splitlines()[0][:110] if out else 'failed'}")
            elif res.get("stage") == "already_applied" or not res.get("changed"):
                lines.append(bullet + f"{verb} {path} (no change)")
            else:
                after = cmp.read_before(self.ws or Path("."), name, args) or ""
                plus, minus, dl = cmp.diff_lines(before, after)
                tag = f"(new, {len(after.splitlines())} lines)" if before is None else f"+{plus} -{minus}"
                lines.append(bullet + f"{verb} {path} {tag}")
                if before is not None:
                    for ln in dl[:8]:
                        lines.append("      " + (self.green(ln) if ln.startswith("+") else self.red(ln)))
                    if len(dl) > 8:
                        lines.append(self.dim(f"      … +{len(dl) - 8} lines"))
            if res.get("syntax_error"):
                lines.append(self.red("      ⚠ syntax error after edit — sent back to the model"))
        elif name == "run" and (args.get("background") or res.get("background")):
            cmd = " ".join(args.get("cmd", "").split())
            lines.append(bullet + f"run {cmd[:90]} → " + (f"still running in background as {res['background']}"
                                                          if res.get("background") else "started in background"))
        elif name == "run":
            cmd = " ".join(args.get("cmd", "").split())
            code = res.get("exit")
            tail = ""
            if "pytest" in cmd:
                tail = " · " + cmp.test_summary(out, code if code is not None else 1)
            lines.append(bullet + f"run {cmd[:90]} → exit {code}{tail}")
            if not ok and "pytest" not in cmd:
                body = [ln for ln in out.splitlines()[1:] if ln.strip()][-2:]
                lines += [self.dim("      " + ln[:120]) for ln in body]
        elif name == "run_tests":
            code = res.get("exit")
            summ = cmp.test_summary(out, code if code is not None else 1)
            lines.append(bullet + f"test {res.get('cmd', '')} → " + (self.green(summ) if ok else self.yellow(summ)))
        elif name == "skill_search":
            lines.append(bullet + f'skill search "{args.get("query", "")[:60]}" → {out.splitlines()[0][:60] if out else ""}')
        elif name == "plugin_search":
            lines.append(bullet + f'plugin search "{args.get("query", "")[:60]}" → {out.splitlines()[0][:60] if out else ""}')
        elif name == "plugin_install":
            lines.append(bullet + f"plugin install {args.get('id', '')[:80]}" + ("" if ok else " — failed"))
            lines.append(self.dim("      " + out.splitlines()[0][:140] if out else ""))
        elif name == "skill_install":
            lines.append(bullet + f"skill install {args.get('id', '')[:80]}" + ("" if ok else " — failed"))
            lines.append(self.dim("      " + out[:140]))
        elif name == "symbols":
            what = f'find "{args.get("name", "")}"' if args.get("action") == "find" else f"map {args.get('path', '') or '.'}"
            n = len(out.splitlines())
            lines.append(bullet + f"symbols {what} → {n} lines")
        elif name == "glob":
            n = 0 if out.startswith("(no files") else len([ln for ln in out.splitlines() if not ln.startswith("…")])
            lines.append(bullet + f'glob "{args.get("pattern", "")}" → {n} files')
        elif name == "ask_user":
            lines.append(bullet + f"asked: {args.get('question', '')[:90]}")
            lines.append(self.dim("      " + out[:120]))
        elif name == "web_fetch":
            lines.append(bullet + f"fetch {args.get('url', '')[:90]}" + ("" if ok else " — failed"))
        elif name == "web_search":
            n = len([ln for ln in out.splitlines() if ln.startswith("- ")]) if ok else 0
            lines.append(bullet + f'websearch "{args.get("query", "")[:70]}" → {n} results')
        elif name == "apply_patch":
            lines.append(bullet + "patch " + (", ".join(res.get("changed") or []) or "(not applied)")
                         + ("" if ok else " — " + out[:100]))
        elif name == "process":
            lines.append(bullet + f"process {args.get('action', 'list')} {args.get('id', '')}".rstrip()
                         + (f" ← {str(args.get('text', ''))[:60]!r}" if args.get("action") == "write" else ""))
            lines += [self.dim("      " + ln[:120]) for ln in out.splitlines()[-3:]]
        elif name == "todo":
            lines.append(bullet + "todo")
            lines += [self.dim("      " + ln) for ln in out.splitlines()[:8]]
        else:
            lines.append(bullet + f"{name} {json.dumps(args, ensure_ascii=False)[:80]}")
        return lines


def _print(s: str = "") -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("utf-8", "replace").decode(sys.stdout.encoding or "utf-8", "replace"), flush=True)


def disp_path(path: str, ws: Path | None = None) -> str:
    """화면용 경로: 작업 폴더 안의 절대 경로는 상대 경로로."""
    if not ws or not path:
        return path
    try:
        p = Path(path)
        return p.resolve().relative_to(ws.resolve()).as_posix() if p.is_absolute() else path
    except (ValueError, OSError):
        return path


def short_call(tool: str, args: dict, ws: Path | None = None) -> str:
    if tool in ("edit_file", "write_file", "read_file"):
        return f"{tool.split('_')[0]} {disp_path(args.get('path', ''), ws)}"
    if tool == "run":
        return f"run {args.get('cmd', '')[:100]}" + (" (background)" if args.get("background") else "")
    if tool == "web_fetch":
        return f"fetch {args.get('url', '')[:100]}"
    if tool == "web_search":
        return f'websearch "{args.get("query", "")[:80]}"'
    return f"{tool} {json.dumps(args, ensure_ascii=False)[:80]}"


# ---------------------------------------------------------------- 작업 폴더 정보

def ensure_cap(ws: Path) -> None:
    d = cap_dir(ws)
    (d / "runs").mkdir(parents=True, exist_ok=True)
    (d / "sessions").mkdir(exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    Shadow(ws).ensure()


def detect_test_cmd(ws: Path) -> str:
    """Hermes 07 §2-3 detect_project_facts 의 축소판: pytest(테스트 파일·pytest.ini·[tool.pytest]) → npm test."""
    from .trust import project_setting
    chosen = project_setting(ws, "test_cmd")              # /testcmd 로 사용자가 정한 것이 먼저
    if chosen:
        return chosen
    pyproject = ws / "pyproject.toml"
    if collect_test_files(ws) or (ws / "pytest.ini").is_file() or \
            (pyproject.is_file() and "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore")):
        return "python -m pytest -q"
    pkg = ws / "package.json"
    if pkg.is_file():
        try:
            if "test" in (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}):
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass
    if (ws / "Cargo.toml").is_file():
        return "cargo test"
    if (ws / "go.mod").is_file():
        return "go test ./..."
    mk = ws / "Makefile"
    if mk.is_file() and re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="ignore"), re.M):
        return "make test"
    return ""


def git_facts(ws: Path) -> str:
    """작업 폴더가 git 저장소면 브랜치·변경 수·최근 커밋 한 줄(Hermes 07 §2-3)."""
    import subprocess
    def g(*a):
        try:
            r = subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    if g("rev-parse", "--is-inside-work-tree") != "true":
        return ""
    dirty = len([x for x in g("status", "--porcelain").splitlines() if x.strip()])
    return f"branch {g('rev-parse', '--abbrev-ref', 'HEAD') or '?'}, {dirty} changed file(s), last commit: {g('log', '-1', '--format=%h %s')[:80]}"


def expand_imports(text: str, base: Path, ws: Path, depth: int = 0, seen: set | None = None) -> str:
    """Claude Code 의 CLAUDE.md `@경로` 가져오기: 코드 블록 밖의 @파일을 그 내용으로(5단계까지).
    작업 폴더나 홈 안의 파일만, 비밀 파일은 건너뛴다."""
    seen = seen if seen is not None else set()
    if depth >= 5 or "@" not in text:
        return text
    out, fenced = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or "@" not in ln:
            out.append(ln)
            continue

        def sub(m: re.Match) -> str:
            raw = m.group(1)
            q = (Path.home() / raw[2:]) if raw.startswith("~/") else (Path(raw) if raw.startswith("/") else base / raw)
            try:
                q = q.resolve()
            except OSError:
                return m.group(0)
            ok_root = any(r == q or r in q.parents for r in (ws.resolve(), Path.home().resolve()))
            if not ok_root or not q.is_file() or q in seen or is_secret_path(q.name) or q.stat().st_size > 50_000:
                return m.group(0)
            seen.add(q)
            body = q.read_text(encoding="utf-8", errors="replace").strip()
            return f"\n<!-- imported from {raw} -->\n{expand_imports(body, q.parent, ws, depth + 1, seen)}\n"
        out.append(IMPORT_RX.sub(sub, ln))
    return "\n".join(out)


def _git_root(ws: Path) -> Path | None:
    for d in (ws.resolve(), *ws.resolve().parents):
        if (d / ".git").exists():
            return d
    return None


def load_instructions(ws: Path) -> tuple[list[str], str]:
    """지침 파일. Codex·Claude Code 처럼 작업 폴더가 git 저장소의 하위 폴더면 저장소 루트부터 작업 폴더까지 차례로
    (위가 먼저, 가까운 것이 나중 — 가까운 지침이 이긴다). 길면 앞 70%·뒤 20% 만 남긴다(Hermes 03 §1)."""
    names, parts = [], []
    root = _git_root(ws)
    chain = [ws.resolve()]
    if root is not None and root != ws.resolve():
        chain = [d for d in reversed(ws.resolve().parents) if d == root or root in d.parents] + chain
    from .compat import claude_home, codex_home, import_on
    user_files = ([("~/.codex/AGENTS.md", codex_home() / "AGENTS.md")] if import_on("codex") else []) + \
        ([("~/.claude/CLAUDE.md", claude_home() / "CLAUDE.md")] if import_on("claude") else [])
    for label, p in user_files:                  # 사용자 전역 지침(Codex·Claude Code) — 프로젝트 지침보다 먼저
        if p.is_file():
            t = expand_imports(p.read_text(encoding="utf-8", errors="replace").strip(), p.parent, ws)
            if t:
                names.append(label)
                parts.append(f"## {label}\n{t}")
    for d in chain:
        for n in INSTRUCTION_FILES:
            p = d / n
            if p.is_file():
                t = expand_imports(p.read_text(encoding="utf-8", errors="replace").strip(), p.parent, ws)
                if t:
                    rel_d = d.relative_to(root).as_posix() if d != ws.resolve() else "."
                    label = n if rel_d == "." else f"{rel_d}/{n}"
                    names.append(label)
                    parts.append(f"## {label}\n{t}")
    text = "\n\n".join(parts)
    if len(text) > INSTRUCTIONS_MAX:
        head, tail = int(INSTRUCTIONS_MAX * 0.7), int(INSTRUCTIONS_MAX * 0.2)
        text = text[:head] + f"\n…[지침 {len(text) - head - tail}자 생략]…\n" + text[-tail:]
    return names, text


def workspace_block(ws: Path, test_cmd: str, inst_names: list[str], inst_text: str, limit: int = 40) -> str:
    """Hermes 07 §2-3 작업공간 스냅샷의 축소판 + 지침 파일(Codex 11 §4.3 <INSTRUCTIONS> 형식)."""
    files = sorted(rel(ws, f) for f in iter_files(ws))
    more = f"\n  … (+{len(files) - limit})" if len(files) > limit else ""
    lines = ["Workspace (snapshot at session start; re-read files before relying on it):",
             f"- Root: {ws}",
             f"- Verify: {test_cmd or '(no tests found)'}",
             *([f"- Git: {gf}"] if (gf := git_facts(ws)) else []),
             f"- Context files: {', '.join(inst_names) or '(none)'}",
             "- Files:"] + [f"  {f}" for f in files[:limit]]
    block = "\n".join(lines) + more
    if inst_text:
        block += ("\n\nProject instructions (follow them unless the user says otherwise):\n<INSTRUCTIONS>\n"
                  + inst_text + "\n</INSTRUCTIONS>")
    return block


# ---------------------------------------------------------------- 세션

def sessions_dir(ws: Path) -> Path:
    return cap_dir(ws) / "sessions"


def list_sessions(ws: Path) -> list[dict]:
    out = []
    for p in sessions_dir(ws).glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(out, key=lambda m: m.get("updated") or m.get("started") or "")


class ChatSession:
    def __init__(self, ws: Path, sid: str | None = None, approval: str = "ask", ui: UI | None = None):
        ensure_cap(ws)
        self.ws = ws
        self.ui = ui or UI()
        self.ui.ws = ws
        self.new = sid is None
        self.sid = sid or now_id()
        if sid is None:            # 같은 초에 연 새 세션이 앞 세션의 대화를 이어받지 않게
            base, k = self.sid, 2
            while (sessions_dir(ws) / f"{self.sid}.json").exists():
                self.sid, k = f"{base}-{k}", k + 1
        self.meta_path = sessions_dir(ws) / f"{self.sid}.json"
        meta = {}
        if self.meta_path.is_file():
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.approval = {"mode": normalize_mode(approval)}
        self.test_cmd = detect_test_cmd(ws)
        self.inst_names, inst_text = load_instructions(ws)
        self.facts = self._facts(self.test_cmd, self.inst_names, inst_text)
        if not meta:
            meta = {"sid": self.sid, "started": _dt.datetime.now().isoformat(timespec="seconds"), "title": "",
                    "turns": 0, "start_sha": Shadow(ws).snapshot("session start")}
        self.meta = meta
        self._save_meta()
        from .runner import checkpointer
        self.shell_notes: list[str] = []      # 사용자가 !명령으로 직접 돌린 결과 — 다음 요청에 붙인다
        self.autocommit = False               # /autocommit on: 검증된 턴마다 CAP 이 바꾼 파일을 커밋
        from .styles import default_name
        self.output_style = default_name()      # /output-style: 매 요청에 덧붙는 답하는 방식
        self.steer_box: list[str] = []        # 작업 중에 사용자가 보낸 말 — 다음 모델 호출 앞에 끼워 넣는다
        self.cancel_event = threading.Event()  # Esc/Ctrl+C: 하위 에이전트도 다음 단계에서 멈춘다
        self._ask_lock = threading.Lock()      # 하위 에이전트 여럿이 동시에 승인을 묻지 않게
        self.current_ask = self.ask_approval
        from .hooks import Hooks
        self.hooks = Hooks(ws)
        from .extras import load_rules
        self.rules = load_rules(ws)
        from .mcp_client import MCPManager
        self.mcp = MCPManager(ws)
        self.hook_context = ""
        if self.hooks.any("SessionStart"):
            hr = self.hooks.run("SessionStart", {"source": "resume" if sid else "startup", "session_id": self.sid})
            self.hook_context = hr["context"] or ""
        self.system_extra = self._system_extra(inst_text)
        self.saver, self.conn = checkpointer(ws)
        self.ledger = Ledger(ws, f"chat-{self.sid}")
        self.app = self._build()
        self.cfg = {"configurable": {"thread_id": f"chat-{self.sid}"}, "recursion_limit": RECURSION,
                    "max_concurrency": SUBAGENT_CONCURRENCY}
        self.turn_mode: str | None = None     # 다음 한 턴만 쓸 모드(/review 는 읽기 전용)

    @staticmethod
    def _facts(test_cmd: str, names: list[str], inst_text: str) -> tuple[str, str]:
        import hashlib
        return test_cmd, hashlib.sha1(("\n".join(names) + "\0" + inst_text).encode("utf-8", "replace")).hexdigest()

    def _system_extra(self, inst_text: str) -> str:
        from .memory import memory_block, skills_block
        extra = [workspace_block(self.ws, self.test_cmd, self.inst_names, inst_text), memory_block(self.ws),
                 skills_block(self.ws)]
        if self.hook_context:
            extra.append("Context from a SessionStart hook:\n" + self.hook_context)
        return "\n\n".join(x for x in extra if x)

    def _build(self, facts_note: str = ""):
        """코딩 루프 그래프. 상태는 SQLite 체크포인터에 있으므로 다시 만들어도 대화는 그대로 이어진다."""
        from .trust import project_approvals
        return build_loop(ws=self.ws, ledger=self.ledger, test_cmd=self.test_cmd, frozen=set(),
                          preapproved=set(project_approvals(self.ws)),
                          decisions=[], printer=self.ui.out, checkpointer=self.saver, mode="chat",
                          approval=self.approval, ui=self.ui, steer=self.take_steer,
                          ask_human=self._ask_human, should_stop=self.cancel_event.is_set,
                          hooks=self.hooks, mcp=self.mcp if self.mcp.tools else None, rules=self.rules,
                          system_extra=self.system_extra, facts_note=facts_note)

    def refresh_facts(self) -> str:
        """매 턴: 테스트 명령과 지침 파일을 다시 읽는다. 바뀌었으면(모델이 첫 테스트를 만들었거나 /init 이 AGENTS.md 를 썼다)
        시스템 글과 루프를 새로 만들고, 다음 요청 앞에 붙일 한 줄을 돌려준다(그대로면 '')."""
        test_cmd = detect_test_cmd(self.ws)
        names, inst_text = load_instructions(self.ws)
        facts = self._facts(test_cmd, names, inst_text)
        if facts == self.facts:
            return ""
        changes = []
        if facts[0] != self.facts[0]:
            changes.append(f"tests: {test_cmd or '(none)'}")
        if facts[1] != self.facts[1]:
            changes.append("instructions updated" + (f" ({', '.join(names)})" if names else " (none now)"))
        self.facts, self.test_cmd, self.inst_names = facts, test_cmd, names
        self.system_extra = self._system_extra(inst_text)
        note = "[Project facts changed] " + " / ".join(changes)
        self.app = self._build(note)
        self.ledger.log("facts_changed", test_cmd=test_cmd, instructions=names)
        return note

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        with contextlib.suppress(Exception):
            if self.hooks.any("SessionEnd"):
                self.hooks.run("SessionEnd", {"session_id": self.sid, "reason": "close"})
        with contextlib.suppress(Exception):
            self.conn.close()
        with contextlib.suppress(Exception):
            self.mcp.close()
        with contextlib.suppress(Exception):
            from .extras import bg_kill_all
            bg_kill_all(self.ws)

    def _save_meta(self) -> None:
        self.meta["updated"] = _dt.datetime.now().isoformat(timespec="seconds")
        self.meta_path.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def values(self) -> dict:
        return self.app.get_state(self.cfg).values or {}

    def pending(self) -> list:
        st = self.app.get_state(self.cfg)
        return [i.value for t in st.tasks for i in t.interrupts]

    def mcp_mentions(self, text: str) -> list[str]:
        """`@서버:uri` (예: @docs:docs://style-guide) — 켜 둔 MCP 서버의 리소스를 요청에 붙인다."""
        out = []
        if not getattr(self.mcp, "resources", None):
            return out
        for m in MCP_MENTION_RX.finditer(text):
            server, uri = m.group(1), m.group(2).rstrip(".,;)")
            if server not in self.mcp.resources:
                continue
            ok, body = self.mcp.read_resource(server, uri)
            body = spill(self.ws, body)
            out.append(f"--- @{server}:{uri} (MCP resource) ---\n" + (body if ok else f"(could not read: {body})"))
        return out

    def stopped_midway(self) -> str:
        """지난 턴이 끝나지 않고 멈췄으면(강제 종료·오류·Esc) 다음에 돌 노드 이름. 승인 대기는 여기 안 든다.
        LangGraph 체크포인터가 노드마다 상태를 저장하므로 그 자리부터 이어 갈 수 있다(/continue)."""
        try:
            st = self.app.get_state(self.cfg)
        except Exception:  # noqa: BLE001
            return ""
        if not st.next or self.pending():
            return ""
        return ", ".join(st.next)

    # -- 한 턴
    def run_turn(self, text: str | None, ask=None) -> dict:
        """text=None(또는 CONTINUE) 이면 멈춰 있던 승인 대기를, 없으면 중간에 멈춘 턴을 이어서."""
        if text == CONTINUE:
            text = None
        ask = self._with_notify(ask or self.ask_approval)
        self.current_ask = ask
        self.cancel_event.clear()
        t0, calls0 = time.perf_counter(), llm.STATS["calls"]
        self.refresh_facts()
        if text is not None:
            if not self.meta.get("title"):
                self.meta["title"] = text[:60]
            extra = attach_mentions(self.ws, text) + self.mcp_mentions(text) + self.shell_notes
            self.shell_notes = []
            if self.output_style:
                from .styles import available
                st = available(self.ws).get(self.output_style)
                if st:
                    extra.append(f"[Output style: {self.output_style}] {st[1]}")
            inp = {"goal": text + (CONTEXT_MARK + "\n\n".join(extra) if extra else "")}
        elif self.pending():
            inp = Command(resume=ask(self.pending()[0]))
        else:
            inp = None              # /continue: 마지막 체크포인트에서 멈춘 노드부터 다시(LangGraph 이어 하기)
            self.ledger.log("continue", next=self.stopped_midway())
        mode, self.turn_mode = self.turn_mode, None
        prev = self.approval["mode"]
        if mode:
            self.approval["mode"] = mode
        try:
            while True:
                self.app.invoke(inp, self.cfg)
                intr = self.pending()
                if not intr:
                    break
                inp = Command(resume=ask(intr[0]))
        finally:
            if mode:
                self.approval["mode"] = prev
        self.refresh_facts()                 # 이번 턴에 생긴 테스트·지침을 /goal 관문과 다음 턴이 보게
        v = self.values()
        self.meta["turns"] = v.get("turn_no", 0)
        self.meta["calls"] = self.meta.get("calls", 0) + llm.STATS["calls"] - calls0
        self._save_meta()
        if self.autocommit:
            note = self.auto_commit(v)
            if note:
                self.ui.out(note)
        return {"values": v, "seconds": time.perf_counter() - t0, "calls": llm.STATS["calls"] - calls0}

    def auto_commit(self, v: dict) -> str:
        """/autocommit on (Aider 방식): 턴이 끝나고 검증이 실패하지 않았으면, 이 턴에 CAP 이 바꾼 파일만 git 에 커밋."""
        import subprocess
        files = list(v.get("turn_changed") or [])
        note = v.get("verify_note") or ""
        if v.get("status") != "done" or not files or "failed" in note:
            return ""

        def git(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *a], cwd=self.ws, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return "· autocommit: not a git repository — skipped"
        log = v.get("turn_log") or []
        goal = shown_goal(log[-1].get("goal", "")) if log else ""
        subject = "cap: " + (" ".join(goal.split())[:60] or "changes")
        body = "Files: " + ", ".join(files) + (f"\n{note}" if note else "")
        git("add", "-A", "--", *files)
        r = git("commit", "-m", subject, "-m", body, "--", *files)
        if r.returncode != 0:
            return "· autocommit failed: " + (r.stderr or r.stdout).strip().splitlines()[-1][:160] if (r.stderr or r.stdout).strip() else "· autocommit failed"
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
        self.ledger.log("autocommit", sha=sha, files=files)
        return f"✓ committed {sha} {subject}"

    def ask_approval(self, payload: dict) -> str:
        ui = self.ui
        if payload.get("kind") == "escalate":       # 같은 실패가 반복됨: 힌트를 받거나 멈춘다
            ui.out(ui.red(f"  ⚠ CAP is stuck — {payload.get('reason', '')}"))
            for ln in (payload.get("output") or "").strip().splitlines()[-8:]:
                ui.out(ui.dim("      " + ln))
            try:
                ans = input("    Give a hint to continue (empty = stop) > ")
            except EOFError:
                ans = ""
            return ans.strip() or "abort"
        if payload.get("kind") == "question":       # 모델이 묻는 질문(ask_user)
            ui.out(ui.cyan(f"  ? {payload.get('question', '')}"))
            opts = payload.get("options") or []
            for i, o in enumerate(opts, 1):
                ui.out(f"    {i}. {o}")
            try:
                ans = input("    Answer (number or text, empty = let CAP decide) > ").strip()
            except EOFError:
                ans = ""
            return opts[int(ans) - 1] if ans.isdigit() and 0 < int(ans) <= len(opts) else ans
        who = f"[{payload['agent']}] " if payload.get("agent") else ""
        for c in payload.get("calls", []):
            ui.out(ui.yellow(f"  ? {who}Permission needed — {c['reason']}: ") + short_call(c["tool"], c["args"], self.ws))
            for ln in c.get("preview") or []:
                col = ui.green if ln.startswith("+") else ui.red if ln.startswith("-") else ui.dim
                ui.out("      " + col(ln))
        installs = all(c["tool"] in ("plugin_install", "skill_install") for c in payload.get("calls", []))
        proj = project_ok(payload.get("calls", []))
        prompt = ("    [y] yes / [n] no (or type what to do instead) > " if installs else      # 설치는 늘 묻는다
                  "    [y] yes / [a] yes, don't ask again this session / "
                  + ("[p] yes, and don't ask again in this project / " if proj else "")
                  + "[n] no (or type what to do instead) > ")
        try:
            ans = input(prompt)
        except EOFError:
            ans = "n"
            ui.out("n (no input → declined)")
        else:
            if not sys.stdin.isatty():
                ui.out(ans)
        return ans.strip() or "n"

    # -- 슬래시 명령용
    def last_answer(self, v: dict) -> str:
        log = v.get("turn_log") or []
        start = log[-1]["msg_idx"] if log and log[-1].get("msg_idx", -1) >= 0 else 0
        for m in reversed(v.get("messages", [])[start:]):
            if m.get("role") == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
                return m["content"].strip()
        return ""

    def last_plan(self, v: dict) -> list[str]:
        """이번 턴에서 모델이 마지막으로 낸 todo 목록(플랜 모드의 계획). 없으면 []."""
        log = v.get("turn_log") or []
        start = log[-1]["msg_idx"] if log and log[-1].get("msg_idx", -1) >= 0 else 0
        for m in reversed(v.get("messages", [])[start:]):
            for tc in reversed(m.get("tool_calls") or []):
                if tc.get("function", {}).get("name") == "todo":
                    try:
                        items = json.loads(tc["function"].get("arguments") or "{}").get("items") or []
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    return [str(i.get("text", "")) if isinstance(i, dict) else str(i) for i in items if i]
        return []

    def diff(self, stat: bool = False) -> str:
        sha = self.meta.get("start_sha")
        return Shadow(self.ws).diff(sha, stat=stat) if sha else ""

    def undo(self) -> str:
        v = self.values()
        log = list(v.get("turn_log") or [])
        if not log or not log[-1].get("sha"):
            return "Nothing to undo."
        last = log[-1]
        sh = Shadow(self.ws)
        # CAP 이 이 턴에 쓴 파일만, 그리고 CAP 이 쓴 그대로일 때만 되돌린다. 그 뒤 사람이 고친 파일·새로 만든 파일은 둔다
        # (Hermes 07 §9-4: 되돌릴 때 사람의 편집을 덮지 않는다)
        hashes = v.get("file_hashes") or {}
        mine, kept = [], []
        for f in v.get("turn_changed") or []:
            p = self.ws / f
            cur = file_hash(p) if p.is_file() else ""
            (mine if hashes.get(f, cur) == cur else kept).append(f)
        files = sh.restore_files(last["sha"], mine)
        msgs = list(v.get("messages") or [])
        upd: dict = {"turn_log": log[:-1], "status": "done", "pending": []}
        if last.get("msg_idx", -1) > 0:
            upd["messages"] = msgs[:last["msg_idx"]]
        else:   # 압축으로 그 턴의 메시지 위치를 잃었으면 다음 요청 앞에 알림을 붙인다
            upd["carry_note"] = f"(The user undid the file changes of the previous request: {shown_goal(last.get('goal', ''))[:120]})"
        self.app.update_state(self.cfg, upd)
        msg = f"↩ Undid turn {last['n']} \"{shown_goal(last.get('goal', ''))[:40]}\" — restored: {', '.join(files) or 'none'}"
        return msg + (f" · kept your later edits: {', '.join(kept)}" if kept else "")

    def _with_notify(self, ask):
        """사람의 답을 기다리기 직전에 Notification 훅(데스크톱 알림 등)."""
        if not self.hooks.any("Notification"):
            return ask

        def wrapped(payload: dict) -> str:
            kind = payload.get("kind", "approval")
            msg = {"question": "CAP has a question: " + str(payload.get("question", ""))[:200],
                   "escalate": "CAP is stuck and needs a hint"}.get(kind, "CAP needs your permission")
            with contextlib.suppress(Exception):
                self.hooks.run("Notification", {"message": msg, "kind": kind, "session_id": self.sid}, tool=kind)
            return ask(payload)
        return wrapped

    def _ask_human(self, payload: dict) -> str:
        with self._ask_lock:
            return self.current_ask(payload)

    def run_goal(self, objective: str, ask=None, on_turn=None, on_note=None, max_turns: int = GOAL_MAX_TURNS) -> str:
        """/goal: 목표가 이뤄질 때까지 턴을 이어 간다(Hermes 09 §2 '목표 루프').
        턴이 끝날 때마다 ① 품질 관문 — 하네스가 테스트 명령을 직접 돌려 실패하면 그 출력이 다음 지시가 되고,
        ② 통과하면 같은 모델 심판이 done/continue/blocked 를 정한다(JSON 강제, 3번 연속 못 읽으면 멈춤).
        반환: 끝난 이유 한 줄."""
        note = on_note or (lambda t: None)
        msg = (f"[Goal] {objective}\nWork toward this goal step by step. When it is fully done and verified, say so and "
               "stop. If you are blocked and need the user, say exactly why and stop.")
        bad_judge, gate_fail = 0, 0
        last_sig, same = None, 0

        def stalled(changed: list, sig: str) -> bool:
            """파일을 안 바꾸고 같은 결과로 끝난 턴이 3번 연속이면 진전 없음(남은 턴을 다 태우지 않는다)."""
            nonlocal last_sig, same
            key = (tuple(sorted(changed)), sig)
            same = same + 1 if (not changed and key == last_sig) else (1 if not changed else 0)
            last_sig = key
            return same >= GOAL_SAME_LIMIT

        for k in range(1, max_turns + 1):
            if self.cancel_event.is_set():
                return "goal stopped by the user"
            note(f"◎ Goal turn {k}/{max_turns}")
            r = self.run_turn(msg, ask=ask)
            if on_turn:
                on_turn(r)
            v = r["values"]
            if v.get("status") in ("escalated", "blocked"):
                return f"goal stopped: {v.get('status')} — {(v.get('verify_note') or v.get('escalate_reason') or '')[:200]}"
            if self.test_cmd:      # 관문도 모델의 명령과 같은 격리 안에서(bypass 만 격리 없음)
                from . import sandbox as sbx
                sb = "" if normalize_mode(self.approval["mode"]) == "bypass" else sbx.backend()
                code, out = run_shell(self.test_cmd, self.ws, timeout=300, sandbox=sb)
                if code not in (0, 5):
                    gate_fail += 1
                    note(f"◎ Gate: {self.test_cmd} failed (attempt {gate_fail})")
                    if stalled(v.get("turn_changed") or [], fail_signature(re.sub(r"\d+\.\d+s", "", out))):
                        return (f"goal blocked: no progress ({same} turns in a row changed no files and "
                                f"`{self.test_cmd}` failed the same way)")
                    msg = (f"[Continuing toward your goal] {objective}\nThe quality gate `{self.test_cmd}` must pass before the "
                           f"goal can be called done, and it just failed (exit {code}):\n{out[-3000:]}\nFix the root cause, "
                           "then run the tests to confirm. Do not claim the goal is done while the gate fails. If the gate "
                           "itself is wrong or cannot pass, say so clearly and stop.")
                    continue
            answer = self.last_answer(v)
            try:
                j = llm.chat_json([{"role": "system", "content": GOAL_JUDGE},
                                   {"role": "user", "content": f"Goal: {objective}\n\nAgent's latest reply:\n{answer[:3000]}\n\n"
                                                               f"Quality gate: {'passed' if self.test_cmd else 'none'}"}],
                                  GOAL_SCHEMA, name="verdict", max_tokens=200, ledger=self.ledger, tag="goal_judge",
                                  think=False)
                verdict, reason = j.get("verdict"), str(j.get("reason", ""))[:200]
                bad_judge = 0
            except (RuntimeError, ValueError, AttributeError):
                bad_judge += 1
                if bad_judge >= 3:
                    return "goal paused: the judge could not answer 3 times in a row"
                verdict, reason = "continue", "judge unavailable"
            self.ledger.log("goal", turn=k, verdict=verdict, reason=reason)
            note(f"◎ Judge: {verdict} — {reason}")
            if verdict == "done":
                return f"goal done in {k} turn(s): {reason}"
            if verdict == "blocked":
                return f"goal blocked: {reason}"
            if stalled(v.get("turn_changed") or [], "judge:continue"):
                return f"goal blocked: no progress ({same} turns in a row changed no files; judge: {reason})"
            msg = (f"[Continuing toward your goal] {objective}\nDo the next concrete step (judge: {reason}). If it is done, "
                   "say so and stop. If you are blocked and need the user, say so clearly and stop.")
        return f"goal not finished after {max_turns} turns"

    def learn(self) -> list[str]:
        """/learn: 이번 세션에서 다음 세션에도 쓸 사실을 모델이 뽑고(없으면 빈 목록), 프로젝트 기억에 더한다."""
        from . import memory as M
        msgs = list(self.values().get("messages") or [])
        if len(msgs) < 3:
            return ["Nothing to learn yet."]
        msgs = cmp.shrink_old(cmp.normalize(msgs))
        try:
            r = llm.chat(msgs + [{"role": "user", "content": M.LEARN_PROMPT}], max_tokens=400, temperature=0.1,
                         ledger=self.ledger, tag="learn", think=False)
            text = r["content"] or ""
            facts = json.loads(text[text.find("{"): text.rfind("}") + 1]).get("facts") or []
        except (RuntimeError, ValueError, AttributeError):
            return ["Could not extract facts this time."]
        out = []
        for f in [str(x).strip() for x in facts if str(x).strip()][:5]:
            ok, msg = M.change_memory(self.ws, "add", f, "project")
            out.append(("+ " if ok else "· ") + f + ("" if ok else f"  ({msg.splitlines()[0]})"))
        return out or ["Nothing worth remembering from this session."]

    def take_steer(self) -> list[str]:
        items, self.steer_box = self.steer_box, []
        return items

    def rewind(self, n: int, what: str = "both") -> str:
        """/rewind n [code|chat|both]: 턴 n 이 시작되기 전으로 — 파일만, 대화만, 또는 둘 다(Claude Code /rewind)."""
        v = self.values()
        log = list(v.get("turn_log") or [])
        idx = next((i for i, t in enumerate(log) if t.get("n") == n), None)
        if idx is None or not log[idx].get("sha"):
            return f"No turn {n} to go back to."
        t = log[idx]
        sh = Shadow(self.ws)
        # /undo 와 같은 원칙: CAP 이 쓴 파일만, CAP 이 마지막으로 쓴 그대로일 때만 되돌린다. 사람이 고친 것은 둔다
        cap_wrote = set(v.get("changed") or []) | set(v.get("turn_changed") or [])
        hashes = v.get("file_hashes") or {}
        mine, kept = [], []
        for f in sh.changed_files(t["sha"]):
            p = self.ws / f
            cur = file_hash(p) if p.is_file() else ""
            (mine if f in cap_wrote and hashes.get(f, cur) == cur else kept).append(f)
        goal = shown_goal(t.get("goal", ""))
        files = sh.restore_files(t["sha"], mine) if what in ("both", "code") else []
        msgs = list(v.get("messages") or [])
        upd: dict = {"status": "done", "pending": []}
        if what in ("both", "chat"):
            upd["turn_log"] = log[:idx]
            if t.get("msg_idx", -1) > 0:
                upd["messages"] = msgs[:t["msg_idx"]]
            else:
                upd["carry_note"] = f"(The user went back to before this request: {goal[:120]})"
        else:                                # 파일만: 대화는 두고, 파일이 되돌려졌다고 알린다
            upd["carry_note"] = (f"(The user restored the files to how they were before the request \"{goal[:80]}\": "
                                 f"{', '.join(files) or 'nothing changed'}. Re-read files before editing.)")
            upd["full_reads"], upd["file_hashes"] = [], {}
        self.app.update_state(self.cfg, upd)
        label = {"both": "files and conversation", "code": "files only", "chat": "conversation only"}[what]
        msg = f"⏪ Back to before turn {n} \"{goal[:40]}\" ({label})"
        if what != "chat":
            msg += f" — files restored: {', '.join(files) or 'none'}"
        return msg + (f" · kept files you changed yourself: {', '.join(kept)}" if kept and what != "chat" else "")

    FORK_KEYS = ("messages", "turn_log", "turn_no", "changed", "file_hashes", "full_reads", "preapproved_extra",
                 "carry_note", "todos", "status", "sub_instructions")

    def fork(self, title: str = "") -> "ChatSession":
        """/fork: 지금까지의 대화를 새 세션에 복사한다(파일은 그대로 — 같은 작업 폴더). 원래 세션은 /resume 으로."""
        v = self.values()
        new = ChatSession(self.ws, None, self.approval["mode"], self.ui)
        keep = {k: v[k] for k in self.FORK_KEYS if k in v}
        if keep:
            new.app.update_state(new.cfg, keep, as_node="context")
        new.meta["title"] = (title or f"fork of {self.meta.get('title') or self.sid}")[:80]
        new.meta["forked_from"] = self.sid
        new.meta["turns"] = v.get("turn_no", 0)
        new._save_meta()
        self.ledger.log("fork", to=new.sid)
        return new

    def context_lines(self) -> list[str]:
        """/context: 맥락 창을 무엇이 차지하는지(추정 토큰)."""
        v = self.values()
        msgs = list(v.get("messages") or [])
        parts = {"system prompt (instructions, memory, skills)": 0, "your messages": 0, "CAP's replies": 0,
                 "tool results": 0}
        for m in msgs:
            n = llm.est_messages([m], None)
            key = {"system": "system prompt (instructions, memory, skills)", "user": "your messages",
                   "assistant": "CAP's replies", "tool": "tool results"}.get(m.get("role"), "tool results")
            parts[key] += n
        total = sum(parts.values())
        win = llm.CONTEXT_WINDOW
        out = [f"Context: ~{total:,} / {win:,} tokens ({total * 100 // max(win, 1)}%)"]
        for k, n in parts.items():
            bar = "█" * min(30, n * 30 // max(win, 1))
            out.append(f"  {k:<46} ~{n:>6,}  {bar}")
        out.append(f"  {'free':<46} ~{max(win - total, 0):>6,}")
        out.append(f"Auto-compacts at ~{cmp.compact_at():,} tokens; /compact to do it now.")
        return out

    def compact(self, focus: str = "") -> str:
        """/compact: 지금까지 대화를 요약 한 덩어리로 바꿔 맥락을 비운다(자동 압축과 같은 방식, 지금 바로)."""
        v = self.values()
        msgs = list(v.get("messages") or [])
        if len(msgs) < 3:
            return "Nothing to compact yet."
        msgs = cmp.shrink_old(cmp.normalize(msgs))
        before = llm.est_messages(msgs, None)
        if self.hooks.any("PreCompact"):
            self.hooks.run("PreCompact", {"trigger": "manual", "session_id": self.sid})
        summary = cmp.summarize(msgs, ledger=self.ledger, focus=focus)
        note = v.get("carry_note") or ""
        log = [{**t, "msg_idx": -1} for t in (v.get("turn_log") or [])]
        self.app.update_state(self.cfg, {"messages": msgs[:1], "turn_log": log,
                                         "carry_note": summary + ("\n\n" + note if note else "")})
        self.ledger.log("compact", before=before, summary=summary[:1500], manual=True)
        after = llm.est_messages(msgs[:1], None) + llm.est_text(summary)
        return f"Compacted context: ~{before:,} → ~{after:,} tokens"

    def shell(self, cmd: str) -> tuple[int, str]:
        """!명령: 모델을 거치지 않고 사용자가 직접 돌린다. 하드 금지·비밀 파일만 막고, 결과는 다음 요청에 붙인다."""
        d = classify_command(cmd, self.ws)
        if d.verdict == "deny" and not d.reason.startswith("evaluator"):
            return 1, f"Blocked — {d.reason}"
        code, out = run_shell(cmd, self.ws, timeout=300)
        tail = out[-SHELL_NOTE_MAX:]
        self.shell_notes.append(f"[The user ran `{cmd}` themselves → exit {code}]\n{tail}".rstrip())
        self.ledger.log("user_shell", cmd=cmd, exit=code)
        return code, out

    def status_lines(self) -> list[str]:
        v = self.values()
        est = llm.est_messages(v.get("messages") or [], None) if v.get("messages") else 0
        stat = self.diff(stat=True).strip().splitlines()
        return [f"session {self.sid} · turn {v.get('turn_no', 0)} · mode {mode_label(self.approval['mode'])}",
                f"model {llm.MODEL} · context ~{est:,}/{llm.CONTEXT_WINDOW:,} tokens · {llm.STATS['calls']} calls this run "
                f"({llm.STATS['seconds']:.0f}s, in {llm.STATS['prompt_tokens']:,} / out {llm.STATS['completion_tokens']:,} tokens)"
                f" · {self.meta.get('calls', 0)} calls in this session",
                f"tests {self.test_cmd or '(none)'} · instructions {', '.join(self.inst_names) or '(none)'}",
                __import__("cap.sandbox", fromlist=["describe"]).describe(),
                "changed since session start: " + (stat[-1].strip() if stat else "nothing")] + [f"  {s.strip()}" for s in stat[:-1]]


# ---------------------------------------------------------------- 출력 모음

def banner_rows(s: ChatSession) -> list[tuple[str, str]]:
    """배너의 (이름, 값) 줄 — 줄 모드와 TUI 가 같이 쓴다."""
    return [("model", f"{llm.MODEL} (window {llm.CONTEXT_WINDOW:,})"),
            ("folder", str(s.ws)),
            ("mode", f"{mode_label(s.approval['mode'])} — {APPROVAL_HELP[s.approval['mode']]}"),
            ("tests", s.test_cmd or "(not found — verify gate off)"),
            ("instructions", ", ".join(s.inst_names) or "(none: AGENTS.md / CLAUDE.md / .cap/instructions.md)")]


def banner(s: ChatSession, resumed: bool = False) -> None:
    ui = s.ui
    ui.out(ui.bold("CAP") + ui.dim("  Code As Planned"))
    for k, v in banner_rows(s):
        ui.out(f"  {k:<12} {v}")
    ui.out(f"  {'session':<12} {s.sid}" + (" (resumed)" if resumed else "") + ui.dim("   /help · /quit · Ctrl+C interrupts the turn"))


def turn_summary(s: ChatSession, r: dict) -> tuple[str, str, list[str]]:
    """턴 결과 → (경고, 모델의 답, 바닥줄 항목). 줄 모드와 TUI 가 같이 쓴다."""
    v = r["values"]
    status = v.get("status")
    warn = ("Couldn't finish: " + (v.get("verify_note") or "turn limit")) if status == "failed" else \
        ("Stopped: " + (v.get("verify_note") or v.get("escalate_reason") or "you chose to stop")) if status == "escalated" else         (v.get("escalate_reason") or "Blocked by a hook") if status == "blocked" else ""
    changed = v.get("turn_changed") or []
    foot = [f"{r['calls']} model call{'s' if r['calls'] != 1 else ''}", f"{r['seconds']:.0f}s"]
    if changed:
        foot.append("changed " + ", ".join(changed))
    if v.get("verify_note") and status != "failed":
        note = v["verify_note"]
        foot.append(note if note.startswith("not verified") else "verified: " + note)
    return warn, s.last_answer(v), foot


def show_result(s: ChatSession, r: dict) -> None:
    ui = s.ui
    warn, ans, foot = turn_summary(s, r)
    ui.out("")
    if warn:
        ui.out(ui.yellow("⚠ " + warn))
    if ans:
        ui.out(ans)
    elif not warn:
        ui.out(ui.dim("(no reply)"))
    ui.out(ui.dim("── " + " · ".join(foot)))


def project_ok(calls: list[dict]) -> bool:
    """'이 프로젝트에서는 다시 묻지 않기'를 보여 줄지: 모두 명령 앞부분(exec:) 또는 MCP 서버(mcp:) 하나 단위일 때만."""
    from .trust import PROJECT_OK_PREFIXES
    return bool(calls) and all(str(c.get("category", "")).startswith(PROJECT_OK_PREFIXES) for c in calls)


def loop_mermaid() -> str:
    """컴파일된 대화형 코딩 루프 그래프를 Mermaid 로(문서 그림 = 실제 그래프. tests 가 docs/cap/graph.mmd 와 대조)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ui = UI(out=lambda s="": None)
        s = ChatSession(Path(d), None, "ask", ui)
        try:
            return s.app.get_graph().draw_mermaid().strip()
        finally:
            s.close()


MCP_MENTION_RX = re.compile(r"(?<![\w@])@([A-Za-z][\w-]*):(\S+)")
CONTINUE = "\x00continue"     # handle_command → 대화창: 새 요청 대신 멈춘 턴을 이어서


def recap(s: ChatSession) -> None:
    v = s.values()
    log = v.get("turn_log") or []
    where = s.stopped_midway()
    if where:
        s.ui.out(s.ui.yellow(f"The last turn stopped midway (next step: {where}). /continue picks it up from "
                             "the last checkpoint; or just type a new request."))
    if not log:
        return
    s.ui.out(s.ui.dim(f"{len(log)} previous turn{'s' if len(log) != 1 else ''}:"))
    for t in log[-3:]:
        s.ui.out(s.ui.dim(f"  > {shown_goal(t.get('goal', ''))[:80]}"))
    ans = s.last_answer(v)
    if ans:
        s.ui.out(s.ui.dim("  last reply: " + " ".join(ans.split())[:160]))


def print_diff(s: ChatSession, text: str, limit: int = 300) -> None:
    ui = s.ui
    if not text.strip():
        ui.out("No files changed in this session.")
        return
    lines = text.splitlines()
    for ln in lines[:limit]:
        if ln.startswith(("+++", "---", "diff ", "index ")):
            ui.out(ui.bold(ln))
        elif ln.startswith("+"):
            ui.out(ui.green(ln))
        elif ln.startswith("-"):
            ui.out(ui.red(ln))
        elif ln.startswith("@@"):
            ui.out(ui.cyan(ln))
        else:
            ui.out(ln)
    if len(lines) > limit:
        ui.out(ui.dim(f"… +{len(lines) - limit} lines"))


# ---------------------------------------------------------------- 대화창

def _read_line(prompt: str) -> str:
    line = input(prompt)
    if not sys.stdin.isatty():
        _print(line)            # 파이프 입력이면 기록에 보이게 되울림
    return line


def _turn(s: ChatSession, text: str | None) -> None:
    try:
        r = s.run_turn(text)
        show_result(s, r)
        plan = s.last_plan(r["values"]) if s.approval["mode"] == "plan" else []
        if plan:
            try:
                ans = _read_line("Would you like to proceed? [a] yes, auto-accept edits / [y] yes, approve edits / [b] yes, bypass permissions / [n] keep planning > ")
            except EOFError:
                ans = "n"
            mode = plan_choice(ans)
            if mode:
                for ln in set_mode(s, mode):
                    s.ui.out(ln)
                r = s.run_turn(PLAN_GO)
                show_result(s, r)
    except KeyboardInterrupt:
        s.ui.out(s.ui.dim("\n■ Interrupted · tell CAP what to do instead"))
    except RuntimeError as exc:
        s.ui.out(s.ui.red(f"Error: {llm.scrub(str(exc))[:300]}") + llm.error_hint(exc))


def _pick_session(ws: Path, arg: str) -> str | None:
    ss = list_sessions(ws)
    if not ss:
        return None
    if not arg:
        return ss[-1]["sid"]
    if arg.isdigit() and 1 <= int(arg) <= len(ss):
        return list(reversed(ss))[int(arg) - 1]["sid"]
    hit = [m["sid"] for m in ss if m["sid"].startswith(arg)]
    return hit[-1] if hit else None


SLASH_COMMANDS = [   # (명령, 인자 설명, 한 줄 설명) — /help 와 TUI 자동완성 목록이 같이 쓴다
    ("/help", "", "show commands and keys"),
    ("/init", "", "create AGENTS.md with notes about this project"),
    ("/compact", "[what to keep]", "summarize the conversation to free up context"),
    ("/review", "[uncommitted|<branch>]", "review changes (read-only): this session, uncommitted, or vs a branch"),
    ("/security-review", "[what]", "read-only security review of the changes, or of the project / given files"),
    ("/commit", "", "write a commit message and commit (asks first)"),
    ("/mode", "[chat|ask|plan|auto|bypass]", "show or switch the mode (also /approvals)"),
    ("/plan", "[task]", "plan mode: propose a plan, run it after you approve"),
    ("/status", "", "model, context, calls, changed files"),
    ("/doctor", "", "check the model server, key, git, bash and tests"),
    ("/goal", "<objective>", "keep working across turns until the goal is done (tests must pass)"),
    ("/memory", "[add|remove <text>]", "show or edit what CAP remembers across sessions"),
    ("/learn", "", "save durable facts from this session to project memory"),
    ("/skills", "", "list saved skills (step-by-step guides)"),
    ("/hooks", "[trust]", "list hooks, or trust this project's hooks"),
    ("/mcp", "[trust|enable <name>|disable <name>]", "list MCP servers (also from Claude Code/Codex), trust or turn on/off"),
    ("/rewind", "[n] [code|chat]", "list turns, or go back to before turn n (files, conversation, or both)"),
    ("/history", "<words>", "search earlier sessions in this folder"),
    ("/export", "[file]", "save this conversation as Markdown"),
    ("/permissions", "[trust]", "show allow/deny rules, or trust this project's allow rules"),
    ("/context", "", "what is using the context window"),
    ("/agents", "", "list subagent roles, including your own (.cap/agents)"),
    ("/rename", "<title>", "rename this session"),
    ("/plugins", "", "plugins in use (CAP, Claude Code, Codex) and what each adds"),
    ("/output-style", "[name|default]", "how CAP writes its answers (concise, explanatory, learning, or your own)"),
    ("/config", "", "effective settings: mode, sandbox, model, imports, rules, hooks, MCP — and where they come from"),
    ("/autocommit", "[on|off]", "commit CAP's changes after each verified turn (git)"),
    ("/task", "<what to do>", "run a task in the background on its own git branch (worktree); keep chatting"),
    ("/tasks", "[diff n|commit n|remove n]", "background tasks: status, result, diff, commit on its branch, clean up"),
    ("/fork", "[title]", "copy this conversation into a new session and continue there (the original stays)"),
    ("/bg", "[kill <id>]", "list background commands, or stop one"),
    ("/think", "[on|off]", "turn the model's thinking mode on or off"),
    ("/sandbox", "[on|off|setup|undo|appcontainer|wsl]", "show or change command sandboxing (writes only in the workspace, no network)"),
    ("/model", "[name]", "show or switch the model"),
    ("/provider", "[name [model]]", "list or switch model providers (class server, Ollama, LM Studio, OpenAI, yours)"),
    ("/diff", "", "changes made in this session"),
    ("/undo", "", "undo the last turn's file changes and messages"),
    ("/clear", "", "start a new conversation"),
    ("/resume", "[n|id]", "list or reopen past sessions"),
    ("/continue", "", "continue a turn that stopped midway (crash, error, Esc) from its last checkpoint"),
    ("/testcmd", "[command|reset]", "show or set the test command CAP uses to verify changes in this project"),
    ("/spec", "<task>", "optional: draft and lock a spec"),
    ("/graph", "[change]", "optional: build or change the plan graph"),
    ("/run", "", "optional: run the plan graph"),
    ("/quit", "", "exit"),
]


def config_lines(s: "ChatSession", ws: Path) -> list[str]:
    """/config: 지금 적용되는 설정을 한눈에(무엇이 어디서 왔는지)."""
    from . import autoreview, compat, sandbox
    from .memory import find_skills, user_home
    rules = s.rules
    hooks_n = sum(len(v) for v in s.hooks.by_event.values())
    mcp_on = list(s.mcp.configs)
    imp = {k: compat.import_on(k) for k in ("claude", "codex", "hooks")}
    return [
        f"mode          {s.approval['mode']}   (Shift+Tab, /mode, cap --mode)",
        f"sandbox       {sandbox.describe()}",
        f"model         {llm.MODEL} · provider {llm.PROVIDER['name']} ({llm.BASE_URL}) · thinking "
        f"{'on' if llm.thinking() else 'off'} · context {llm.CONTEXT_WINDOW:,}",
        f"autocommit    {'on' if s.autocommit else 'off'} · output style {s.output_style or 'default'}",
        f"imports       Claude Code {'on' if imp['claude'] else 'off'} · Codex {'on' if imp['codex'] else 'off'} · "
        f"their hooks {'on' if imp['hooks'] else 'off'}   (~/.cap/settings.json \"import\", CAP_IMPORT=none)",
        f"plugins       {len(compat.plugins(ws))} · skills {len(find_skills(ws))} · commands "
        f"{len(all_commands(ws)) - len(SLASH_COMMANDS)}",
        f"rules         allow {len(rules.get('allow', []))} · deny {len(rules.get('deny', []))}"
        + (f" · {len(rules['untrusted_allow'])} project allow rules not trusted (/permissions trust)"
           if rules.get("untrusted_allow") else ""),
        f"auto review   {autoreview.describe()}",
        f"hooks         {hooks_n}" + (" · project hooks not trusted (/hooks trust)" if s.hooks.untrusted else ""),
        f"mcp           on: {', '.join(mcp_on) or 'none'} · available but off: "
        f"{len([n for n in s.mcp.foreign if n not in s.mcp.configs])} (/mcp)",
        f"instructions  {', '.join(s.inst_names) or 'none'}",
        f"files         user {user_home()} · project {ws / '.cap'}",
    ]


def quick_memory(ws: Path, text: str) -> str:
    """`# 내용` (Claude Code 의 # 단축): 모델을 거치지 않고 프로젝트 기억에 한 줄 더한다."""
    from .memory import change_memory
    ok, msg = change_memory(ws, "add", text.strip(), "project")
    return ("✓ Remembered for this project: " + text.strip()[:100]) if ok else ("· " + msg.splitlines()[0])


def review_diff(s: "ChatSession", ws: Path, target: str) -> tuple[str, str]:
    """/review 대상 → (diff, 설명). 빈 대상 = 이 세션, uncommitted = 커밋 안 한 것 전부, 그 밖 = git 브랜치·커밋 기준."""
    import subprocess

    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60)
    if not target:
        return s.diff(), "the changes below, made in this session"
    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return "", "not a git repository"
    if target == "uncommitted":
        d = git("diff", "HEAD").stdout
        new = [f for f in git("ls-files", "--others", "--exclude-standard").stdout.splitlines() if f.strip()]
        if new:
            d += "\n# untracked files: " + ", ".join(new[:30])
        return d, "the uncommitted changes below"
    if not re.fullmatch(r"[\w./@^~-]+", target) or git("rev-parse", "--verify", "--quiet", target).returncode != 0:
        return "", f"no such branch or commit: {target}"
    base = git("merge-base", target, "HEAD").stdout.strip() or target
    return git("diff", base).stdout, f"the changes below, compared with {target}"


def all_commands(ws: Path) -> list[tuple[str, str, str]]:
    """기본 명령 + 사용자 명령(.cap/commands/*.md) — /help 와 대화창 자동완성이 같이 쓴다."""
    from .extras import custom_commands
    from .memory import find_skills
    base = {c for c, _, _ in SLASH_COMMANDS}
    custom = custom_commands(ws)
    out = SLASH_COMMANDS + [(n, "[args]", (v["description"] or "custom command")[:60])
                            for n, v in custom.items() if n not in base]
    taken = base | set(custom)
    out += [("/" + n, "[task]", "skill: " + v["description"][:54]) for n, v in find_skills(ws).items()
            if v.get("user_invocable", True) and "/" + n not in taken and re.fullmatch(r"[\w.-]+", n)]
    return out


def help_text() -> str:
    rows = [f"  {c + (' ' + a if a else ''):<36} {d}" for c, a, d in SLASH_COMMANDS]
    return "\n".join([
        "Type a task in plain words — CAP reads, edits, runs and tests code. The conversation continues.",
        "  @path                                attach a file, folder listing or image (.png .jpg) to your message",
        "  !command                             run a shell command yourself; CAP sees the output next turn",
        "  # note                               remember a fact for this project (no model call)",
        "  (typing while CAP works)             your message is sent to CAP at its next step",
        *rows,
        "Modes: chat (read-only) · ask (default: asks before edits and commands) · plan (plan first, then run)",
        "       auto (asks only for risky commands) · bypass (never asks; hard limits still apply)",
        "       Shift+Tab cycles modes in the full-screen UI; on the command line use --mode <name>.",
        "Esc / Ctrl+C interrupts the current turn; the session is kept. Esc twice when idle: pick a turn to rewind to.",
    ])


INIT_PROMPT = ("Explore this repository (list the top-level files, read the README, build/config files and a few key "
               "source files) and write AGENTS.md at the repository root for coding agents. Include: what the project "
               "does, how to install it, the exact command to run the tests, the folder layout, and code style or "
               "conventions you observed. Keep it under 60 lines and only state facts you checked. If AGENTS.md "
               "already exists, improve it and keep its useful content.")
REVIEW_PROMPT = ("Review the changes below, made in this session. Do not edit files or run commands; you may read and "
                 "search files for context. Report, most important first: bugs, missing or weak tests, risky changes, "
                 "then small cleanups. Cite file:line for each point and keep it short. If nothing is wrong, say so.")
SECURITY_PROMPT = ("Do a security review of {what}. Do not edit files or run commands; you may read and search files. "
                   "Look for: injection (SQL, shell, path traversal, template), unsafe deserialization or eval, secrets "
                   "in code or logs, missing authentication or authorization checks, unsafe file permissions, weak "
                   "crypto or randomness, and input that reaches dangerous calls without validation. For each finding "
                   "give file:line, why it is exploitable, and a concrete fix, most severe first. Report only real, "
                   "specific issues; if you find none, say so.")
COMMIT_PROMPT = ("Commit the current changes with git. Run `git status` and `git diff` (and `git diff --staged`), "
                 "write a concise commit message (imperative subject line under 60 characters, a short body only if "
                 "it helps), then run `git add -A` and `git commit -m \"...\"`. Do not push. If this folder is not a "
                 "git repository or there is nothing to commit, say so and stop.")


def attach_mentions(ws: Path, text: str) -> list[str]:
    """`@경로` 로 지목한 파일 내용(폴더면 목록)을 요청에 붙일 조각으로. 작업 폴더 밖·비밀 파일은 건너뛴다."""
    out, used, seen = [], 0, set()
    root = ws.resolve()
    for m in MENTION_RX.finditer(text):
        name = m.group(1).replace("\\", "/")
        if name in seen:
            continue
        seen.add(name)
        p = (ws / name).resolve()
        if not (p == root or root in p.parents) or is_secret_path(name) or not p.exists():
            continue
        if p.is_file() and p.suffix.lower() in llm.IMAGE_EXT:     # 그림: 모델에게 이미지로 보낸다
            if p.stat().st_size > llm.IMAGE_MAX_BYTES:
                out.append(f"--- @{name} --- (image not attached: larger than {llm.IMAGE_MAX_BYTES // 1_000_000} MB)")
                continue
            out.append(f"--- @{name} (image) ---\n[[image:{p.as_posix()}]]")
            continue
        if p.is_dir():
            items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()
                           if not x.name.startswith(".") and x.name not in ("__pycache__", "node_modules"))
            chunk = f"--- @{name} (folder) ---\n" + "  ".join(items[:200])
        else:
            try:
                body = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lines = body.splitlines()
            shown = "\n".join(lines[:MENTION_LINES])[:MENTION_CHARS]
            n = shown.count("\n") + 1 if shown else 0
            more = f" — first {n} of {len(lines)} lines; use read_file for the rest" if n < len(lines) else ""
            chunk = f"--- @{name} ({len(lines)} lines{more}) ---\n{shown.replace('[[image:', '[[ image:')}"
        if used + len(chunk) > MENTION_TOTAL:
            out.append(f"--- @{name} --- (not attached: too much attached already; use read_file)")
            continue
        out.append(chunk)
        used += len(chunk)
    return out


def doctor_lines(ws: Path, test_cmd: str | None = None, ping: bool = True) -> list[str]:
    """시작 점검(NVIDIA apply §5.3 스모크): 키·서버·모델·git·bash·테스트 명령·.cap 쓰기."""
    import shutil
    from .tools import find_bash, python_note
    out = []

    def line(ok: bool | None, what: str, detail: str) -> None:
        out.append(f"{'✓' if ok else '·' if ok is None else '✗'} {what:<12} {detail}")
    try:
        llm._load_key()
        line(True, "API key", "found (value not shown)")
        key_ok = True
    except RuntimeError as exc:
        line(False, "API key", str(exc)[:120])
        key_ok = False
    if ping and key_ok:
        t0 = time.perf_counter()
        try:
            llm.chat([{"role": "user", "content": "Reply with: ok"}], max_tokens=4, temperature=0.0)
            line(True, "model", f"{llm.MODEL} answered in {time.perf_counter() - t0:.1f}s")
        except RuntimeError as exc:
            line(False, "model", f"{llm.MODEL}: {llm.scrub(str(exc))[:120]}")
    line(bool(shutil.which("git")), "git", shutil.which("git") or "not found — /undo and /diff need git")
    b = find_bash()
    line(bool(b) or None, "bash", b or "not found — commands run in the system shell")
    line(True, "python", python_note(ws))
    tc = test_cmd if test_cmd is not None else detect_test_cmd(ws)
    line(bool(tc) or None, "tests", tc or "no test command found — changes cannot be verified")
    from . import sandbox as sbx
    line(True if sbx.backend() else None, "sandbox", sbx.describe().split(": ", 1)[1])
    try:
        d = cap_dir(ws)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".write_check").write_text("ok", encoding="utf-8")
        (d / ".write_check").unlink()
        line(True, ".cap", str(d))
    except OSError as exc:
        line(False, ".cap", f"cannot write: {exc}")
    return out


def shown_goal(goal: str) -> str:
    """화면용: 요청 뒤에 붙인 덧붙임(@파일 내용·!명령 결과)은 뺀다."""
    return goal.split(CONTEXT_MARK, 1)[0]


def mode_label(mode: str) -> str:
    return mode


def set_mode(s: ChatSession, mode: str) -> list[str]:
    """모드를 바꾸고 화면에 낼 줄을 돌려준다(줄 모드·TUI 공용). 바이패스는 켤 때 경고 한 줄."""
    m = normalize_mode(mode)
    s.approval["mode"] = m
    return [f"mode: {mode_label(m)} — {APPROVAL_HELP[m]}"] + ([BYPASS_WARNING] if m == "bypass" else [])


PLAN_GO = "Carry out the plan above. Update the todo list as you finish each step."
PLAN_CHOICES = {"y": "ask", "1": "ask", "a": "auto", "2": "auto", "b": "bypass", "3": "bypass"}


def plan_choice(answer: str) -> str | None:
    """플랜 승인 답 → 실행할 모드(y=승인, a=오토, b=바이패스). 그 밖은 None(실행 안 함)."""
    return PLAN_CHOICES.get((answer or "").strip().lower()[:1])


def handle_command(s: ChatSession, ws: Path, line: str) -> tuple[ChatSession, bool, str | None]:
    """슬래시 명령 하나를 처리한다(줄 모드·TUI 공용).
    반환: (지금 세션 — /clear·/resume 이면 새 것, 종료 여부, 이어서 돌릴 턴 문장 — `/plan 할 일` 이면 그 문장)."""
    ui = s.ui
    cmd, _, rest = line.partition(" ")
    rest = rest.strip()
    try:
        if cmd in ("/quit", "/exit", "/q"):
            return s, True, None
        elif cmd in ("/help", "/?"):
            ui.out(help_text())
        elif cmd == "/context":
            for ln in s.context_lines():
                ui.out(ln)
        elif cmd == "/agents":
            from .agents import ROLES, custom_agents
            base = {"explore": "read-only investigation", "review": "read-only review for bugs",
                    "worker": "edits only the files it is given"}
            for r in ROLES:
                ui.out(f"  {r:<12} {base[r]}")
            for n, c in custom_agents(ws).items():
                ui.out(f"  {n:<12} {c['description'] or 'custom'} ({'edits given files' if c['writes'] else 'read-only'})")
            ui.out("Add your own in .cap/agents/<name>.md (description:, tools: read|edit, then instructions).")
        elif cmd == "/autocommit":
            if rest in ("on", "off"):
                s.autocommit = rest == "on"
            ui.out(f"autocommit: {'on' if s.autocommit else 'off'} — after a turn that changed files and did not fail "
                   "verification, CAP commits only the files it changed (git).")
        elif cmd == "/task":
            from . import tasks as T
            ui.out(T.start(ws, rest)[1])
        elif cmd == "/tasks":
            from . import tasks as T
            parts = rest.split()
            if len(parts) == 2 and parts[0] == "diff" and parts[1].isdigit():
                ui.out(T.diff(ws, int(parts[1]))[-6000:])
            elif len(parts) == 2 and parts[0] == "remove" and parts[1].isdigit():
                ui.out(T.remove(ws, int(parts[1])))
            elif len(parts) == 2 and parts[0] == "commit" and parts[1].isdigit():
                ui.out(T.commit(ws, int(parts[1])))
            else:
                for ln in T.describe(ws):
                    ui.out(ln)
        elif cmd == "/fork":
            new = s.fork(rest)
            ui.out(f"Forked: now in session {new.sid} (copied {len(new.values().get('messages') or [])} messages). "
                   f"The original is {s.sid} — /resume to go back.")
            s.close()
            s = new
        elif cmd == "/output-style":
            from .styles import available
            styles = available(ws)
            if rest in ("default", "off", "none"):
                s.output_style = ""
            elif rest:
                if rest.lower() in styles:
                    s.output_style = rest.lower()
                else:
                    ui.out(f"No output style named {rest}.")
            for n, (desc, _) in sorted(styles.items()):
                ui.out(f"  {'❯' if n == s.output_style else ' '} {n:<14} {desc}")
            ui.out(f"Output style: {s.output_style or 'default'}  (add your own in .cap/output-styles/<name>.md or "
                   ".claude/output-styles)")
        elif cmd == "/config":
            for ln in config_lines(s, ws):
                ui.out(ln)
        elif cmd == "/plugins":
            from .compat import describe_plugins
            for ln in describe_plugins(ws):
                ui.out("  " + ln)
            ui.out("Install: cap plugin add <folder | owner/repo>. Claude Code and Codex plugins you enabled there are "
                   "used too (skills, commands, agents; their MCP servers via /mcp enable, hooks via import.hooks).")
        elif cmd == "/rename":
            if rest:
                s.meta["title"] = rest[:80]
                s._save_meta()
                ui.out(f"Session renamed: {rest[:80]}")
            else:
                ui.out(f"Session title: {s.meta.get('title') or '(none)'}  — /rename <title> to change")
        elif cmd in ("/status", "/cost"):
            for ln in s.status_lines():
                ui.out(ln)
        elif cmd == "/diff":
            print_diff(s, s.diff())
        elif cmd == "/undo":
            ui.out(s.undo())
        elif cmd in ("/clear", "/new"):
            mode = s.approval["mode"]
            s.close()
            s = ChatSession(ws, None, mode, ui)
            ui.out(f"New conversation: session {s.sid} (/resume to reopen older ones)")
        elif cmd == "/testcmd":
            from .trust import set_project_setting
            if rest == "reset":
                set_project_setting(ws, "test_cmd", "")
            elif rest:
                set_project_setting(ws, "test_cmd", rest)
            ui.out(f"test command for this project: {detect_test_cmd(ws) or '(none found)'}"
                   + ("" if rest else "  — set: /testcmd <command>, back to auto: /testcmd reset"))
            if rest:
                ui.out("It is used from the next request (the harness runs it to verify changes).")
        elif cmd == "/continue":
            if s.pending() or s.stopped_midway():
                return s, False, CONTINUE
            ui.out("Nothing to continue: the last turn finished.")
        elif cmd == "/resume":
            ss = list(reversed(list_sessions(ws)))
            if not rest:
                if not ss:
                    ui.out("No saved sessions.")
                for k, m in enumerate(ss[:10], 1):
                    cur = "  ← current" if m["sid"] == s.sid else ""
                    ui.out(f"  {k}) {m['sid']}  {m.get('turns', 0)} turns  {m.get('title', '')[:50]}{cur}")
                ui.out("To continue one: /resume <number>")
            else:
                sid2 = _pick_session(ws, rest)
                if not sid2:
                    ui.out(f"No session '{rest}'")
                else:
                    mode = s.approval["mode"]
                    s.close()
                    s = ChatSession(ws, sid2, mode, ui)
                    ui.out(f"Resumed session {sid2}")
                    recap(s)
        elif cmd in ("/mode", "/approval", "/approvals"):     # 보조(기본은 TUI 의 Shift+Tab, 줄 모드는 --mode)
            if rest:
                try:
                    for ln in set_mode(s, rest):
                        ui.out(ln)
                except ValueError as exc:
                    ui.out(str(exc))
            else:
                ui.out(f"current mode: {mode_label(s.approval['mode'])}")
                for k, r in MODE_TABLE.items():
                    ui.out(f"  {k:<7} {r.label} — {r.help}")
        elif cmd == "/plan":
            for ln in set_mode(s, "plan"):
                ui.out(ln)
            if rest:
                return s, False, rest
            ui.out("Describe the task: CAP will read the code and propose a plan, then run it after you approve.")
        elif cmd == "/init":
            return s, False, INIT_PROMPT
        elif cmd == "/compact":
            ui.out(s.compact(rest))
        elif cmd == "/memory":
            from . import memory as M
            act, _, txt = rest.partition(" ")
            if act in ("add", "remove") and txt.strip():
                ok, msg = M.change_memory(ws, act, txt.strip(), "project")
                ui.out(msg)
            else:
                for scope in ("user", "project"):
                    items = M.read_memory(ws, scope)
                    ui.out(f"{scope} memory ({M.memory_path(ws, scope)}): " + ("" if items else "(empty)"))
                    for x in items:
                        ui.out(f"  - {x}")
                ui.out("Add with /memory add <fact>, or ask CAP to remember something. Applies from the next session.")
        elif cmd == "/goal":
            if not rest:
                ui.out("Usage: /goal <objective>   e.g. /goal make all tests pass")
            else:
                def turn_done(r):
                    show_result(s, r)
                ui.out(s.run_goal(rest, on_turn=turn_done, on_note=ui.out))
        elif cmd == "/learn":
            for ln in s.learn():
                ui.out(ln)
        elif cmd == "/skills":
            from . import memory as M
            sk = M.find_skills(ws)
            if not sk:
                ui.out("No skills. Add one as .cap/skills/<name>/SKILL.md with a 'description:' line in its --- header.")
            for v in sk.values():
                ui.out(f"  {v['name']:<20} {v['description'][:80]}  ({v['scope']})")
        elif cmd == "/hooks":
            from . import hooks as H
            if rest == "trust":
                ui.out(H.trust(ws))
                s.hooks = H.Hooks(ws)
                ui.out("Restart CAP (or /clear) so the graph picks up the trusted hooks.")
            else:
                for ln in s.hooks.describe():
                    ui.out("  " + ln)
        elif cmd == "/mcp":
            from . import mcp_client as MC
            if rest == "trust":
                ui.out(MC.trust(ws))
            elif rest.startswith(("enable ", "disable ")):
                from .compat import set_foreign_enabled
                act, name = rest.split(" ", 1)
                name = name.strip()
                if act == "enable" and name not in s.mcp.foreign:
                    ui.out(f"No server named {name} in your Claude Code / Codex / plugin settings.")
                else:
                    cur = set_foreign_enabled(name, act == "enable")
                    ui.out(f"MCP {act}d: {name}. On: {', '.join(cur) or 'none'}. Start a new conversation (/clear) to connect.")
            else:
                for ln in s.mcp.describe():
                    ui.out("  " + ln)
        elif cmd == "/rewind":
            parts = rest.split()
            if parts and parts[0].isdigit():
                what = parts[1] if len(parts) > 1 and parts[1] in ("code", "chat", "both") else "both"
                ui.out(s.rewind(int(parts[0]), what))
            else:
                log = s.values().get("turn_log") or []
                for t in log:
                    ui.out(f"  {t.get('n')}) {shown_goal(t.get('goal', ''))[:70]}")
                ui.out("Go back with /rewind <n> [code|chat] (default: both files and conversation)." if log else "No turns yet.")
        elif cmd == "/history":
            from .extras import search_history
            hits = search_history(ws, rest) if rest else []
            for h in hits:
                ui.out("  " + h)
            ui.out("Open one with /resume <session>." if hits else "No matches." if rest else "Usage: /history <words>")
        elif cmd == "/export":
            from .extras import export_markdown
            out_p = Path(rest) if rest else cap_dir(ws) / "exports" / f"{s.sid}.md"
            out_p = out_p if out_p.is_absolute() else ws / out_p
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(export_markdown(s.values().get("messages") or [], s.sid), encoding="utf-8")
            ui.out(f"Saved {out_p}")
        elif cmd == "/permissions":
            from .extras import load_rules, project_settings
            if rest == "trust":
                from .extras import project_allow_hash
                from .trust import set_trusted_hash
                h = project_allow_hash(ws)
                if h:
                    set_trusted_hash("permissions", ws, h)
                fresh = load_rules(ws)
                s.rules.clear()
                s.rules.update(fresh)                  # 루프가 같은 dict 를 보므로 바로 적용
                ui.out(f"Trusted project allow rules ({h[:12]})." if h else
                       "No project allow rules (.cap/settings.json, .claude/settings.json).")
            from .trust import clear_project_approvals, project_approvals
            if rest == "forget":
                n = clear_project_approvals(ws)
                ui.out(f"Forgot {n} saved approval(s) for this project (takes effect in a new session: /clear).")
            for kind in ("deny", "allow"):
                for t, p in s.rules.get(kind, []):
                    ui.out(f"  {kind:<5} {t}({p})")
            saved = project_approvals(ws)
            if saved:
                ui.out("  saved for this project (asked once, 'don't ask again in this project'): " + ", ".join(saved)
                       + "  — /permissions forget to clear")
            if s.rules.get("untrusted_allow"):
                ui.out(f"Project allow rules (.cap/ or .claude/settings.json) are NOT trusted yet "
                       f"({', '.join(map(str, s.rules['untrusted_allow']))[:120]}) — /permissions trust to use them.")
            ui.out('Rules live in .cap/settings.json or ~/.cap/settings.json: {"permissions": {"allow": ["Bash(npm run test:*)"], "deny": ["Bash(curl:*)"]}}')
        elif cmd in ("/bg", "/bashes"):
            from .extras import bg_kill, bg_list
            if rest.startswith("kill "):
                ui.out(bg_kill(ws, rest[5:].strip())[1])
            else:
                ui.out(bg_list(ws))
        elif cmd == "/sandbox":
            from . import sandbox as sbx
            if rest in ("on", "off", "auto", "wsl", "landlock", "appcontainer"):
                sbx.STATE["mode"] = rest
            elif rest == "setup wsl":
                ui.out("Creating a Python environment with pytest inside WSL (~/.cap/wsl-venv)…")
                ok, out = sbx.wsl_setup()
                ui.out(("Done: " if ok else "Failed: ") + out.splitlines()[-1] if out else ("Done." if ok else "Failed."))
            elif rest == "setup":
                if os.name == "nt":
                    ok, out = sbx.appc_setup()                # Windows 자체 격리(WSL 불필요)
                    ui.out(("Done: " if ok else "Failed: ") + out)
                else:
                    ui.out("Linux uses Landlock automatically; nothing to set up.")
            elif rest == "undo":
                ui.out(sbx.appc_undo(ws))
            ui.out(sbx.describe())
            if s.approval["mode"] == "bypass" and sbx.backend():
                ui.out("Note: bypass mode runs commands without the sandbox.")
        elif cmd == "/think":
            if rest in ("on", "off"):
                llm.set_thinking(rest == "on")
            ui.out(f"thinking mode: {'on' if llm.thinking() else 'off'} (on is slower but can help on hard problems)")
        elif cmd == "/provider":
            if rest:
                try:
                    ui.out(llm.use_provider(rest.split()[0], rest.split()[1] if len(rest.split()) > 1 else None))
                except ValueError as exc:
                    ui.out(str(exc))
            for name, p in llm.providers().items():
                cur = "  ← current" if name == llm.PROVIDER["name"] else ""
                key = f"key ${p['env_key']}" if p.get("env_key") else "no key"
                ui.out(f"  {name:<10} {p.get('base_url', '')}  {p.get('model', '')}  {key}{cur}")
            ui.out('Add your own in ~/.cap/settings.json: {"model_providers": {"name": {"base_url": …, "env_key": …, '
                   '"model": …, "context_window": …}}}')
        elif cmd == "/model":
            try:
                models = [m.id for m in llm.client().models.list().data]
            except Exception as exc:  # noqa: BLE001
                models = []
                ui.out(f"could not list models: {llm.scrub(str(exc))[:120]}")
            if rest and rest in models:
                llm.MODEL = rest
            elif rest:
                ui.out(f"no model named {rest}")
            ui.out(f"model: {llm.MODEL}" + (f"   available: {', '.join(models)}" if models else ""))
        elif cmd == "/doctor":
            for ln in doctor_lines(ws, s.test_cmd):
                ui.out(ln)
        elif cmd == "/review":
            diff, what = review_diff(s, ws, rest.strip())
            if not diff.strip():
                ui.out(f"Nothing to review ({what})." if "no such" in what or "not a git" in what
                       else "No changes to review.")
            else:
                s.turn_mode = "chat"          # 이 한 턴은 읽기 전용
                cut = diff[:REVIEW_DIFF_MAX] + ("\n… (diff truncated)" if len(diff) > REVIEW_DIFF_MAX else "")
                prompt = REVIEW_PROMPT.replace("the changes below, made in this session", what)
                return s, False, f"{prompt}\n\n```diff\n{cut}\n```"
        elif cmd == "/security-review":
            diff = s.diff()
            s.turn_mode = "chat"              # 읽기 전용
            if rest:
                return s, False, SECURITY_PROMPT.format(what=rest)
            if diff.strip():
                cut = diff[:REVIEW_DIFF_MAX] + ("\n… (diff truncated)" if len(diff) > REVIEW_DIFF_MAX else "")
                return s, False, (SECURITY_PROMPT.format(what="the changes below, made in this session")
                                  + f"\n\n```diff\n{cut}\n```")
            return s, False, SECURITY_PROMPT.format(what="this project (start from entry points and input handling)")
        elif cmd == "/commit":
            return s, False, COMMIT_PROMPT
        elif cmd in ("/spec", "/graph", "/run"):
            _optional_flow(cmd, rest)
        else:
            from .extras import custom_commands, expand_command
            custom = custom_commands(ws).get(cmd)
            if custom:
                return s, False, expand_command(custom, rest, ws)
            if cmd.startswith("/mcp__") and getattr(s, "mcp", None) is not None and cmd[1:] in s.mcp.prompts:
                ok, text = s.mcp.get_prompt(cmd, rest)          # MCP 서버의 프롬프트(Claude Code 와 같은 이름)
                if ok:
                    return s, False, text
                ui.out(text)
                return s, False, None
            from .memory import find_skills                      # Claude Code 처럼 스킬을 /이름 으로 바로
            sk = find_skills(ws).get(cmd[1:])
            if sk and sk.get("user_invocable", True):
                return s, False, (f"Use the skill `{sk['name']}`: open it with the skill tool first and follow it."
                                  + (f"\n\nTask: {rest}" if rest else ""))
            ui.out(f"Unknown command: {cmd}  (/help)")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            ui.out(str(exc.code))
    except RuntimeError as exc:
        ui.out(f"Error: {llm.scrub(str(exc))[:300]}")
    return s, False, None


def repl(ws: Path, approval: str = "ask", resume: str | None = None) -> int:
    ui = UI()
    sid = None
    if resume is not None:
        sid = _pick_session(ws, "" if resume == "last" else resume)
        if sid is None:
            ui.out("No session to resume — starting a new one.")
    s = ChatSession(ws, sid, approval, ui)
    banner(s, resumed=sid is not None)
    if sid is not None:
        recap(s)
        if s.pending():
            ui.out(ui.yellow("Resuming a pending approval from last time."))
            _turn(s, None)
    from . import updates
    updates.start()                    # 새 버전 확인(하루 한 번, 백그라운드) — 알림은 다음 입력 줄 앞에
    ui.out("")
    try:
        while True:
            note = updates.take()
            if note:
                ui.out(ui.yellow(note))
            try:
                line = _read_line(ui.bold("> ")).strip()
            except EOFError:
                ui.out("")
                return 0
            except KeyboardInterrupt:
                ui.out("\n(/quit to exit)")
                continue
            if not line:
                continue
            if line.startswith("# ") and line[2:].strip():
                ui.out(quick_memory(ws, line[2:]))
                continue
            if line.startswith("!") and line[1:].strip():
                code, out = s.shell(line[1:].strip())
                ui.out(out.rstrip()[-4000:])
                ui.out(ui.dim(f"── exit {code} · CAP will see this output with your next message"))
                continue
            if not line.startswith("/"):
                _turn(s, line)
                ui.out("")
                continue
            try:
                s, quit_, text = handle_command(s, ws, line)
                if quit_:
                    return 0
                if text:
                    _turn(s, text)
                    ui.out("")
            except KeyboardInterrupt:
                ui.out("\nInterrupted.")
    finally:
        s.close()


def _optional_flow(cmd: str, rest: str) -> None:
    """선택 기능: 기존 스펙 → 그래프 → 재생 흐름(사용자가 명시적으로 부를 때만)."""
    from . import cli
    if cmd == "/spec":
        cli.cmd_spec(cli._ns(task=rest or None, name=None, set=None, yes=False, lock=False, show=not rest))
    elif cmd == "/graph":
        cli.cmd_graph(cli._ns(modify=[rest] if rest else None, yes=False, regen=False, show=False, preapprove=None))
    else:
        cli.cmd_run(cli._run_ns())


def structured_output(s: "ChatSession", v: dict, schema: dict) -> dict | str:
    """Codex exec --output-schema: 작업이 끝난 뒤, 이번 결과를 주어진 JSON 스키마로 정리한다(서버가 형식을 강제)."""
    note = (f"Status: {v.get('status')}. Verification: {v.get('verify_note') or 'none'}. "
            f"Changed files: {', '.join(v.get('turn_changed') or []) or 'none'}.\nYour final answer:\n{s.last_answer(v)}")
    msgs = [{"role": "system", "content": "Fill the JSON schema from the coding session result below. Use only facts "
                                          "stated there; do not invent values."},
            {"role": "user", "content": note[:6000]}]
    try:
        return llm.chat_json(msgs, schema, name="output", max_tokens=1200, ledger=s.ledger, tag="output_schema")
    except (RuntimeError, ValueError) as exc:
        return f"could not produce the schema: {llm.scrub(str(exc))[:200]}"


class EventUI(UI):
    """`cap exec --events`: 진행을 JSON 한 줄씩(JSONL) 낸다 — Codex `exec --json` 의 이벤트 모양
    (thread.started · turn.started · item.completed · turn.completed/turn.failed)."""

    def __init__(self, emit=None):
        super().__init__(out=lambda s="": None)
        self.tty = self.color = False
        self.emit = emit or (lambda d: print(json.dumps(d, ensure_ascii=False), flush=True))
        self.n = 0

    def item(self, item: dict) -> None:
        self.n += 1
        self.emit({"type": "item.completed", "item": {"id": f"item_{self.n}", **item}})

    def event(self, kind: str, **d) -> None:
        if kind == "tool":
            name, args, res = d["name"], d.get("args") or {}, d.get("res") or {}
            status = "completed" if res.get("ok", True) else "failed"
            out = llm.scrub(str(res.get("output", "")))[:2000]
            if name in ("run", "run_tests"):
                self.item({"type": "command_execution", "command": args.get("cmd") or res.get("cmd") or name,
                           "exit_code": res.get("exit"), "status": status, "aggregated_output": out})
            elif name in ("edit_file", "write_file"):
                kind_ = "add" if name == "write_file" and not d.get("before") else "update"
                self.item({"type": "file_change", "status": status,
                           "changes": [{"path": args.get("path", ""), "kind": kind_}]})
            elif name.startswith("mcp__"):
                _, server, tool = (name.split("__", 2) + ["", ""])[:3]
                self.item({"type": "mcp_tool_call", "server": server, "tool": tool, "status": status})
            elif name == "web_search":
                self.item({"type": "web_search", "query": args.get("query", "")})
            elif name == "todo":
                self.item({"type": "todo_list", "items": [{"text": t.get("text", ""), "completed": bool(t.get("done"))}
                                                          for t in args.get("items") or [] if isinstance(t, dict)]})
            else:
                self.item({"type": "tool_call", "name": name, "status": status})
        elif kind == "deny":
            self.item({"type": "error", "message": f"blocked {d.get('tool')}: {d.get('reason')}"})
        elif kind == "note":
            self.item({"type": "agent_message", "text": d.get("text", "")})
        elif kind == "verify":
            self.item({"type": "verification", "passed": bool(d.get("ok")), "text": d.get("text", "")})
        elif kind == "compact":
            self.item({"type": "context_compaction", "before": d.get("before"), "after": d.get("after")})
        elif kind == "subagent_done":
            self.item({"type": "subagent", "label": d.get("label"), "status": d.get("status")})


def one_shot(ws: Path, task: str, approval: str = "ask", as_json: bool = False,
             schema: dict | None = None, events: bool = False, resume: str | None = None,
             max_turns: int | None = None, out_file: str | None = None) -> int:
    """`cap "할 일"` / `cap exec "할 일"`: 대화창 없이 한 번 실행하고 끝낸다(Codex exec). --json 이면 결과를 JSON 한 줄로,
    --events 면 진행을 JSONL 이벤트로. resume: 지난 세션(id·번호·"last")에 이어서(Codex `exec resume`)."""
    if max_turns:                      # Claude Code -p --max-turns: 이 실행의 모델 호출(도구 라운드) 상한
        from . import loop as _L
        _L.MAX_TURNS = max(1, int(max_turns))

    def save_last(answer: str) -> None:   # Codex exec -o: 마지막 답을 파일로
        if out_file:
            Path(out_file).write_text(answer or "", encoding="utf-8")
    sid = None
    if resume is not None:
        sid = _pick_session(ws, "" if resume in ("", "last", "--last") else resume)
        if sid is None:
            msg = f"no session to resume ({resume or 'last'})"
            print(json.dumps({"status": "error", "error": msg}) if (as_json or events) else msg)
            return 2
    if as_json or events:
        ui = EventUI() if events else UI(out=lambda s="": None)
        ui.tty = ui.color = False
        s = ChatSession(ws, sid, approval, ui)
        if events:
            ui.emit({"type": "thread.started", "thread_id": s.sid})
            ui.emit({"type": "turn.started"})
        try:
            r = s.run_turn(task, ask=lambda p: "" if p.get("kind") == "question" else "n")   # 비대화식: 승인은 거절, 질문은 모델이 정함
            v = r["values"]
            out = {"status": v.get("status"), "answer": s.last_answer(v), "changed": v.get("turn_changed") or [],
                   "verify_note": v.get("verify_note", ""), "calls": r["calls"],
                   "seconds": round(r["seconds"], 1), "session": s.sid}
            if schema is not None:
                so = structured_output(s, v, schema)
                out["output"] = so if isinstance(so, dict) else None
                if not isinstance(so, dict):
                    out["output_error"] = so
            save_last(out["answer"])
            if events:
                ui.item({"type": "agent_message", "text": out["answer"]})
                ui.emit({"type": "turn.completed" if v.get("status") == "done" else "turn.failed",
                         "usage": {"input_tokens": llm.STATS.get("prompt_tokens", 0),
                                   "output_tokens": llm.STATS.get("completion_tokens", 0)}, "result": out})
            else:
                print(json.dumps(out, ensure_ascii=False))
            return 0 if v.get("status") == "done" else 1
        except RuntimeError as exc:
            err = llm.scrub(str(exc))[:300]
            print(json.dumps({"type": "error", "message": err} if events else {"status": "error", "error": err},
                             ensure_ascii=False))
            return 1
        finally:
            s.close()
    ui = UI()
    s = ChatSession(ws, sid, approval, ui)
    ui.out(ui.dim(f"CAP · {llm.MODEL} · {ws} · mode {approval} · session {s.sid} (resume: cap --resume)"))
    ui.out(ui.bold("> ") + task)
    try:
        r = s.run_turn(task)
        show_result(s, r)
        save_last(s.last_answer(r["values"]))
        return 0 if r["values"].get("status") == "done" else 1
    except KeyboardInterrupt:
        ui.out(ui.dim("\n■ Interrupted. Resume with: cap --resume"))
        return 130
    except RuntimeError as exc:
        ui.out(ui.red(f"Error: {llm.scrub(str(exc))[:300]}"))
        return 1
    finally:
        s.close()
