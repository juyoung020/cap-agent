"""그 밖에 일반 코딩 에이전트가 갖춘 것들.

- 사용자 명령: `.cap/commands/<이름>.md`·`~/.cap/commands/` → `/이름 인자` (Claude Code 의 사용자 슬래시 명령 형식, `$ARGUMENTS`)
- 권한 규칙: `.cap/settings.json`·`~/.cap/settings.json` 의 permissions.allow / deny (Claude Code 형식 `Bash(npm run test:*)`)
- 백그라운드 명령: run(background=true) → 로그 파일로, process 도구로 목록·출력·종료 (Codex unified exec, Hermes terminal background)
- 웹: web_fetch(url) — 페이지 글만, web_search(query) — 설정된 검색 API(Brave·Tavily·SearXNG) 결과 (Hermes web 도구)
- 편집 뒤 린트 비교: ruff 가 있으면 이번 편집이 '새로' 만든 문제만 알린다 (Hermes 07 §7-3)
- 지난 세션 검색(/history), 대화 내보내기(/export)
"""

from __future__ import annotations

import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 사용자 명령

def _cmd_dirs(ws: Path) -> list[Path]:
    from .memory import user_home
    return [user_home() / "commands", ws / ".cap" / "commands"]


def custom_commands(ws: Path) -> dict[str, dict]:
    """Claude Code(.claude/commands)·Codex(~/.codex/prompts)·플러그인 명령도 읽는다. 플러그인 명령은 /플러그인:이름,
    다른 것과 겹치지 않으면 /이름 으로도."""
    from .compat import command_dirs
    out: dict[str, dict] = {}
    plugin_short: list[tuple[str, dict]] = []
    for prefix, d in command_dirs(ws):
        for f in sorted(d.glob("**/*.md"))[:200]:
            text = f.read_text(encoding="utf-8", errors="replace")
            desc, body = "", text
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
            if m:
                body = text[m.end():]
                for ln in m.group(1).splitlines():
                    k, sep, v = ln.partition(":")
                    if sep and k.strip() == "description":
                        desc = v.strip().strip("'\"")
            stem = re.sub(r"[^A-Za-z0-9_-]", "-", f.stem).lower()
            name = f"/{prefix}:{stem}" if prefix else "/" + stem
            item = {"name": name, "description": desc or (body.strip().splitlines()[0][:60] if body.strip() else ""),
                    "body": body.strip(), "path": f}
            out.pop(name, None)
            out[name] = item
            if prefix:
                plugin_short.append(("/" + stem, item))
    for short, item in plugin_short:
        out.setdefault(short, item)
    return out


INLINE_RX = re.compile(r"!`([^`\n]+)`")


def run_inline_commands(text: str, ws: Path) -> str:
    """Claude Code 명령의 !`명령` — 실행해서 그 출력으로 바꾼다. 권한표가 승인 없이 허용하는 명령만(샌드박스 안),
    나머지는 실행하지 않고 이유를 남긴다. 출력의 비밀 값은 가린다."""
    from . import sandbox
    from .policy import classify_command
    from .tools import clip, redact, run_shell, secret_values

    def sub(m: re.Match) -> str:
        cmd = m.group(1).strip()
        d = classify_command(cmd, ws)
        if d.verdict != "allow":
            return f"(not run: `{cmd}` — {d.reason or 'needs approval'})"
        code, out = run_shell(cmd, ws, timeout=60, sandbox=sandbox.backend())
        out = redact(clip(out.strip(), 3000), secret_values(ws))
        return f"\n```\n$ {cmd}{'' if code == 0 else f'  (exit {code})'}\n{out}\n```\n"
    return INLINE_RX.sub(sub, text) if "!`" in text else text


def expand_command(cmd: dict, args: str, ws: Path | None = None) -> str:
    """$ARGUMENTS(전체)와 $1..$9(공백으로 나눈 조각) — Claude Code·Codex 명령과 같은 자리 표시.
    ws 를 주면 !`명령` 도 실행해 넣는다."""
    body = cmd["body"]
    parts = args.split()
    used = "$ARGUMENTS" in body or re.search(r"\$[1-9]", body)
    body = body.replace("$ARGUMENTS", args)
    body = re.sub(r"\$([1-9])", lambda m: parts[int(m.group(1)) - 1] if int(m.group(1)) <= len(parts) else "", body)
    body = body if used else body + (f"\n\n{args}" if args else "")
    return run_inline_commands(body, ws) if ws is not None else body


