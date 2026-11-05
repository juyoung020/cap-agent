"""모델 호출과 프로젝트 사실: 보조 호출은 생각 끔 · 호출 오류 분류와 고칠 방법 · 되풀이 끊기 ·
매 턴 테스트 명령·지침 다시 읽기 · MCP 큰 스키마 줄이기와 긴 결과 파일로. API 호출 없음."""

import json
from types import SimpleNamespace as NS

import pytest

from cap import compact as cmp
from cap import llm
from cap import loop as L
from cap import mcp_client as MC
from test_chat import call, session, ws  # noqa: F401  (ws 는 fixture)


def _reply(content="ok", reasoning=None):
    msg = NS(content=content, tool_calls=None, reasoning_content=reasoning)
    return NS(choices=[NS(message=msg, finish_reason="stop")], usage=None)


def _fake_client(monkeypatch, create):
    monkeypatch.setattr(llm, "client", lambda: NS(chat=NS(completions=NS(create=create))))


# ---------------------------------------------------------------- 1. 보조 호출은 생각하지 않는다

def test_helper_calls_never_think_but_the_agent_call_follows_think_on(monkeypatch):
    monkeypatch.setitem(llm.NO_THINK["chat_template_kwargs"], "enable_thinking", True)    # /think on
    monkeypatch.setitem(llm.PROVIDER, "thinking_param", True)
    sent = []

    def create(**kw):
        sent.append(kw)
        return _reply("ok")
    _fake_client(monkeypatch, create)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "done"}]
    cmp.summarize(msgs)
    llm.chat(msgs, tools=[{"type": "function", "function": {"name": "read_file"}}])
    assert sent[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False      # 요약
    assert sent[1]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True       # 에이전트
    assert llm.thinking() is True                                                         # 전역 설정은 그대로


def test_empty_content_falls_back_to_reasoning_for_tool_free_calls(monkeypatch):
    _fake_client(monkeypatch, lambda **kw: _reply(None, reasoning="요약: m.py 의 double 을 고쳤다"))
    summary = cmp.summarize([{"role": "system", "content": "s"}, {"role": "user", "content": "goal"},
                             {"role": "assistant", "content": "a"}])
    assert "요약: m.py 의 double 을 고쳤다" in summary and "(요약 없음)" not in summary


# ---------------------------------------------------------------- 2. 호출 오류: 분류하고 고칠 방법을

def _failing(monkeypatch, exc):
    tries, sleeps = [], []

    def create(**kw):
        tries.append(1)
        raise exc
    _fake_client(monkeypatch, create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError) as ei:
        llm.chat([{"role": "user", "content": "hi"}])
    return ei.value, tries, sleeps


def test_rejected_key_is_not_retried_and_says_how_to_fix(monkeypatch):
    monkeypatch.setitem(llm.PROVIDER, "name", "kau")
    monkeypatch.setitem(llm.PROVIDER, "env_key", "KAU_API_KEY")
    err, tries, sleeps = _failing(monkeypatch, RuntimeError("Error code: 401 - {'error': 'Invalid API key'}"))
    assert len(tries) == 1 and not sleeps
    assert "the key was rejected" in str(err) and "cap setup" in str(err) and "KAU_API_KEY" in str(err)
    assert err.kind == "auth" and "/continue" not in llm.error_hint(err)


def test_unknown_model_lists_available_models(monkeypatch):
    from cap import setup
    monkeypatch.setattr(setup, "list_models", lambda timeout=10.0: ["qwen3:8b", "llama3.2"])
    err, tries, sleeps = _failing(monkeypatch, RuntimeError("Error code: 404 - The model `nope` does not exist"))
    assert len(tries) == 1 and not sleeps
    assert err.kind == "model_not_found" and "qwen3:8b" in str(err) and "llama3.2" in str(err)
    assert "/continue" not in llm.error_hint(err)


def test_connection_refused_on_a_local_provider_asks_if_it_is_running(monkeypatch):
    import httpx
    import openai
    monkeypatch.setitem(llm.PROVIDER, "name", "ollama")
    monkeypatch.setattr(llm, "BASE_URL", "http://localhost:11434/v1")
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"))
    err, tries, sleeps = _failing(monkeypatch, exc)
    assert len(tries) == 1 and not sleeps
    assert err.kind == "unreachable" and "is Ollama running?" in str(err)


def test_classify_error_kinds():
    assert llm.classify_error("Error code: 400 - This model's maximum context length is 16384 tokens") == "context"
    assert llm.classify_error("Error code: 429 - Too Many Requests") == "rate_limited"
    assert llm.classify_error("Error code: 403 - Forbidden") == "auth"
    assert llm.classify_error("Error code: 404 - model_not_found") == "model_not_found"
    assert llm.classify_error("Connection error.") == "unreachable"
    assert llm.classify_error("HTTP 502") == "other"
    assert "/continue" in llm.error_hint(RuntimeError("HTTP 502"))


# ---------------------------------------------------------------- 3. 같은 문장을 되풀이하면 끊는다

def test_streaming_stops_when_the_same_sentence_repeats(monkeypatch):
    sentence = "So the value of x must be 3, let me check that again. "
    chunks = [sentence[i:i + 9] for i in range(0, len(sentence), 9)] * 60
    sent = {"n": 0, "closed": False}

    class Stream:
        def __iter__(self):
            for c in chunks:
                sent["n"] += 1
                yield NS(usage=None, choices=[NS(delta=NS(content=c, tool_calls=None), finish_reason=None)])

        def close(self):
            sent["closed"] = True

    _fake_client(monkeypatch, lambda **kw: Stream())
    r = llm.chat([{"role": "user", "content": "x?"}], tools=[{"type": "function", "function": {"name": "run"}}],
                 on_text=lambda t: None, stop_on=llm.repeating)
    assert r["finish_reason"] == "repetition" and sent["closed"]
    assert sent["n"] < len(chunks) // 10 and r["content"].count("let me check that again") == 3


def test_loop_keeps_only_the_head_of_a_repeating_reply_and_tells_the_model(ws, monkeypatch):
    long = "The answer is computed again from the start here. " * 80
    replies = iter([{"content": long, "finish": "repetition"},
                    {"tool_calls": [call("read_file", path="m.py")]},
                    {"content": "끝."}])
    seen, kws = [], []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        kws.append(kw)
        r = next(replies)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []),
                "finish_reason": r.get("finish", "stop"), "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, approval="auto")
    s.run_turn("x 계산해줘", ask=lambda p: "y")
    assert kws[0].get("stop_on") is llm.repeating and callable(kws[0].get("on_text"))     # 화면 없이도 스트리밍
    second = seen[1]
    assert second[-2]["role"] == "assistant" and len(second[-2]["content"]) <= 800
    assert "repeating the same reasoning" in second[-1]["content"] and "python -c" in second[-1]["content"]
    s.close()


