"""권한표: LLM 없는 순수 함수. allow / ask / deny.

classify(tool, args, ws, preapproved, frozen) -> Decision(verdict, reason, category)

- 작업 폴더 밖 쓰기, .cap/.git 쓰기, 동결된 평가기(테스트 파일) 쓰기 → deny
- 시스템 파괴 패턴(rm -rf /, format, mkfs, dd, shutdown, curl|sh …) → deny
- 지우기·옮기기(rm/del/mv/move/rmdir), git push/reset --hard/clean, 패키지 설치, 네트워크 → ask
- ask 이지만 category 가 사전 승인 집합(preapproved)에 있으면 allow
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

READ_TOOLS = {"read_file", "search", "glob", "symbols", "todo", "run_tests", "report_conflict", "ask_user"}
WRITE_TOOLS = {"edit_file", "write_file"}

# 항상 금지(사전 승인으로도 못 푼다)
FORBID_PATTERNS = [
    (r'''\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME|/\*|[A-Za-z]:[\\/]?)(?=[\s"';|&)]|$)''',
     "deletes root or home"),
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+\*\s*$", "deletes the whole current folder"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bformat\s+[a-zA-Z]:", "formats a drive"),
    (r"\bdd\s+if=", "writes raw disk"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shuts down the system"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bdiskpart\b", "partitions disks"),
    (r"\breg\s+(delete|add)\b", "changes the registry"),
    (r"\b(del|erase)\s+(/[a-zA-Z]\s+)*[a-zA-Z]:\\?\s*($|\*)", "deletes a whole drive"),
    (r"\b(rd|rmdir)\s+/s\s+(/q\s+)?[a-zA-Z]:\\?\s*$", "deletes a whole drive"),
    (r"(curl|wget|iwr|Invoke-WebRequest)[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python|pwsh|powershell|iex)\b", "pipes a downloaded script into a shell"),
    (r"\bchmod\s+-R\s+777\s+/", "opens all permissions"),
    (r"\bgit\s+push\b.*(--force|-f\b).*\b(main|master)\b", "force-pushes main"),
    (r">\s*/dev/sd[a-z]", "writes to a disk device"),
]

# 승인 필요: (정규식, 이유, 사전 승인 category)
ASK_PATTERNS = [
    (r"(^|[;&|]\s*|\s)(rm|del|erase|rmdir|rd|unlink|Remove-Item)\b", "delete files", "delete"),
    (r"(^|[;&|]\s*|\s)(mv|move|ren|rename|Move-Item)\b", "move or rename files", "move"),
    (r"\bgit\s+push\b", "git push", "git_push"),
    (r"\bgit\s+(reset\s+--hard|clean\b|checkout\s+--\s|restore\b|rebase\b|branch\s+-D)", "rewrite git history or working tree", "git_rewrite"),
    (r"\bgit\s+commit\b", "git commit", "git_commit"),
    (r"\b(pip|pip3|uv\s+pip|poetry|conda|npm|pnpm|yarn|cargo|apt|apt-get|brew|winget|choco)\s+(install|add|i)\b", "install packages (network)", "install"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod|ssh|scp|ftp|nc|telnet)\b", "network access", "network"),
    (r"\bsudo\b|\brunas\b", "admin rights", "admin"),
    (r"\b(kill|taskkill|pkill|Stop-Process)\b", "kill processes", "kill"),
]

# 비밀 파일: 읽기·쓰기·명령 모두 금지(Hermes 06 §6-2, D3 권한표 초안의 `.env`·키 파일 forbid)
SECRET_RX = re.compile(r"(^|[\s/\\'\"=<>|;&(])(\.env(\.[\w.-]+)?|id_rsa\w*|id_ed25519\w*|[\w.-]+\.pem|"
                       r"credentials\.json|\.netrc|\.pypirc)(?=$|[\s'\";|&)<>])", re.I)
SECRET_OK = re.compile(r"\.env\.(example|sample|template|dist)$", re.I)
# 지침 파일: 미래의 에이전트 행동을 조종하는 파일이라 인젝션이 눌러앉는 통로 → 항상 사람(Hermes 07 §5-3)
INSTRUCTION_NAMES = {"cap.md", "cap.local.md", "agents.md", "agents.override.md", "claude.md", "claude.local.md", "soul.md", ".cursorrules",
                     "gemini.md", ".clinerules", ".mcp.json"}
# 다른 에이전트의 설정 폴더: 스킬·명령·하위 에이전트·훅·MCP 가 들어 있어 다음 세션의 행동을 바꾼다 → 쓰기는 늘 사람
AGENT_CONFIG_DIRS = {".claude", ".codex", ".agents", ".claude-plugin", ".codex-plugin", ".cap-plugin"}
ALWAYS_HUMAN = {"instructions"}   # 사전 승인·자동 응답(--yes)으로도 허용하지 않는 category


def is_secret_path(path: str) -> bool:
    name = PurePosixPath(str(path).replace("\\", "/")).name
    if SECRET_OK.search(name):
        return False
    return bool(SECRET_RX.search(" " + name + " "))


def _secret_in_cmd(cmd: str) -> str:
    for m in SECRET_RX.finditer(cmd):
        tok = m.group(2)
        if not SECRET_OK.search(tok):
            return tok
    return ""


SHELL_META = re.compile(r"[;&|<>`$(){}\n\r*?\[\]!'\"]")
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


def is_test_path(rel_path: str) -> bool:
    return bool(TEST_PATH.search(rel_path.replace("\\", "/")))


def _bad_test_target(target: str, ws: Path) -> str:
    """run_tests 의 target 은 테스트 경로·pytest 옵션만. 셸 문자·작업 폴더 밖·비밀 파일이면 사유를 돌려준다."""
    if not target.strip():
        return ""
    if SHELL_META.search(target):
        return "run_tests target may only contain test paths and pytest options (no shell characters)"
    for tok in target.split():
        if tok.startswith("-"):
            continue
        path = tok.split("::", 1)[0]
        ok, _ = _inside(ws, path)
        if not ok or is_secret_path(path):
            return f"run_tests target must be a test path inside the workspace: {tok}"
    return ""


def _glob_secret(cmd: str, ws: Path) -> str:
    """글롭(*, ?, [..])을 작업 폴더 기준으로 펼쳐 비밀 파일에 닿으면 그 이름(`cat .en*`)."""
    import glob
    for tok in _tokens(cmd):
        t = tok.strip("'\"")
        if not re.search(r"[*?\[]", t):
            continue
        base = t if os.path.isabs(t) else str(ws / t)
        try:
            hits = glob.glob(base, recursive=True, include_hidden=True)
        except TypeError:                       # 3.10: include_hidden 없음 — '.*' 로 시작하는 글롭은 숨김 파일도 잡힌다
            hits = glob.glob(base, recursive=True)
        except (OSError, ValueError):
            continue
        for h in hits[:500]:
            if is_secret_path(h):
                return PurePosixPath(h.replace("\\", "/")).name
    return ""


def _secret_files(ws: Path, limit: int = 3000) -> list[str]:
    out = []
    for _root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in (".git", ".cap", ".venv", "node_modules", "__pycache__")]
        out += [f for f in files if is_secret_path(f)]
        limit -= len(files)
        if limit < 0:
            break
    return out


RECURSIVE_READ = re.compile(r"\b(grep|egrep|fgrep)\b[^;&|]*\s-(\w*[rR]\w*)\b|\b(grep|egrep|fgrep)\b[^;&|]*--recursive|"
                            r"\b(rg|ag)\b[^;&|]*(\s--hidden\b|\s-u+\b|\s--no-ignore\b)|\bfindstr\b[^;&|]*\s/s\b", re.I)


WRITEY_CMD = re.compile(r"(>|\bsed\s+-i|\btee\b|\bcp\b|\bcopy\b|\bmv\b|\bmove\b|\brm\b|\bdel\b|Set-Content|Out-File|"
                        r"Add-Content|\btruncate\b|open\(.+['\"]w)")


@dataclass
class Decision:
    verdict: str          # allow | ask | deny
    reason: str = ""
    category: str = ""


def _inside(ws: Path, path: str) -> tuple[bool, Path]:
    p = Path(path)
    if not p.is_absolute():
        p = ws / p
    try:
        rp = p.resolve()
        rp.relative_to(ws.resolve())
        return True, rp
    except (ValueError, OSError):
        return False, p


def _outside(ws: Path, tok: str) -> bool:
    """명령의 경로 토큰이 작업 폴더 밖을 가리키나(절대 경로·~·../ 포함 상대 경로)."""
    if not tok or tok.startswith("-") or re.match(r"^(/dev/null|nul)$", tok, re.I):
        return False
    if re.match(r"^[a-z][\w+.-]*://", tok, re.I):
        return False
    if re.match(r"^(\$\{?HOME\}?|\$env:USERPROFILE|%USERPROFILE%|%HOMEPATH%)", tok, re.I):
        return True
    if not re.match(r"^([A-Za-z]:[\\/]|/|~)", tok) and ".." not in tok.replace("\\", "/").split("/"):
        return False
    ok, _ = _inside(ws, os.path.expanduser(tok))
    return not ok


def _rel(ws: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def classify(tool: str, args: dict, ws: Path, preapproved: set[str] | frozenset = frozenset(),
             frozen: set[str] | frozenset = frozenset()) -> Decision:
    ws = Path(ws)
    if tool == "run_tests":
        bad = _bad_test_target(str(args.get("target") or ""), ws)
        if bad:
            return Decision("deny", bad)
        return Decision("allow")
    if tool in READ_TOOLS:
        p = args.get("path")
        if p:
            if is_secret_path(p):
                return Decision("deny", f"secret file ({PurePosixPath(str(p)).name}) is never read. Do not try another path to it")
            ok, _ = _inside(ws, p)
            if not ok:
                return _maybe(Decision("ask", "read outside the workspace", "read_outside"), preapproved)
        return Decision("allow")
    if tool in WRITE_TOOLS:
        p = args.get("path") or ""
        if not p:
            return Decision("deny", "empty path")
        ok, rp = _inside(ws, p)
        if not ok:
            return Decision("deny", f"writing outside the workspace is not allowed: {p}")
        r = _rel(ws, rp)
        top = PurePosixPath(r).parts[0] if PurePosixPath(r).parts else ""
        if top in (".cap", ".git"):
            return Decision("deny", f"{top}/ is reserved for the harness")
        if is_secret_path(r):
            return Decision("deny", f"writing a secret file ({PurePosixPath(r).name}) is not allowed")
        if r in frozen:
            # 추가만 하는 편집(새 test_ 함수·import)은 허용: '기존 테스트 파일에 새 기능 테스트도 추가해' 과제(도그푸딩 t2)
            if tool == "edit_file" and rp.is_file():
                ok_add, why = _frozen_append(rp, args)
                if ok_add:
                    return Decision("allow", f"appends new tests to a frozen file: {why}", "test_append")
                return Decision("deny", f"evaluator frozen: existing tests in {r} cannot change ({why}). Only appending new test_ functions is allowed. "
                                        "If a test is wrong, use report_conflict")
            return Decision("deny", f"evaluator frozen: {r} cannot change during the loop (append new tests with edit_file or use a new file; "
                                    "if a test is wrong, use report_conflict)")
        if PurePosixPath(r).name.lower() in INSTRUCTION_NAMES:
            # 사전 승인으로 풀리지 않는다(_maybe 안 씀). 자동 응답 모드에서도 거부(runner.ask_human)
            return Decision("ask", f"writes agent instructions ({PurePosixPath(r).name}) — needs a human", "instructions")
        if AGENT_CONFIG_DIRS & {x.lower() for x in PurePosixPath(r).parts[:-1]}:
            return Decision("ask", f"writes agent configuration ({r}) — needs a human", "instructions")
        return Decision("allow")
    if tool == "run":
        return classify_command(args.get("cmd", ""), ws, preapproved, frozen)
    if tool == "delegate":       # 하위 에이전트 자체는 허용. 하위 에이전트의 행동은 각자 이 표로 다시 판정된다
        return Decision("allow")
    if tool == "apply_patch":
        d = _patch_decisions(args, lambda t, a: classify(t, a, ws, preapproved, frozen))
        return d if d is not None else Decision("allow")
    if tool == "process" and _stdin_command(args, ws) is not None:   # 셸·REPL 에 보내는 글 = 새 명령
        return classify_command(_stdin_command(args, ws), ws, preapproved, frozen)
    if tool in ("skill", "process"):
        return Decision("allow")
    if tool in ("web_fetch", "web_search"):   # 네트워크: 밖으로 나가는 요청이라 사람이 본다(Codex: 기본 네트워크 꺼짐)
        return _maybe(Decision("ask", "web access (network)", "network"), preapproved)
    if tool.startswith("mcp__"):     # 외부 MCP 도구: 무엇을 하는지 알 수 없다 — 서버 단위로 사람이 허락
        server = tool.split("__")[1] if tool.count("__") >= 2 else "?"
        return _maybe(Decision("ask", f"MCP tool from server '{server}'", f"mcp:{server}"), preapproved)
    if tool == "remember":       # 다음 세션의 프롬프트에 들어간다 — 인젝션이 눌러앉는 통로라 사람이 본다
        return _maybe(Decision("ask", "saves a memory for future sessions", "memory"), preapproved)
    if tool in ("list_mcp_resources", "read_mcp_resource"):    # 켜 둔 MCP 서버의 데이터 읽기(Codex 와 같이 묻지 않음)
        return Decision("allow")
    if tool in ("skill_search", "plugin_search", "tool_search", "session_search"):   # 정해진 공개 저장소·마켓플레이스 카탈로그만 읽는다
        return Decision("allow")
    if tool == "plugin_install":  # 훅·MCP 서버로 명령을 실행할 수 있는 남의 묶음 — 사전 승인 없이 늘 사람
        return Decision("ask", f"installs plugin {args.get('id', '')} (it may add hooks and MCP servers)",
                        "plugin_install")
    if tool == "skill_install":  # 남이 쓴 지시문(과 스크립트)을 들여온다 — 사전 승인 없이 늘 사람
        return Decision("ask", f"installs skill {args.get('id', '')} from the internet or a marketplace", "skill_install")
    if tool == "save_skill":     # 다음 세션에 열리는 절차 — 기억과 같은 통로라 늘 사람이 본다
        return _maybe(Decision("ask", "saves a skill for future sessions", "memory"), preapproved)
    return Decision("deny", f"unknown tool: {tool}")


def append_only(before: str, after: str) -> tuple[bool, str]:
    """after 가 before 의 최상위 문장을 순서대로 그대로 갖고, 더해진 것은 새 이름의 test 함수·import 뿐인가."""
    import ast
    try:
        b, a = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return False, "syntax error after the edit"
    old = [ast.dump(n) for n in b.body]
    names = {n.name for n in b.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    k, added = 0, []
    for n in a.body:
        if k < len(old) and ast.dump(n) == old[k]:
            k += 1
            continue
        added.append(n)
    if k < len(old):
        return False, "existing lines were changed or removed"
    for n in added:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") and n.name not in names:
            continue
        what = getattr(n, "name", type(n).__name__)
        return False, f"appended something that is not a new test function ({what})"
    if not added:
        return False, "nothing changed"
    return True, f"{sum(1 for n in added if not isinstance(n, (ast.Import, ast.ImportFrom)))} new tests"


def _frozen_append(rp: Path, args: dict) -> tuple[bool, str]:
    from .tools import apply_edit_args   # tools → policy 순환을 피하려고 여기서
    before = rp.read_text(encoding="utf-8", errors="replace")
    res = apply_edit_args(before, args)
    if not res.ok or res.new_text is None:
        return False, "the edit cannot be applied"
    return append_only(before, res.new_text)


def _maybe(d: Decision, preapproved) -> Decision:
    if d.verdict == "ask" and d.category and d.category in preapproved:
        return Decision("allow", f"pre-approved ({d.category}): {d.reason}", d.category)
    return d


CARRIER_RX = re.compile(r"""(?:^|[;&|]\s*|\s)(?:bash|sh|zsh|dash|ksh|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh)"""
                        r"""(?:\s+-[-\w]+)*?\s+(?:-c|/c|/k|-Command|-command|-EncodedCommand)\s+("[^"]*"|'[^']*'|[^\s;&|]+)""", re.I)