# ---------------------------------------------------------------- 권한 규칙

TOOL_ALIASES = {"bash": "run", "edit": "edit_file", "write": "write_file", "read": "read_file", "grep": "search",
                "webfetch": "web_fetch", "websearch": "web_search", "multiedit": "edit_file"}
RULE_RX = re.compile(r"^\s*([A-Za-z_]+)\s*(?:\((.*)\))?\s*$")


def project_settings(ws: Path) -> Path:
    return ws / ".cap" / "settings.json"


def project_rule_files(ws: Path) -> list[Path]:
    """CAP 과 Claude Code 의 프로젝트 설정(같은 permissions 형식)."""
    return [p for p in (ws / ".cap" / "settings.json", ws / ".claude" / "settings.json",
                        ws / ".claude" / "settings.local.json") if p.is_file()]


def _perms(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("permissions") or {}
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def project_allow_hash(ws: Path) -> str:
    import hashlib
    allows = [(p.name, _perms(p).get("allow") or []) for p in project_rule_files(ws)]
    allows = [a for a in allows if a[1]]
    return hashlib.sha256(json.dumps(allows, sort_keys=True).encode()).hexdigest() if allows else ""


def _norm_pattern(tool: str, pat: str) -> str:
    """Claude Code 규칙 모양 맞추기: Read(./src/**) → src/**, WebFetch(domain:x.com) → *://x.com/*"""
    if tool == "web_fetch" and pat.startswith("domain:"):
        return f"*://{pat[7:]}/*"
    return pat[2:] if pat.startswith("./") else pat


def load_rules(ws: Path) -> dict[str, list]:
    """{'allow': [(tool, pattern)], 'deny': [...], 'untrusted_allow': [...]}.
    사용자 설정(~/.cap/settings.json, Claude Code 의 ~/.claude/settings.json)과 프로젝트 설정(.cap/settings.json,
    .claude/settings(.local).json)을 합친다. 프로젝트의 거부 규칙은 늘 따르고, 허용 규칙은 /permissions trust 뒤에만 쓴다
    (남의 저장소가 Bash(*) 를 넣어 둘 수 있다)."""
    from .compat import claude_home, import_on
    from .memory import user_home
    from .trust import is_trusted_hash
    rules: dict[str, list] = {"allow": [], "deny": [], "untrusted_allow": []}
    user_files = [user_home() / "settings.json"] + ([claude_home() / "settings.json"] if import_on("claude") else [])
    ph = project_allow_hash(ws)
    proj_ok = not ph or is_trusted_hash("permissions", ws, ph)
    for p, is_proj in [(f, False) for f in user_files] + [(f, True) for f in project_rule_files(ws)]:
        perms = _perms(p)
        allow_ok = not is_proj or proj_ok
        if not allow_ok and perms.get("allow"):
            rules["untrusted_allow"] += list(perms.get("allow") or [])
        for kind in (("allow", "deny") if allow_ok else ("deny",)):
            for r in perms.get(kind) or []:
                m = RULE_RX.match(str(r))
                if m:
                    tool = TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
                    rules[kind].append((tool, _norm_pattern(tool, (m.group(2) or "*").strip())))
    return rules


def _subject(tool: str, args: dict, ws: Path | None = None) -> str:
    if tool == "run":
        return " ".join(str(args.get("cmd", "")).split())
    if tool in ("web_fetch",):
        return str(args.get("url", ""))
    if tool in ("web_search",):
        return str(args.get("query", ""))
    raw = str(args.get("path", ""))
    if ws is not None and raw:          # 절대 경로·./·../ 로 돌려 써도 같은 규칙에 걸리게 작업 폴더 기준으로 편다
        try:
            q = Path(raw) if Path(raw).is_absolute() else ws / raw
            return q.resolve().relative_to(ws.resolve()).as_posix()
        except (ValueError, OSError):
            return Path(raw).as_posix()
    return raw.replace("\\", "/")


def match_rule(rules: dict, kind: str, tool: str, args: dict, ws: Path | None = None) -> str:
    """맞는 규칙 문자열(없으면 ""). 명령은 Claude Code 처럼 `npm run test:*` 의 `:*` 를 앞부분 일치로 본다."""
    subj = _subject(tool, args, ws)
    if tool == "run":
        return _match_command(rules, kind, subj)
    for t, pat in rules.get(kind, []):
        if t not in (tool, "*"):
            continue
        if fnmatch.fnmatchcase(subj, pat) or fnmatch.fnmatchcase(subj, pat.replace("**", "*")):
            return f"{t}({pat})"
    return ""


CMD_SEP_RX = re.compile(r"\s*(?:&&|\|\||[;|&\n])\s*")


def _match_command(rules: dict, kind: str, cmd: str) -> str:
    """명령 규칙. 이어 붙인 명령(`a; b`, `a && b`, `a | b`)은 조각마다 본다:
    거부는 한 조각이라도 맞으면, 허용은 모든 조각이 맞을 때만. 치환·리다이렉트가 있으면 허용하지 않는다."""
    pats = [(t, pat, pat[:-2] + "*" if pat.endswith(":*") else pat) for t, pat in rules.get(kind, [])
            if t in ("run", "*")]
    if not pats:
        return ""
    parts = [x for x in CMD_SEP_RX.split(cmd.strip()) if x]

    def hit(seg: str) -> str:
        return next((f"{t}({pat})" for t, pat, p in pats if fnmatch.fnmatchcase(seg, p)), "")
    if kind == "deny":
        return hit(cmd.strip()) or next((h for h in map(hit, parts) if h), "")
    if "$(" in cmd or "`" in cmd or re.search(r"[<>]", cmd):
        return ""
    found = [hit(seg) for seg in parts]
    return found[0] if found and all(found) else ""


# ---------------------------------------------------------------- 백그라운드 명령

_PROCS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def bg_start(ws: Path, cmd: str, sandbox: str = "", extra_env: dict | None = None) -> str:
    from .tools import find_bash, shell_env
    d = ws / ".cap" / "bg"
    d.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        n = 1
        while f"bg{n}@{ws}" in _PROCS or (ws / ".cap" / "bg" / f"bg{n}.log").exists():
            n += 1
        pid_id = f"bg{n}"
    log = d / f"{pid_id}.log"
    bash = find_bash()
    args: Any = [bash, "-c", cmd] if bash else cmd
    if sandbox:                                       # 백그라운드 명령도 같은 격리 안에서
        from .sandbox import wrap
        wrapped, err = wrap(cmd, ws, sandbox)
        if wrapped is None:
            raise RuntimeError(f"sandbox could not start: {err}")
        args, bash = wrapped, "sandbox"
    kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    f = open(log, "wb")
    env = {**shell_env(), **(extra_env or {})}          # extra_env: CAP 자신을 띄울 때만(모델 키)
    p = subprocess.Popen(args, cwd=ws, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                         shell=not bash, **kw)
    with _LOCK:
        _PROCS[pid_id + "@" + str(ws)] = {"id": pid_id, "ws": str(ws), "cmd": cmd, "proc": p, "log": log, "file": f,
                                          "started": time.time()}
    return pid_id


def _find(ws: Path, pid_id: str) -> dict | None:
    return _PROCS.get(f"{pid_id}@{ws}")


def bg_list(ws: Path) -> str:
    rows = []
    for v in _PROCS.values():
        if v["ws"] != str(ws):
            continue
        code = v["proc"].poll()
        rows.append(f"{v['id']}  {'running' if code is None else f'exited {code}'}  {time.time() - v['started']:.0f}s  {v['cmd'][:80]}")
    return "\n".join(rows) or "(no background processes)"


def bg_output(ws: Path, pid_id: str, tail: int = 4000) -> tuple[bool, str]:
    v = _find(ws, pid_id)
    if not v:
        return False, f"no background process {pid_id}. " + bg_list(ws)
    v["file"].flush()
    text = v["log"].read_bytes().decode("utf-8", "replace")
    code = v["proc"].poll()
    state = "running" if code is None else f"exited {code}"
    return True, f"[{pid_id} {state}] {v['cmd'][:80]}\n" + (text[-tail:] or "(no output yet)")


def bg_write(ws: Path, pid_id: str, text: str, enter: bool = True, wait: float = 1.5) -> tuple[bool, str]:
    """실행 중인 백그라운드 명령의 입력(stdin)에 글을 보내고, 잠깐 기다린 뒤 새로 나온 출력을 돌려준다
    (Codex write_stdin: "계속할까요? [y/N]" 같은 질문에 답하거나 REPL 에 한 줄 넣기)."""
    v = _find(ws, pid_id)
    if not v:
        return False, f"no background process {pid_id}. " + bg_list(ws)
    p = v["proc"]
    if p.poll() is not None or p.stdin is None:
        return False, f"{pid_id} has already exited ({p.poll()})"
    v["file"].flush()
    start = v["log"].stat().st_size if v["log"].exists() else 0
    data = text + ("\n" if enter and not text.endswith("\n") else "")
    try:
        p.stdin.write(data.encode("utf-8"))
        p.stdin.flush()
    except OSError as exc:
        return False, f"could not write to {pid_id}: {exc}"
    end = time.time() + max(0.0, min(wait, 10.0))
    while time.time() < end and p.poll() is None:
        time.sleep(0.1)
    v["file"].flush()
    new = v["log"].read_bytes()[start:].decode("utf-8", "replace")
    code = p.poll()
    state = "running" if code is None else f"exited {code}"
    return True, f"[{pid_id} {state}] sent {len(data)} chars\n" + (new[-4000:] or "(no new output yet)")


def stdin_target_is_shell(ws: Path, pid_id: str) -> bool:
    """그 백그라운드 명령이 셸·대화형 인터프리터면 보내는 글 = 새 명령이다(권한표로 다시 판정해야 함)."""
    v = _find(ws, pid_id)
    if not v:
        return False
    words = str(v["cmd"]).strip().split()
    if not words:
        return False
    first = words[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    shells = {"bash", "sh", "zsh", "fish", "dash", "cmd", "powershell", "pwsh", "wsl"}
    repls = {"python", "python3", "py", "node", "irb", "ruby", "perl", "psql", "mysql", "sqlite3", "lua", "php"}
    rest = [w for w in words[1:] if not w.startswith("-")]
    return first in shells or (first in repls and not rest)


TOAST_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def toast_script(title: str, body: str) -> str:
    """Windows 토스트 알림을 띄우는 PowerShell 스크립트(추가 설치 없음, WinRT)."""
    def q(s: str) -> str:
        return "'" + s.replace("'", "''")[:200] + "'"
    return ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]"
            " > $null; $x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); $t = $x.GetElementsByTagName('text'); "
            f"$t.Item(0).AppendChild($x.CreateTextNode({q(title)})) > $null; "
            f"$t.Item(1).AppendChild($x.CreateTextNode({q(body)})) > $null; "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            f"{q(TOAST_AUMID)}).Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def desktop_notify(title: str, body: str) -> bool:
    """긴 작업이 끝났을 때 데스크톱 알림(Windows). ~/.cap/settings.json 의 "notifications": false 로 끈다."""
    from .compat import settings
    if os.name != "nt" or settings().get("notifications") is False:
        return False
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", toast_script(title, body)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return False
    return True


def clipboard_image(ws: Path) -> Path | None:
    """클립보드의 그림을 .cap/images/ 에 PNG 로 저장(Windows: PowerShell + System.Windows.Forms, 추가 설치 없음).
    그림이 없거나 Windows 가 아니면 None."""
    if os.name != "nt":
        return None
    d = ws / ".cap" / "images"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"paste-{time.strftime('%Y%m%d-%H%M%S')}.png"
    ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$i = [System.Windows.Forms.Clipboard]::GetImage(); "
          f"if ($i) {{ $i.Save('{str(p).replace(chr(39), chr(39) * 2)}', [System.Drawing.Imaging.ImageFormat]::Png); 'ok' }}")
    try:
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p if p.is_file() and p.stat().st_size > 0 else None