# ---------------------------------------------------------------- 4. 매 턴 프로젝트 사실을 다시 읽는다

def _scripted(monkeypatch, replies):
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = next(it)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


def test_tests_created_mid_session_turn_on_the_verify_gate(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    test = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
            "from m import double\n\ndef test_double():\n    assert double(3) == 6\n")
    seen = _scripted(monkeypatch, [
        {"tool_calls": [call("write_file", path="tests/test_a.py", content=test)]},
        {"content": "테스트를 만들었습니다."},
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x * 2", new="return x + x")]},
        {"content": "고쳤습니다."}])
    s, _ = session(tmp_path, approval="auto")
    assert s.test_cmd == ""
    r1 = s.run_turn("테스트 만들어줘", ask=lambda p: "y")
    assert "no test command" in r1["values"]["verify_note"]
    assert s.test_cmd == "python -m pytest -q"                        # 턴이 끝나면 다시 읽는다(/goal 관문도 본다)
    r2 = s.run_turn("double 을 덧셈으로", ask=lambda p: "y")
    assert "harness ran" in r2["values"]["verify_note"]
    user = [m for m in seen[2] if m["role"] == "user"][-1]["content"]
    assert user.startswith("[Project facts changed] tests: python -m pytest -q")
    assert "Verify: python -m pytest -q" in seen[2][0]["content"]    # 시스템 글도 지금 것으로
    s.close()