PIPE_TO_SHELL = re.compile(r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|pwsh|powershell|python3?|py|iex|node|perl|ruby)\b|\beval\b|"
                           r"\bInvoke-Expression\b|\bxargs\b", re.I)


def _mask_quoted(c: str) -> str:
    """따옴표 안 글자는 데이터(커밋 메시지·echo 문장) — 금지·확인 패턴 검사에서 뺀다(Hermes 06 §6-2)."""
    return re.sub(r'"[^"]*"|\'[^\']*\'', '""', c)


def _carried(c: str) -> list[str]:
    """`bash -c "..."`·`cmd /c ...`·`powershell -Command ...` 안쪽 명령(Codex 10 §3.5 재귀 검사)."""
    return [m.group(1).strip("'\"") for m in CARRIER_RX.finditer(c)]


_STRICT = {"allow": 0, "ask": 1, "deny": 2}


def classify_command(cmd: str, ws: Path, preapproved=frozenset(), frozen=frozenset(), _depth: int = 0) -> Decision:
    c = (cmd or "").strip()
    if not c:
        return Decision("deny", "empty command")
    inner = [classify_command(x, ws, preapproved, frozen, _depth + 1) for x in _carried(c)] if _depth < 3 else []
    worst = max(inner, key=lambda d: _STRICT[d.verdict], default=None)
    if worst is not None and worst.verdict == "deny":
        return worst
    # 셸로 흘려 보내는 명령(`echo ... | sh`, eval, xargs)은 따옴표 안도 실행될 수 있어 전체를 본다
    scan = c if PIPE_TO_SHELL.search(c) else _mask_quoted(c)
    for pat, why in FORBID_PATTERNS:
        if re.search(pat, scan, flags=re.IGNORECASE):
            return Decision("deny", f"blocked: {why}")
    if worst is not None and worst.verdict == "ask":
        return worst
    sec = _secret_in_cmd(c) or _glob_secret(c, ws)
    if sec:
        return Decision("deny", f"commands touching secret files ({sec}) are not allowed. Do not try another path to it")
    if RECURSIVE_READ.search(c):
        secrets = _secret_files(ws)
        if secrets:
            return Decision("deny", f"a recursive search here would read secret files ({secrets[0]}); use the search tool instead")
    # 동결된 평가기 파일을 건드리는 쓰기성 명령
    if frozen and WRITEY_CMD.search(c):
        for f in frozen:
            name = PurePosixPath(f).name
            if f in c.replace("\\", "/") or re.search(r"(^|[\s/\\'\"])" + re.escape(name) + r"\b", c):
                return Decision("deny", f"evaluator frozen: the command may change {f}")
        # 동결 파일이 든 폴더 통째 삭제·이동(`rm -rf tests`): 파일 이름이 안 나와도 막는다(도그푸딩 레드팀)
        destructive = re.search(r"(^|[;&|]\s*|\s)(rm|del|erase|rmdir|rd|mv|move|unlink|Remove-Item|Move-Item)\b", c, re.I)
        for tok in (_tokens(c) if destructive else []):
            t = tok.strip("'\"").replace("\\", "/").rstrip("/")
            if not t or t.startswith("-") or t in (".", "*"):
                if t in (".", "*"):
                    return Decision("deny", "evaluator frozen: the command deletes or moves the whole workspace")
                continue
            t = t[2:] if t.startswith("./") else t
            if any(f == t or f.startswith(t + "/") for f in frozen):
                return Decision("deny", f"evaluator frozen: the command may change frozen tests in {t}/")
    # 지침 파일(AGENTS.md 등)을 셸로 쓰기
    for name in [*INSTRUCTION_NAMES, *(d + "/" for d in AGENT_CONFIG_DIRS)]:
        if re.search(r"(>|\btee\b|\bcp\b|\bmv\b|\bsed\s+-i|\bmkdir\b|Set-Content|Out-File|Add-Content)[^;&|]*"
                     + re.escape(name), c, re.I):
            return Decision("ask", f"writes agent instructions or configuration ({name}) — needs a human", "instructions")
    # 작업 폴더 밖으로 쓰기: 절대 경로·~·상대 경로(../) 모두 작업 폴더 기준으로 풀어서 본다
    targets = re.findall(r">>?\s*([^\s;&|]+)", c)
    if re.search(r"(^|[;&|]\s*)(rm|del|erase|unlink|mv|move|cp|copy|rmdir|rd|tee|touch|mkdir|ln|install|rsync|"
                 r"Remove-Item|Move-Item|Copy-Item|New-Item)\b", c, re.IGNORECASE):
        targets += _tokens(c)
    for tok in targets:
        tok = tok.strip("'\"")
        if _outside(ws, tok):
            return Decision("deny", f"writes outside the workspace: {tok}")
    for pat, why, cat in ASK_PATTERNS:
        if re.search(pat, scan, flags=re.IGNORECASE):
            return _maybe(Decision("ask", why, cat), preapproved)
    if re.search(r"\$\(|`|\beval\b|\bInvoke-Expression\b|\biex\b", c, re.IGNORECASE):
        return _maybe(Decision("ask", "command substitution / eval", "subst"), preapproved)
    if any(_outside(ws, t.strip("'\"")) for t in _tokens(c)[1:]):
        return _maybe(Decision("ask", "reads outside the workspace", "read_outside"), preapproved)
    if re.search(r"(^|[;&|]\s*)cd\s+(\.\.|/|[A-Za-z]:)", c):
        return _maybe(Decision("ask", "moves outside the workspace", "cd_outside"), preapproved)
    return Decision("allow")


