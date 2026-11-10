"""`cap mcp-server`: 다른 에이전트가 CAP 을 MCP 도구로 부른다."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cap import mcp_client as MC
from cap import mcp_server as S
from test_chat import FIX, script, ws  # noqa: F401

pytestmark = pytest.mark.skipif(not MC.available(), reason="mcp package not installed")
SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_run_task_returns_json_and_never_bypasses(ws, monkeypatch):
    script(monkeypatch, FIX)
    out = json.loads(S.run_task(ws, "double 고쳐줘", "auto"))
    assert out["status"] == "done" and out["changed"] == ["m.py"] and "passed" in out["verify_note"]
    assert "mode must be" in json.loads(S.run_task(ws, "x", "bypass"))["error"]
    assert json.loads(S.run_task(ws, " ", "auto"))["error"] == "task is empty"


def test_server_lists_the_tool_over_stdio(ws):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        env = dict(os.environ, PYTHONPATH=SRC)
        params = StdioServerParameters(command=sys.executable, args=["-m", "cap.cli", "mcp-server"], cwd=str(ws), env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                return {t.name: t for t in tools.tools}
    tools = asyncio.run(asyncio.wait_for(go(), 60))
    assert "run_task" in tools and "coding agent" in tools["run_task"].description
