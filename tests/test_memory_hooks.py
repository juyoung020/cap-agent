"""기억 · 스킬 · 훅 (가짜 모델 대본, API 호출 없음)."""

import json
import sys

from cap import chat as C
from cap import hooks as H
from cap import memory as M
from test_chat import BUGGY, call, script, session, ws  # noqa: F401  (ws 는 fixture)

PY = sys.executable.replace("\\", "/")


def test_memory_add_replace_remove_and_cap(ws):
    assert M.change_memory(ws, "add", "Tests run with python -m pytest -q")[0]
    assert M.change_memory(ws, "add", "Tests run with python -m pytest -q")[1] == "already remembered"
    assert M.change_memory(ws, "replace", "Tests run with pytest -q -x", old="pytest -q")[0]
    assert M.read_memory(ws, "project") == ["Tests run with pytest -q -x"]
    ok, msg = M.change_memory(ws, "add", "x" * 3000)
    assert not ok and "full" in msg
    assert M.change_memory(ws, "remove", "pytest")[0] and M.read_memory(ws, "project") == []


def test_memory_is_in_the_next_session_prompt_not_this_one(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"tool_calls": [call("remember", action="add", text="The user wants Korean comments")]}, {"content": "기억했습니다."},
        {"content": "네"}])
    s, _ = session(ws, "auto")
    asked = []
    s.run_turn("앞으로 한국어 주석 써", ask=lambda p: asked.append(p) or "y")
    assert asked and asked[0]["calls"][0]["category"] == "memory"          # auto 에서도 사람이 본다
    assert "The user wants Korean comments" not in seen[0][0]["content"]    # 이번 세션 프롬프트는 그대로
    s.close()
    s2, _ = session(ws, "auto")
    s2.run_turn("안녕")
    assert "<MEMORY>" in seen[-1][0]["content"] and "The user wants Korean comments" in seen[-1][0]["content"]
    s2.close()


def test_skills_are_listed_and_opened_on_demand(ws, monkeypatch):
    d = ws / ".cap" / "skills" / "release"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: release\ndescription: How to cut a release\n---\n1. bump version\n2. tag\n",
                                encoding="utf-8")
    skill_call = {"id": "c_skill", "type": "function", "function": {"name": "skill", "arguments": '{"name": "release"}'}}
    seen = script(monkeypatch, [{"tool_calls": [skill_call]}, {"content": "따라 하겠습니다."}])
    s, _ = session(ws, "ask")
    s.run_turn("릴리스 해줘")
    assert "- release: How to cut a release" in seen[0][0]["content"] and "bump version" not in seen[0][0]["content"]
    tool = [m["content"] for m in seen[-1] if m["role"] == "tool"][0]
    assert "<skill name=\"release\">" in tool and "1. bump version" in tool
    s.close()


def _hooks(ws, spec):
    (ws / ".cap").mkdir(exist_ok=True)
    (ws / ".cap" / "hooks.json").write_text(json.dumps({"hooks": spec}), encoding="utf-8")
    H.trust(ws)


def _script_file(ws, name, code):
    p = ws / name
    p.write_text(code, encoding="utf-8")
    return f'"{PY}" "{p.as_posix()}"'


def test_untrusted_project_hooks_do_not_run(ws):
    (ws / ".cap").mkdir(exist_ok=True)
    (ws / ".cap" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "echo x"}]}]}}),
                                            encoding="utf-8")
    h = H.Hooks(ws)
    assert h.untrusted and not h.any("PreToolUse") and "/hooks trust" in h.describe()[-1]
    H.trust(ws)
    assert H.Hooks(ws).any("PreToolUse")