# ---------------------------------------------------------------- 모드 (일반 코딩 에이전트 대화형)
#
# Claude Code 의 권한 모드처럼 5개. 판정 = classify(하드 금지·위험 분류, 모드와 무관) 위에 모드 표 한 줄을 얹는다.
# 모드를 바꾸면 표의 행만 바뀐다(MODE_TABLE). Codex 10_safety §2.1(forbidden 은 어느 정책이든 거부)·
# 12_langgraph_port §4.3(OS 샌드박스가 약하므로 기본 ask, 읽기 전용으로 알려진 명령만 allow)을 따른다.
# 어느 모드든 하드 금지(deny)는 그대로, 지침 파일(AGENTS.md 등) 쓰기는 항상 사람에게 묻는다(ALWAYS_HUMAN).

@dataclass(frozen=True)
class ModeRule:
    label: str        # 화면 이름
    help: str         # 한 줄 설명
    edit: str         # 작업 폴더 안 파일 편집
    tests: str        # run_tests (테스트 명령)
    safe_cmd: str     # 읽기·테스트 명령(ls, cat, rg, git status/diff/log, pytest)
    other_cmd: str    # 그 밖의 명령
    risky_cmd: str    # 위험 명령(삭제·이동·설치·네트워크·git 되돌리기/커밋/푸시·종료) — classify 가 ask 로 분류한 것
    outside_read: str = "ask"   # 작업 폴더 밖 읽기


