"""훅 — 사용자가 정한 명령을 에이전트의 정해진 순간에 돌린다(docs/codex/11_memory_skills_hooks.md §3).

형식은 Codex·Claude Code 훅과 같다(`hooks.json`):
  {"hooks": {"PreToolUse": [{"matcher": "run|edit_file", "hooks": [{"type": "command", "command": "...", "timeout": 30}]}]}}
위치: 프로젝트 `.cap/hooks.json`, 사용자 `~/.cap/hooks.json`.

이벤트와 효과(Codex §3.3 의 축소판)
  SessionStart      stdout 이 추가 맥락으로 시스템 프롬프트에 들어간다
  UserPromptSubmit  exit 2 = 요청 거부(사유 표시), stdout = 요청에 덧붙일 맥락
  PreToolUse        exit 2 = 도구 차단(사유가 도구 결과로 모델에게), stdout = 도구 결과 앞에 덧붙일 맥락
  PostToolUse       exit 2 = 사유를 도구 결과 뒤에 붙여 모델에게 피드백, stdout = 덧붙일 맥락
  Stop              exit 2 = 끝내지 말고 사유를 새 지시로 넣어 계속(한 턴에 한 번, stop_hook_active)
입력은 JSON 한 덩어리를 stdin 으로(event, cwd, session_id, tool_name, tool_input, tool_response …).
stdout 이 JSON 이면 {"decision": "block", "reason": "..."} 와 {"additionalContext": "..."} 를 읽는다.
Claude Code 형식도 읽는다: "hookSpecificOutput" 의 permissionDecision(allow·deny·ask)·permissionDecisionReason·
updatedInput·additionalContext, PermissionRequest 의 decision.behavior(allow·deny), 공통 "continue": false + "stopReason"
(턴을 멈춤), "systemMessage"(사용자에게 보이는 알림). PreToolUse 는 권한 판정 전에 돈다(Claude Code 와 같은 순서).
종료 코드 0·2 외(실패)는 효과 없음(fail-open). 출력은 2,000자로 자른다.

신뢰(Codex §3.2): 사용자 훅은 바로 돈다. 프로젝트 훅은 남이 만든 저장소에 들어 있을 수 있으므로
`/hooks trust` 로 그 내용(해시)을 믿겠다고 해야 돈다. 내용이 바뀌면 다시 믿어 줘야 한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SubagentStop", "PreCompact",
          "Notification", "PermissionRequest", "SessionEnd")
OUT_MAX = 2000
# Claude Code·Codex 훅 이름 별칭 → CAP 도구 이름
ALIASES = {"Bash": "run", "Edit": "edit_file", "Write": "write_file", "Read": "read_file", "Grep": "search",
           "Agent": "delegate", "Task": "delegate", "Glob": "glob", "WebFetch": "web_fetch", "WebSearch": "web_search",
           "MultiEdit": "edit_file"}
# Claude Code 도구 인자 이름 → CAP 인자 이름(updatedInput 을 그대로 받기 위해)
ARG_ALIASES = {"command": "cmd", "file_path": "path", "old_string": "old", "new_string": "new"}


def map_input(d: dict) -> dict:
    return {ARG_ALIASES.get(k, k): v for k, v in d.items()} if isinstance(d, dict) else {}


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("hooks") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def project_file(ws: Path) -> Path:
    return ws / ".cap" / "hooks.json"


def user_file() -> Path:
    return Path.home() / ".cap" / "hooks.json"


def project_hash(ws: Path) -> str:
    """.cap/hooks.json 과 .claude/settings(.local).json 의 "hooks" 를 함께 — 어느 것이 바뀌어도 다시 믿어 줘야 한다."""
    from .compat import project_hook_files
    h = hashlib.sha256()
    found = False
    for p in project_hook_files(ws):
        hooks = _load(p)
        if hooks:
            found = True
            h.update(p.name.encode() + json.dumps(hooks, sort_keys=True).encode())
    from .compat import project_plugin_hooks
    for name, hooks in project_plugin_hooks(ws):
        found = True
        h.update(f"plugin:{name}".encode() + json.dumps(hooks, sort_keys=True).encode())
    return h.hexdigest() if found else ""


def trusted(ws: Path) -> bool:
    from .trust import is_trusted_hash
    h = project_hash(ws)
    return not h or is_trusted_hash("hooks", ws, h)


def trust(ws: Path) -> str:
    from .trust import set_trusted_hash
    h = project_hash(ws)
    if not h:
        return "No project hooks (.cap/hooks.json or .claude/settings.json)."
    set_trusted_hash("hooks", ws, h)
    return f"Trusted project hooks ({h[:12]}). They run from now on; if the file changes you will be asked again."


class Hooks:
    def __init__(self, ws: Path):
        self.ws = ws
        self.by_event: dict[str, list[dict]] = {e: [] for e in EVENTS}
        self.untrusted = bool(project_hash(ws)) and not trusted(ws)
        from .compat import imported_hooks, project_hook_files, project_plugin_hooks
        sources = [_load(user_file())] + ([] if self.untrusted else [_load(p) for p in project_hook_files(ws)]
                                          + [h for _, h in project_plugin_hooks(ws)])
        self.imported = imported_hooks(ws)                 # 사용자의 Claude·Codex 훅, 플러그인 훅(켰을 때만)
        sources += [h for _, h in self.imported]
        for src in sources:
            for ev, groups in src.items():
                if ev not in self.by_event or not isinstance(groups, list):
                    continue
                for g in groups:
                    for h in (g.get("hooks") or []):
                        if h.get("type", "command") == "command" and h.get("command"):
                            self.by_event[ev].append({"matcher": g.get("matcher", "") or "", "command": h["command"],
                                                      "timeout": int(h.get("timeout") or 60)})

    def any(self, event: str) -> bool:
        return bool(self.by_event.get(event))

    def describe(self) -> list[str]:
        out = []
        for ev, hs in self.by_event.items():
            for h in hs:
                out.append(f"{ev:<17} {h['matcher'] or '*':<18} {h['command'][:70]}")
        if self.untrusted:
            out.append(f"Project hooks in {project_file(self.ws)} are NOT trusted yet — run /hooks trust to enable them.")
        return out or ["No hooks. Add them in .cap/hooks.json (project) or ~/.cap/hooks.json (user)."]

    def run(self, event: str, payload: dict, tool: str = "") -> dict:
        """이 이벤트의 훅을 차례로 돌려 합친 결과:
        {block, allow, ask, reason, context, updated_input, stop, stop_reason, messages}."""
        from .tools import run_shell
        block, reasons, ctx, allow, ask = False, [], [], False, False
        updated: dict = {}
        stop, stop_reason, notes = False, "", []
        for h in self.by_event.get(event, []):
            if tool and h["matcher"] and h["matcher"] != "*":
                names = {ALIASES.get(x, x) for x in re.split(r"[|,\s]+", h["matcher"]) if x}
                if tool not in names:
                    continue
            data = json.dumps({"event": event, "hook_event_name": event, "cwd": str(self.ws), **payload},
                              ensure_ascii=False, default=str)
            # stdin 대신 임시 파일로 넘기고 셸에서 리다이렉트(같은 run_shell: 시간 제한·프로세스 트리 종료·비밀 환경 제거)
            tmp = self.ws / ".cap" / f".hook_input_{event}.json"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            code, out = run_shell(f'{h["command"]} < "{tmp.as_posix()}"', self.ws, timeout=h["timeout"])
            out = (out or "").strip()[:OUT_MAX]
            parsed = None
            if out.startswith("{"):
                try:
                    parsed = json.loads(out)
                except json.JSONDecodeError:
                    parsed = None
            if code == 2:
                block = True
                reasons.append(out or f"blocked by {event} hook")
            elif code == 0 and isinstance(parsed, dict):
                spec = parsed.get("hookSpecificOutput") if isinstance(parsed.get("hookSpecificOutput"), dict) else {}
                pd = str(spec.get("permissionDecision") or "").lower()
                beh = spec.get("decision") if isinstance(spec.get("decision"), dict) else {}
                pd = pd or str(beh.get("behavior") or "").lower()
                why = str(spec.get("permissionDecisionReason") or beh.get("message") or parsed.get("reason") or "")
                if pd == "deny":
                    block = True
                    reasons.append(why or f"denied by {event} hook")
                elif pd == "allow":
                    allow = True
                    reasons.append(why or f"allowed by {event} hook")
                elif pd == "ask":
                    ask = True
                    reasons.append(why or f"{event} hook asks for confirmation")
                new_in = spec.get("updatedInput") or beh.get("updatedInput")
                if isinstance(new_in, dict):
                    updated.update(map_input(new_in))
                if spec.get("additionalContext"):
                    ctx.append(str(spec["additionalContext"])[:OUT_MAX])
                if parsed.get("continue") is False:
                    stop, stop_reason = True, str(parsed.get("stopReason") or f"stopped by {event} hook")
                if parsed.get("systemMessage"):
                    notes.append(str(parsed["systemMessage"])[:300])
                if parsed.get("decision") in ("block", "deny"):
                    block = True
                    reasons.append(str(parsed.get("reason") or f"blocked by {event} hook"))
                elif parsed.get("decision") in ("allow", "approve"):
                    allow = True
                    reasons.append(str(parsed.get("reason") or f"allowed by {event} hook"))
                if parsed.get("additionalContext"):
                    ctx.append(str(parsed["additionalContext"])[:OUT_MAX])
            elif code == 0 and out:
                ctx.append(out)
        return {"block": block, "allow": allow and not block and not ask, "ask": ask and not block,
                "reason": "\n".join(reasons), "context": "\n".join(ctx), "updated_input": updated,
                "stop": stop, "stop_reason": stop_reason, "messages": notes}
