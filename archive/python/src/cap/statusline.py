"""상태 줄 (Claude Code 의 statusLine 과 같은 형식).

~/.cap/settings.json:  {"statusLine": {"type": "command", "command": "python ~/.cap/status.py"}}
명령은 표준 입력으로 세션 정보 JSON(cwd·workspace·model·session_id·mode·context)을 받고, 출력 첫 줄이 대화창 아래에 보인다.
Claude Code 의 ~/.claude/settings.json statusLine 은 다른 에이전트의 명령이라 훅처럼 "import": {"hooks": true} 일 때만 쓴다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def command() -> str:
    from .compat import _read_json, claude_home, import_on, settings
    sl = settings().get("statusLine")
    if not sl and import_on("hooks") and import_on("claude"):
        sl = _read_json(claude_home() / "settings.json").get("statusLine")
    if isinstance(sl, dict) and sl.get("type", "command") == "command" and sl.get("command"):
        return str(sl["command"])
    return ""


def render(ws: Path, info: dict, cmd: str | None = None) -> str:
    """명령을 돌려 첫 줄을 돌려준다(실패하면 ""). 3초 안에 끝나야 한다."""
    from .tools import run_shell
    cmd = cmd if cmd is not None else command()
    if not cmd:
        return ""
    tmp = ws / ".cap" / ".statusline_input.json"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        code, out = run_shell(f'{cmd} < "{tmp.as_posix()}"', ws, timeout=3)
    except OSError:
        return ""
    if code != 0:
        return ""
    first = next((ln for ln in out.splitlines() if ln.strip()), "")
    return ANSI.sub("", first).strip()[:120]


def info(ws: Path, sid: str, mode: str, model: str, used: int, window: int) -> dict:
    return {"hook_event_name": "Status", "session_id": sid, "cwd": str(ws),
            "workspace": {"current_dir": str(ws), "project_dir": str(ws)},
            "model": {"id": model, "display_name": model}, "mode": mode,
            "context": {"used_tokens": used, "window": window}, "version": "cap"}
