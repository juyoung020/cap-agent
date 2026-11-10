"""MCP 연결 — 진짜 MCP 서버(작은 파이썬 스크립트)를 띄워 붙인다. 모델은 가짜 대본."""

import json
import sys

import pytest

from cap import mcp_client as MC
from test_chat import script, session, ws  # noqa: F401  (ws 는 fixture)

pytestmark = pytest.mark.skipif(not MC.available(), reason="mcp package not installed")

SERVER = '''from mcp.server.mcpserver import MCPServer
app = MCPServer("demo")

@app.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    app.run()
'''


def _config(ws):
    (ws / "mcp_demo.py").write_text(SERVER, encoding="utf-8")
    (ws / ".cap").mkdir(exist_ok=True)
    (ws / ".cap" / "mcp.json").write_text(json.dumps({"mcpServers": {"demo": {
        "command": sys.executable, "args": [str(ws / "mcp_demo.py")]}}}), encoding="utf-8")


def mcp_call(name, **args):
    return {"id": "c_mcp", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_untrusted_project_mcp_is_not_started(ws):
    _config(ws)
    m = MC.MCPManager(ws)
    assert m.untrusted and not m.tools and "/mcp trust" in m.describe()[-1]
    m.close()


def test_mcp_tool_is_exposed_approved_and_called(ws, monkeypatch):
    _config(ws)
    MC.trust(ws)
    seen = script(monkeypatch, [{"tool_calls": [mcp_call("mcp__demo__add", a=2, b=3)]}, {"content": "2+3=5"}])
    s, _ = session(ws, "auto")
    assert "mcp__demo__add" in s.mcp.tools and s.mcp.describe()[0].startswith("✓ demo: 1 tools")
    asked = []
    s.run_turn("2 더하기 3", ask=lambda p: asked.append(p) or "y")
    assert asked and asked[0]["calls"][0]["category"] == "mcp:demo"          # 외부 도구는 auto 에서도 묻는다
    tool = [m["content"] for m in seen[-1] if m["role"] == "tool"][0]
    assert tool.startswith('<data source="mcp__demo__add">') and "\n5\n" in tool
    s.close()