def test_agents_md_written_between_turns_reaches_the_system_message(ws, monkeypatch):
    seen = _scripted(monkeypatch, [{"content": "안녕하세요."}, {"content": "네."}, {"content": "또."}])
    s, _ = session(ws)
    s.run_turn("안녕")
    assert "XYZZY-RULE" not in seen[0][0]["content"]
    (ws / "AGENTS.md").write_text("# Rules\nAlways mention XYZZY-RULE.\n", encoding="utf-8")
    s.run_turn("다음")
    assert "XYZZY-RULE" in seen[1][0]["content"]
    assert "instructions updated" in seen[1][-1]["content"]
    s.run_turn("또")                                                  # 바뀐 것이 없으면 다시 알리지 않는다
    assert "Project facts changed" not in seen[2][-1]["content"] and "XYZZY-RULE" in seen[2][0]["content"]
    s.close()


# ---------------------------------------------------------------- 5. MCP: 큰 스키마 줄이기, 긴 결과는 파일로

def _big_schema():
    deep = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for i in range(5):
        deep = {"type": "object", "title": f"Level{i}", "description": "nested " * 40,
                "properties": {f"child{i}": deep, "note": {"type": "string", "description": "d" * 300}}}
    props = {f"field_{i}": {"type": "string", "title": f"Field {i}", "description": f"Field {i}: " + "words " * 60,
                            "examples": ["a" * 50, "b" * 50]} for i in range(12)}
    props["config"] = deep
    props["ref"] = {"$ref": "#/$defs/Used"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "title": "Big",
            "properties": props, "required": ["field_0", "config"],
            "$defs": {"Used": {"type": "object", "properties": {"a": {"type": "integer"}}},
                      "Unused": {"type": "object", "description": "x" * 2000}}}


def test_big_mcp_schema_is_compacted_keeping_names_and_required():
    big = _big_schema()
    assert len(json.dumps(big).encode()) > 2000
    out = MC.compact_schema(big)
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 2000
    assert out["required"] == ["field_0", "config"] and set(out["properties"]) == set(big["properties"])
    assert "$schema" not in out and "title" not in out and "Unused" not in json.dumps(out)


def test_first_compaction_step_keeps_short_descriptions_and_folds_deep_objects():
    out = MC._shrink(_big_schema(), 1, 80, 3)
    MC._prune_defs(out)
    assert all(len(p.get("description", "")) <= 80 for p in out["properties"].values())
    assert "examples" not in out["properties"]["field_1"]
    lvl2 = out["properties"]["config"]                      # 2단
    lvl3 = lvl2["properties"]["child4"]                     # 3단
    assert lvl3["properties"]["child3"] == {"type": "object"}     # 4단부터 접힘
    assert list(out["$defs"]) == ["Used"]


def test_small_mcp_schema_is_unchanged():
    small = {"type": "object", "title": "T", "properties": {"q": {"type": "string", "description": "query"}},
             "required": ["q"]}
    assert MC.compact_schema(small) == small


class BigOutputMCP:
    def __init__(self, ws):
        self.ws, self.configs, self.foreign, self.untrusted, self.errors = ws, {"dump": {}}, {}, False, {}
        self.resources, self.last_images = {}, []
        self.tools = {"mcp__dump__all": {"server": "dump", "tool": "all", "schema": {"type": "function", "function": {
            "name": "mcp__dump__all", "description": "[MCP dump] Dump everything.",
            "parameters": {"type": "object", "properties": {}}}}}}

    def schemas(self):
        return [t["schema"] for t in self.tools.values()]

    def resource_schemas(self):
        return []

    def call(self, name, args):
        body = "\n".join(f"row {i:05d} " + "." * 40 for i in range(400))
        return True, body + "\nTHE-LAST-LINE"

    def describe(self):
        return []

    def close(self):
        pass


def test_big_mcp_result_is_spilled_keeping_head_and_tail(ws, monkeypatch):
    monkeypatch.setattr(MC, "MCPManager", BigOutputMCP)
    seen = _scripted(monkeypatch, [{"tool_calls": [{"id": "c_dump", "type": "function",
                                                     "function": {"name": "mcp__dump__all", "arguments": "{}"}}]},
                                   {"content": "다 봤습니다."}])
    s, _ = session(ws, approval="bypass")
    s.run_turn("덤프해줘", ask=lambda p: "y")
    tool_msg = next(m for m in seen[1] if m["role"] == "tool")["content"]
    assert ".cap/outputs/" in tool_msg and "THE-LAST-LINE" in tool_msg and "row 00000" in tool_msg
    assert len(tool_msg) < 8000
    saved = list((ws / ".cap" / "outputs").glob("out-*.txt"))
    assert saved and "row 00200" in saved[0].read_text(encoding="utf-8")
    s.close()