MODE_TABLE: dict[str, ModeRule] = {
    #                 이름     설명                                              편집     테스트   읽기명령  기타명령  위험명령  밖 읽기
    "chat":   ModeRule("chat", "reads and answers only — no edits, no commands",        "deny", "deny", "deny", "deny", "deny"),
    "ask":    ModeRule("ask", "asks before every edit and command (reads and tests run directly)", "ask",  "allow", "allow", "ask",  "ask"),
    "plan":   ModeRule("plan", "reads only and proposes a plan; runs it after you approve", "deny", "deny", "deny", "deny", "deny"),
    "auto":   ModeRule("auto", "edits and runs commands; asks only for risky ones",      "allow", "allow", "allow", "allow", "ask"),
    "bypass": ModeRule("bypass", "runs everything without asking (hard limits still apply)", "allow", "allow", "allow", "allow", "allow", "allow"),
}
APPROVAL_MODES = tuple(MODE_TABLE)                      # ("chat", "ask", "plan", "auto", "bypass")
MODE_ALIASES = {"auto-edit": "auto", "full-auto": "auto", "default": "ask", "yolo": "bypass",
                "대화": "chat", "승인": "ask", "플랜": "plan", "오토": "auto", "바이패스": "bypass"}
APPROVAL_HELP = {k: r.help for k, r in MODE_TABLE.items()}
MODE_CYCLE = ("ask", "plan", "auto", "bypass", "chat")   # Shift+Tab 순서
BYPASS_WARNING = "⚠ Bypass permissions: everything runs without asking. Hard limits (outside writes, secret files, destructive commands) and instruction-file writes still apply."
MODE_DENY = {"chat": "chat mode: no edits or commands. Answer using read and search only",
             "plan": "plan mode: do not execute yet. Read and search, then write the plan with todo"}
