"""도구 지연 로딩: 설명이 큰 MCP 도구는 tool_search 뒤로 숨기고, 찾으면 싣는다(Claude Code·Hermes 방식)."""
import json

from cap import loop as L
from cap import mcp_client as MC
from test_chat import call, session, ws  # noqa: F401


class FakeMCP:
    def __init__(self, ws, n=40):
        self.ws, self.configs, self.foreign, self.untrusted, self.errors = ws, {"big": {}}, {}, False, {}
        self.tools = {}
        for i in range(n):
            name = f"mcp__big__tool_{i}"
            desc = f"[MCP big] Tool number {i}. " + ("Takes a screenshot of the browser page. " if i == 7 else "") + "x" * 120
            self.tools[name] = {"server": "big", "tool": f"tool_{i}", "schema": {"type": "function", "function": {
                "name": name, "description": desc, "parameters": {"type": "object", "properties": {}}}}}
        self.called = []

    def schemas(self):
        return [t["schema"] for t in self.tools.values()]

    def call(self, name, args):
        self.called.append(name)
        return True, f"{name} ran"

    def describe(self):
        return []

    def close(self):
        pass


def test_big_mcp_server_is_hidden_behind_tool_search(ws, monkeypatch):
    monkeypatch.setattr(MC, "MCPManager", FakeMCP)
    seen_tools = []
    replies = iter([
        {"tool_calls": [call("tool_search", query="browser screenshot")]},
        {"tool_calls": [{"id": "c_shot", "type": "function",
                         "function": {"name": "mcp__big__tool_7", "arguments": "{}"}}]},
        {"content": "찍었습니다."}])

    def fake_chat(messages, tools=None, **kw):
        seen_tools.append([t["function"]["name"] for t in tools or []])
        r = next(replies)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, approval="bypass")
    s.run_turn("브라우저 스크린샷 찍어줘")
    first, second = seen_tools[0], seen_tools[1]
    assert "tool_search" in first and not any(n.startswith("mcp__big__") for n in first)     # 처음엔 숨김
    assert "mcp__big__tool_7" in second and len([n for n in second if n.startswith("mcp__")]) <= 5
    assert s.mcp.called == ["mcp__big__tool_7"]
    s.close()


def test_small_mcp_server_is_shown_directly(ws, monkeypatch):
    monkeypatch.setattr(MC, "MCPManager", lambda ws: FakeMCP(ws, n=2))
    seen = []

    def fake_chat(messages, tools=None, **kw):
        seen.append([t["function"]["name"] for t in tools or []])
        return {"content": "ok", "tool_calls": [], "finish_reason": "stop", "usage": {}, "budget_actions": [],
                "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws)
    s.run_turn("hi")
    assert "mcp__big__tool_0" in seen[0] and "tool_search" not in seen[0]
    s.close()
