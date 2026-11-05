"""`cap mcp-server`: CAP 자체를 MCP 서버로 (Codex 의 `codex mcp-server` 와 같은 생각).

다른 에이전트(Claude Code·Codex·사람이 만든 MCP 클라이언트)가 이 폴더에서 CAP 에게 코딩 일을 맡길 수 있다.
도구 하나: run_task(task, mode) → 결과 JSON(상태·답·바뀐 파일·검증·모델 호출 수).

- 비대화식이라 승인이 필요한 행동은 거절된다. 그래서 mode 는 chat(읽기만)·ask·auto 만 받는다 — bypass 는 받지 않는다
  (부르는 쪽이 권한을 올리지 못하게).
- 표준 출력은 MCP 통신선이므로 CAP 의 화면 출력은 모두 표준 오류로 돌린다.
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

ALLOWED_MODES = ("chat", "ask", "auto")


def run_task(ws: Path, task: str, mode: str = "auto") -> str:
    from . import chat as C
    from . import llm
    if mode not in ALLOWED_MODES:
        return json.dumps({"status": "error", "error": f"mode must be one of {', '.join(ALLOWED_MODES)}"})
    if not (task or "").strip():
        return json.dumps({"status": "error", "error": "task is empty"})
    with contextlib.redirect_stdout(sys.stderr):
        ui = C.UI(out=lambda s="": print(s, file=sys.stderr))
        ui.tty = ui.color = False
        s = C.ChatSession(ws, None, mode, ui)
        try:
            r = s.run_turn(task, ask=lambda p: "" if p.get("kind") == "question" else "n")
            v = r["values"]
            out = {"status": v.get("status"), "answer": s.last_answer(v), "changed": v.get("turn_changed") or [],
                   "verify_note": v.get("verify_note", ""), "calls": r["calls"], "seconds": round(r["seconds"], 1),
                   "session": s.sid}
        except RuntimeError as exc:
            out = {"status": "error", "error": llm.scrub(str(exc))[:300]}
        finally:
            s.close()
    return json.dumps(out, ensure_ascii=False)


def serve(ws: Path) -> int:
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        print("cap mcp-server needs the mcp package: pip install -e \".[mcp]\"", file=sys.stderr)
        return 2
    app = MCPServer("cap")

    @app.tool(name="run_task")
    def run_task_tool(task: str, mode: str = "auto") -> str:
        """Give CAP (a coding agent) a coding task in its workspace. It reads, edits, runs and tests code, then returns
        JSON: status, answer, changed files, verification note. mode: chat (read-only), ask or auto (actions that
        need approval are declined, since nobody is there to approve)."""
        return run_task(ws, task, mode)

    app.run()
    return 0