# 턴마다 사용자 메시지 앞에 붙는 모드 안내(모델용)
MODE_PROMPT = {
    "chat": "[mode: chat] Answer or explain only. You may use read_file and search; do NOT edit files or run commands.",
    "plan": ("[mode: plan] Do NOT edit files or run commands yet. Read and search what you need, then call the todo tool "
             "ONCE with a concrete step-by-step plan (each step: which file/function to change and how to check it), "
             "then reply with a 1-3 sentence summary of the plan and stop. The user will approve it before you execute."),
}


def normalize_mode(mode: str | None) -> str:
    m = (mode or "ask").strip().lower()
    m = MODE_ALIASES.get(m, m)
    if m not in MODE_TABLE:
        raise ValueError(f"mode must be one of {' | '.join(APPROVAL_MODES)}")
    return m


def next_mode(mode: str) -> str:
    m = normalize_mode(mode)
    return MODE_CYCLE[(MODE_CYCLE.index(m) + 1) % len(MODE_CYCLE)]


_SAFE_FIRST = {"ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "rg", "pwd", "echo", "which", "where",
               "tree", "stat", "file", "diff", "sort", "uniq", "cut", "true", "pytest", "py.test"}
_SAFE_GIT = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame"}


def is_safe_command(cmd: str) -> bool:
    """읽기 전용·테스트 명령인가(Codex 12 §4.3 의 allow 목록: ls, cat, rg, git status/diff/log, pytest)."""
    c = (cmd or "").strip()
    if not c or re.search(r"(?<![0-9&])>(?!\s*&|\s*/dev/null|\s*nul\b)|`|\$\(|\btee\b", c, re.I):
        return False
    if re.search(r"(^|[\s/\\])\.\.([/\\\s]|$)", c):      # 상위 폴더로 올라가는 경로는 자동 허용하지 않는다
        return False
    for seg in re.split(r"&&|\|\||;|\|", c):
        toks = _tokens(seg.strip())
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) <= 2 and not re.match(r"^(\.\.|/|[A-Za-z]:|~)", toks[-1] if len(toks) > 1 else ""):
            continue
        first = toks[0].lower()
        if first in ("python", "python3", "py") and len(toks) >= 3 and toks[1] == "-m" and toks[2] == "pytest":
            continue
        if first in ("python", "python3", "py") and len(toks) == 2 and toks[1] in ("--version", "-V"):
            continue
        if first == "git" and len(toks) >= 2 and toks[1] in _SAFE_GIT:
            continue
        if first == "find" and not re.search(r"-(delete|exec|execdir|ok)\b", seg):
            continue
        if first in _SAFE_FIRST:
            continue
        return False
    return True