def test_pre_tool_hook_blocks_and_post_tool_hook_gives_feedback(ws, monkeypatch):
    block = _script_file(ws, "block.py", "import sys,json\nd=json.load(sys.stdin)\n"
                                         "if 'rm ' in d['tool_input'].get('cmd',''):\n    print('no deleting here'); sys.exit(2)\n")
    fb = _script_file(ws, "fb.py", "import sys\nprint('remember to update CHANGELOG'); sys.exit(2)\n")
    _hooks(ws, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": block}]}],
                "PostToolUse": [{"matcher": "edit_file", "hooks": [{"command": fb}]}]})
    seen = script(monkeypatch, [
        {"tool_calls": [call("run", cmd="rm m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "끝"}])
    s, _ = session(ws, "bypass")
    s.run_turn("정리하고 고쳐")
    tools = [m["content"] for m in seen[-1] if m["role"] == "tool"]
    assert (ws / "m.py").exists() and "blocked by a PreToolUse hook: no deleting here" in tools[0]
    assert "[PostToolUse hook feedback] remember to update CHANGELOG" in tools[1]
    s.close()


def test_stop_hook_makes_the_agent_continue_once(ws, monkeypatch):
    stop = _script_file(ws, "stop.py", "import sys\nprint('also add a docstring'); sys.exit(2)\n")
    _hooks(ws, {"Stop": [{"hooks": [{"command": stop}]}]})
    seen = script(monkeypatch, [{"content": "다 했습니다."}, {"content": "docstring 도 달았습니다."}])
    s, _ = session(ws, "auto")
    r = s.run_turn("봐줘")
    assert "[Stop hook] also add a docstring" in seen[-1][-1]["content"]
    assert s.last_answer(r["values"]) == "docstring 도 달았습니다."          # 한 번만 계속(무한 반복 없음)
    s.close()


def test_user_prompt_hook_can_block_a_request(ws, monkeypatch):
    gate = _script_file(ws, "gate.py", "import sys,json\nd=json.load(sys.stdin)\n"
                                       "if 'deploy' in d['prompt']:\n    print('deploys are not allowed here'); sys.exit(2)\n")
    _hooks(ws, {"UserPromptSubmit": [{"hooks": [{"command": gate}]}]})
    script(monkeypatch, [])
    s, _ = session(ws, "auto")
    r = s.run_turn("deploy to prod")
    assert r["values"]["status"] == "blocked" and "deploys are not allowed here" in C.turn_summary(s, r)[0]
    s.close()


def test_session_start_hook_adds_context(ws, monkeypatch):
    start = _script_file(ws, "start.py", "print('Today the CI is down; skip slow tests.')\n")
    _hooks(ws, {"SessionStart": [{"hooks": [{"command": start}]}]})
    seen = script(monkeypatch, [{"content": "ok"}])
    s, _ = session(ws, "auto")
    s.run_turn("안녕")
    assert "Today the CI is down" in seen[0][0]["content"]
    s.close()


def test_learn_extracts_facts_into_project_memory(ws, monkeypatch):
    script(monkeypatch, [{"content": "안녕하세요"},
                         {"content": '{"facts": ["Run tests with python -m pytest -q", "m.py holds double()"]}'}])
    s, _ = session(ws, "auto")
    s.run_turn("안녕")
    out = s.learn()
    assert out[0].startswith("+ Run tests") and M.read_memory(ws, "project")[1] == "m.py holds double()"
    s.close()


def test_notification_subagent_stop_and_precompact_hooks(ws, monkeypatch):
    rec = _script_file(ws, "rec.py", "import sys, json\nd = json.load(sys.stdin)\n"
                                     "open('events.jsonl', 'a', encoding='utf-8').write(json.dumps(d) + chr(10))\n"
                                     "print('checked by hook' if d['event'] == 'SubagentStop' else '')\n")
    _hooks(ws, {"Notification": [{"hooks": [{"command": rec}]}], "SubagentStop": [{"hooks": [{"command": rec}]}],
                "PreCompact": [{"hooks": [{"command": rec}]}]})
    seen = script(monkeypatch, [{"tool_calls": [call("delegate", tasks=[{"role": "explore", "task": "look at m.py"}])]},
                                {"content": "- m.py has double"},
                                {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
                                {"content": "고쳤습니다."},
                                {"content": "요약"}])
    s, _ = session(ws)                                              # ask 모드: 편집 승인을 기다리며 Notification
    s.run_turn("고쳐줘", ask=lambda p: "y")
    s.compact()
    events = [json.loads(ln) for ln in (ws / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in events]
    assert "Notification" in kinds and "SubagentStop" in kinds and "PreCompact" in kinds
    note = next(e for e in events if e["event"] == "Notification")
    assert note["message"] == "CAP needs your permission"
    sub = next(e for e in events if e["event"] == "SubagentStop")
    assert sub["role"] == "explore" and "m.py has double" in sub["summary"]
    assert next(e for e in events if e["event"] == "PreCompact")["trigger"] == "manual"
    delegate_result = [m for m in seen[2] if m["role"] == "tool"][-1]["content"]
    assert "[SubagentStop hook on explore-1] checked by hook" in delegate_result
    s.close()


def test_context_agents_rename_commands(ws, monkeypatch):
    script(monkeypatch, [{"content": "안녕"}])
    s, out = session(ws)
    s.run_turn("인사")
    C.handle_command(s, ws, "/context")
    assert any(ln.startswith("Context: ~") for ln in out) and any("tool results" in ln for ln in out)
    (ws / ".cap" / "agents").mkdir(parents=True, exist_ok=True)
    (ws / ".cap" / "agents" / "sec.md").write_text("---\ndescription: security\n---\nFind bugs.\n", encoding="utf-8")
    C.handle_command(s, ws, "/agents")
    assert any(ln.strip().startswith("sec") and "security" in ln for ln in out)
    C.handle_command(s, ws, "/rename coupon work")
    assert s.meta["title"] == "coupon work"
    assert json.loads((ws / ".cap" / "sessions" / f"{s.sid}.json").read_text(encoding="utf-8"))["title"] == "coupon work" \
        if (ws / ".cap" / "sessions" / f"{s.sid}.json").exists() else True
    s.close()