def bg_kill(ws: Path, pid_id: str) -> tuple[bool, str]:
    from .tools import _kill_tree
    v = _find(ws, pid_id)
    if not v:
        return False, f"no background process {pid_id}"
    if v["proc"].poll() is None:
        _kill_tree(v["proc"])
    return True, f"{pid_id} stopped"


def bg_kill_all(ws: Path) -> None:
    for k, v in list(_PROCS.items()):
        if v["ws"] == str(ws):
            bg_kill(ws, v["id"])
            try:
                v["file"].close()
            except OSError:
                pass
            with _LOCK:
                _PROCS.pop(k, None)


# ---------------------------------------------------------------- 웹

from . import __version__  # noqa: E402

UA = f"CAP-coding-agent/{__version__} (+https://github.com/juyoung020/cap-agent)"


def _get(url: str, timeout: int = 20) -> tuple[int, str, str]:
    """httpx(certifi 인증서 묶음)로 받는다 — 파이썬 기본 urllib 는 이 PC 의 인증서 저장소를 못 찾는 경우가 있다.
    CAP 이라고 밝히는 User-Agent 를 쓰고, 막는 사이트는 막힌 대로 둔다."""
    import httpx
    with httpx.stream("GET", url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,*/*"},
                      follow_redirects=True, timeout=timeout) as r:
        raw = b""
        for chunk in r.iter_bytes():
            raw += chunk
            if len(raw) > 2_000_000:
                break
        return r.status_code, r.headers.get("content-type", ""), raw.decode(r.encoding or "utf-8", "replace")


def html_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", src)
    src = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    src = html.unescape(src)
    lines = [" ".join(ln.split()) for ln in src.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def web_fetch(url: str, limit: int = 6000) -> tuple[bool, str]:
    if not re.match(r"^https?://", url or ""):
        return False, "url must start with http:// or https://"
    try:
        status, ctype, body = _get(url)
    except Exception as exc:  # noqa: BLE001
        return False, f"fetch failed: {type(exc).__name__}: {str(exc)[:200]}"
    text = html_text(body) if "html" in ctype or body.lstrip().startswith("<") else body
    more = f"\n…(+{len(text) - limit} chars cut)" if len(text) > limit else ""
    return True, f"[{status} {url}]\n{text[:limit]}{more}"


SEARCH_KEYS = ("BRAVE_API_KEY", "TAVILY_API_KEY", "CAP_SEARXNG_URL")


def search_backend() -> str:
    """정식 검색 API 가 설정돼 있을 때만 검색을 켠다(검색 결과 페이지를 긁지 않는다)."""
    return next((k for k in SEARCH_KEYS if os.environ.get(k)), "")


def web_search(query: str, n: int = 6) -> tuple[bool, str]:
    import httpx
    backend = search_backend()
    if not backend:
        return False, "web search is not configured (set BRAVE_API_KEY, TAVILY_API_KEY or CAP_SEARXNG_URL)"
    try:
        if backend == "BRAVE_API_KEY":
            r = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                          headers={"Accept": "application/json", "X-Subscription-Token": os.environ[backend]}, timeout=20)
            items = [(i.get("title", ""), i.get("url", ""), i.get("description", ""))
                     for i in (r.json().get("web") or {}).get("results", [])]
        elif backend == "TAVILY_API_KEY":
            r = httpx.post("https://api.tavily.com/search", json={"query": query, "max_results": n},
                           headers={"Authorization": f"Bearer {os.environ[backend]}"}, timeout=30)
            items = [(i.get("title", ""), i.get("url", ""), i.get("content", "")) for i in r.json().get("results", [])]
        else:
            r = httpx.get(os.environ[backend].rstrip("/") + "/search", params={"q": query, "format": "json"},
                          headers={"User-Agent": UA}, timeout=20)
            items = [(i.get("title", ""), i.get("url", ""), i.get("content", "")) for i in r.json().get("results", [])]
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return False, f"search failed: {type(exc).__name__}: {str(exc)[:200]}"
    out = [f"- {html_text(t)}\n  {u}\n  {html_text(d)[:200]}" for t, u, d in items[:n]]
    return (True, "\n".join(out)) if out else (False, "no results")


# ---------------------------------------------------------------- 린트 비교

def lint_delta(path: str, before: str | None, after: str) -> str:
    """ruff 가 있으면 편집 전후를 비교해 '새로 생긴' 문제만(줄 번호 무시하고 코드+메시지로 비교)."""
    if not path.endswith(".py"):
        return ""
    ruff = shutil.which("ruff")
    if not ruff:
        return ""

    def issues(text: str) -> list[tuple[str, str, int]]:
        try:
            r = subprocess.run([ruff, "check", "--output-format", "json", "--stdin-filename", path, "-"], input=text,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
            return [(i.get("code") or "", i.get("message") or "", (i.get("location") or {}).get("row", 0))
                    for i in json.loads(r.stdout or "[]")]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return []
    old = {(c, m) for c, m, _ in issues(before)} if before is not None else set()
    new = [(c, m, row) for c, m, row in issues(after) if (c, m) not in old]
    if not new:
        return ""
    lines = "; ".join(f"line {row} {c} {m}" for c, m, row in new[:5])
    return f"\nNew lint issues from this edit (ruff): {lines}" + (f" (+{len(new) - 5} more)" if len(new) > 5 else "")


# ---------------------------------------------------------------- 세션 검색·내보내기

def search_history(ws: Path, query: str, limit: int = 12, width: int = 90, skip_sid: str = "") -> list[str]:
    """이 폴더의 지난 세션에서 요청(asked)·답(answered)을 찾는다. 낱말이 모두 들어간 줄만."""
    words = [w for w in query.lower().split() if w]
    hits = []
    runs = ws / ".cap" / "runs"
    for f in sorted(runs.glob("chat-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if runs.is_dir() else []:
        sid = f.stem[5:].split("-sub")[0]
        if skip_sid and sid == skip_sid:
            continue
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            text = r.get("goal") if r.get("type") == "turn_start" else r.get("text") if r.get("type") == "llm_call" else None
            if text and words and all(w in str(text).lower() for w in words):
                kind = "asked" if r["type"] == "turn_start" else "answered"
                hits.append(f"{r['ts'][:16].replace('T', ' ')}  {sid}  {kind}: {' '.join(str(text).split())[:width]}")
                if len(hits) >= limit:
                    return hits
    return hits


def export_markdown(messages: list[dict], sid: str) -> str:
    from .chat import shown_goal
    out = [f"# CAP session {sid}", ""]
    for m in messages:
        role, c = m.get("role"), (m.get("content") or "").strip()
        if role == "system":
            continue
        if role == "user":
            out += [f"## You", "", shown_goal(c).replace("[harness]", "> [harness]"), ""]
        elif role == "assistant":
            calls = [f"`{tc['function']['name']}({(tc['function'].get('arguments') or '')[:120]})`" for tc in m.get("tool_calls") or []]
            if c:
                out += ["## CAP", "", c, ""]
            if calls:
                out += ["Tools: " + ", ".join(calls), ""]
        elif role == "tool":
            out += ["<details><summary>tool result</summary>", "", "```", c[:1500], "```", "", "</details>", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- 도구 스키마

def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


SESSION_SEARCH_SCHEMA = _fn(
    "session_search", "Search earlier CAP sessions in this folder (what the user asked, what you answered). Use it "
    "when the user refers to past work ('like last time', 'the fix we did yesterday').",
    {"query": {"type": "string", "description": "a few words that should all appear"}}, ["query"])
FETCH_SCHEMA = _fn("web_fetch", "Fetch a web page (http/https) and return its text. Use for docs/error lookups the "
                   "user asks about.", {"url": {"type": "string"}}, ["url"])
SEARCH_SCHEMA = _fn("web_search", "Search the web and return titles, links and snippets.",
                    {"query": {"type": "string"}}, ["query"])


def web_schemas() -> list[dict]:
    """검색은 검색 API 가 설정됐을 때만 모델에게 보인다."""
    return [FETCH_SCHEMA] + ([SEARCH_SCHEMA] if search_backend() else [])
PROCESS_SCHEMA = _fn("process", "Manage background commands started with run(background=true): "
                     "action=list | output (needs id) | kill (needs id) | write (needs id and text: send input to the "
                     "running program, e.g. answer a y/N prompt; Enter is added).",
                     {"action": {"type": "string", "enum": ["list", "output", "kill", "write"]}, "id": {"type": "string"},
                      "text": {"type": "string"}},
                     ["action"])