def _worst(ds: list[Decision]) -> Decision:
    """여러 판정 중 가장 엄한 것(거부 > 묻기 > 허용). 묻기가 여럿이면 사람 전용 범주를 앞에."""
    for v in ("deny", "ask"):
        hits = [d for d in ds if d.verdict == v]
        if hits:
            hits.sort(key=lambda d: d.category not in ("instructions", "test_edit", "scope"))
            return hits[0]
    return ds[0] if ds else Decision("allow")


def _patch_decisions(args: dict, judge) -> Decision | None:
    """apply_patch: 파일마다 쓰기(추가·수정)는 write_file, 지우기는 rm 명령으로 판정해 가장 엄한 것."""
    import shlex
    try:
        from .patch import PatchError, paths
        items = paths(str(args.get("patch", "")))
    except (ImportError, PatchError):
        return None                 # 형식이 틀리면 실행에서 실패한다(아무것도 바뀌지 않음)
    ds = [judge("run", {"cmd": "rm " + shlex.quote(p)}) if kind == "delete" else judge("write_file", {"path": p})
          for kind, p in items]
    return _worst(ds)


def _stdin_command(args: dict, ws: Path) -> str | None:
    """process(action=write) 가 셸·대화형 인터프리터로 가면 그 글(=명령), 아니면 None."""
    if str(args.get("action", "")) != "write":
        return None
    try:
        from .extras import stdin_target_is_shell
    except ImportError:
        return None
    return str(args.get("text", "")) if stdin_target_is_shell(Path(ws), str(args.get("id", ""))) else None


