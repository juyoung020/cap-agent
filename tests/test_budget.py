from cap import llm


def test_fits_untouched():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out, mt, acts = llm.fit_budget(msgs, None, 1024)
    assert out == msgs and mt == 1024 and not acts


def test_trims_old_tool_outputs_first():
    big = "x" * 30000  # ≈ 10k 토큰
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(4):
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
    out, mt, acts = llm.fit_budget(msgs, None, 2048)
    assert llm.est_messages(out) + mt <= llm.CONTEXT_WINDOW - llm.SAFETY_MARGIN
    assert any(a.startswith("trim_tool_output") for a in acts)
    assert out[0]["role"] == "system" and out[1]["content"] == "task"


def test_drops_turns_when_trimming_is_not_enough():
    big = "y" * 60000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"},
            {"role": "assistant", "content": big}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go2"}]
    out, mt, acts = llm.fit_budget(msgs, None, 2048)
    assert llm.est_messages(out) + mt <= llm.CONTEXT_WINDOW - llm.SAFETY_MARGIN
    assert "drop_old_turn" in acts


def test_korean_estimate_is_conservative():
    assert llm.est_text("가" * 100) >= 100


def test_leaked_tool_call_recovered():
    txt = 'ok <tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    calls = llm.parse_leaked_tool_calls(txt)
    assert calls[0]["function"]["name"] == "read_file"
    assert '"path": "a.py"' in calls[0]["function"]["arguments"]