def classify_mode(tool: str, args: dict, ws: Path, mode: str = "ask", session: set[str] | frozenset = frozenset(),
                  frozen: set[str] | frozenset = frozenset()) -> Decision:
    """모드를 얹은 판정. session = 이 세션에서 [a](항상)로 받은 category 집합."""
    if tool == "process" and _stdin_command(args, ws) is not None:
        return classify_mode("run", {"cmd": _stdin_command(args, ws)}, ws, mode, session, frozen)
    if tool == "apply_patch":
        d = _patch_decisions(args, lambda t, a: classify_mode(t, a, ws, mode, session, frozen))
        return d if d is not None else Decision("allow")
    mode = normalize_mode(mode)
    rule = MODE_TABLE[mode]
    d = classify(tool, args, ws, session, frozen)
    if d.verdict == "deny":
        return d

    def by(v: str, ask: Decision) -> Decision:
        if v == "deny":
            return Decision("deny", MODE_DENY.get(mode, f"not allowed in {rule.label} mode"), "mode")
        return ask if v == "ask" else Decision("allow", d.reason, d.category)

    if tool == "run_tests":
        return by(rule.tests, d)
    if tool == "run" and args.get("escalate") and mode != "bypass":   # 격리 밖 실행 요청: 어느 모드든 사람(Codex escalation)
        if mode in ("chat", "plan"):
            return Decision("deny", MODE_DENY.get(mode, "not allowed"), "mode")
        if "sandbox_escape" not in session:
            return Decision("ask", "run outside the sandbox (network / writes outside the workspace)", "sandbox_escape")
    if tool in ("web_fetch", "web_search"):
        return Decision("allow", d.reason, d.category) if mode == "bypass" or d.verdict == "allow" else d
    if tool.startswith("mcp__"):
        if mode in ("chat", "plan"):
            return Decision("deny", MODE_DENY.get(mode, "not allowed"), "mode")
        return Decision("allow", d.reason, d.category) if mode == "bypass" or d.verdict == "allow" else d
    if tool in ("remember", "save_skill", "skill_install", "plugin_install"):
        if mode in ("chat", "plan"):
            return Decision("deny", MODE_DENY.get(mode, "not allowed"), "mode")
        return Decision("allow", d.reason, d.category) if mode == "bypass" or d.verdict == "allow" else d
    if tool in READ_TOOLS:
        if d.verdict == "ask" and d.category == "read_outside":
            return by(rule.outside_read, d)
        return d
    if tool in WRITE_TOOLS:
        if rule.edit == "deny":
            return by("deny", d)
        if d.verdict == "ask":          # 지침 파일 쓰기(ALWAYS_HUMAN): 어느 모드든 사람
            return d
        ok, rp = _inside(Path(ws), args.get("path") or "")
        if (mode != "bypass" and ok and rp.is_file() and is_test_path(_rel(Path(ws), rp))
                and "test_edit" not in session):     # 테스트를 고쳐 관문을 넘는 것을 막는다(auto 에서도 사람)
            return Decision("ask", "modifies an existing test file", "test_edit")
        if rule.edit == "ask" and "edit" not in session:
            return Decision("ask", "edit file", "edit")
        return Decision("allow", d.reason, d.category)
    if tool == "run":
        cmd = args.get("cmd", "")
        if rule.safe_cmd == "deny":
            return by("deny", d)
        # 셸로 테스트 파일을 쓰거나 지우는 것도 테스트 편집이다(red-team: 편집을 거절당하자 `echo "" > tests/x.py`)
        if mode != "bypass" and "test_edit" not in session and d.verdict != "deny":
            hit = _writes_test(cmd, Path(ws))
            if hit:
                return Decision("ask", f"modifies an existing test file ({hit})", "test_edit")
        if d.verdict == "ask":          # 위험 명령(classify 분류). 세션 [a] 는 classify 안 _maybe 가 이미 반영
            if d.category in ALWAYS_HUMAN:
                return d
            return by(rule.risky_cmd, d)
        if d.category:                  # 사전 승인으로 풀린 위험 명령
            return d
        if is_safe_command(cmd):
            return by(rule.safe_cmd, d)
        key = "exec:" + command_prefix(cmd)
        if rule.other_cmd == "ask":
            if key in session or ("exec:" + " ".join(cmd.split())[:200]) in session:
                return Decision("allow", "command allowed for this session", key)
            return Decision("ask", "run command", key)
        return by(rule.other_cmd, d)
    return d


BROAD_FIRST = {"bash", "sh", "zsh", "dash", "cmd", "powershell", "pwsh", "env", "sudo", "xargs", "eval", "exec",
               "nohup", "time", "timeout", "nice", "start", "call", "source", "."}
RUNNERS = {"python", "python3", "py", "node", "ruby", "perl", "deno", "bun"}
SUBCMD_TOOLS = {"npm", "pnpm", "yarn", "npx", "cargo", "go", "make", "git", "docker", "uv", "poetry", "pip", "pip3",
                "dotnet", "mvn", "gradle", "pytest", "ruff", "black", "mypy", "tsc", "jest", "conda"}


def _writes_test(cmd: str, ws: Path) -> str:
    """명령이 쓰거나 지우거나 옮기는 대상 중 테스트 파일이 있으면 그 경로."""
    targets = re.findall(r">>?\s*([^\s;&|]+)", cmd)
    if re.search(r"\bsed\s+-i|\btee\b|\btruncate\b|\b(cp|copy|mv|move|rm|del|erase|unlink|ren|rename)\b|"
                 r"Set-Content|Out-File|Add-Content|Remove-Item|Move-Item|Copy-Item|Clear-Content", cmd, re.I):
        targets += _tokens(cmd)[1:]
    for t in targets:
        t = t.strip("'\"")
        if not t or t.startswith("-"):
            continue
        ok, rp = _inside(ws, t)
        if ok and is_test_path(_rel(ws, rp)) and (rp.exists() or rp.parent.exists()):
            return _rel(ws, rp)
    return ""


def command_prefix(cmd: str) -> str:
    """'이 세션 동안 허용'을 저장할 명령 앞부분(Codex 10 §3.6). 셸·환경 운반자처럼 너무 넓은 것은 명령 전체만."""
    c = " ".join((cmd or "").split())
    if re.search(r"[;&|<>`$]", c):
        return c[:200]
    toks = [t.strip("'\"") for t in _tokens(c)]
    if not toks:
        return c
    first = re.sub(r"\.exe$", "", toks[0].replace("\\", "/").rsplit("/", 1)[-1].lower())
    if first in BROAD_FIRST:
        return c[:200]
    if first in RUNNERS:
        if len(toks) >= 3 and toks[1] == "-m":
            return " ".join([first, *toks[1:3]])
        if len(toks) >= 2 and not toks[1].startswith("-"):
            return " ".join([first, toks[1]])
        return c[:200]
    if first in SUBCMD_TOOLS and len(toks) >= 2 and not toks[1].startswith("-"):
        n = 3 if toks[1] in ("run", "exec", "x") and len(toks) >= 3 else 2
        return " ".join([first, *toks[1:n]])
    return first


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=False)
    except ValueError:
        return cmd.split()
