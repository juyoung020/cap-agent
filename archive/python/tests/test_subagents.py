"""하위 에이전트(delegate) — 가짜 모델이 대화 내용을 보고 누구의 호출인지 판단해 답한다(병렬이라 순서 대본을 못 씀)."""

import json
import threading
import time

from cap import agents as sub
from cap import loop as L
from test_chat import BUGGY, call, session, ws  # noqa: F401  (ws 는 fixture)


def router(monkeypatch, parent_replies, child_fn, delay=0.0):
    """parent_replies: 부모 호출 차례대로. child_fn(role, task, n번째 호출, messages) → 답."""
    it = iter(parent_replies)
    counts: dict[str, int] = {}
    log = {"parent": [], "child": [], "active": 0, "max_active": 0}
    lock = threading.Lock()

    def fake_chat(messages, **kw):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        if "focused subagent" in system:
            role = system.split("(role: ", 1)[1].split(")", 1)[0]
            task = next(m["content"] for m in messages if m["role"] == "user")
            with lock:
                counts[task] = counts.get(task, 0) + 1
                n = counts[task]
                log["active"] += 1
                log["max_active"] = max(log["max_active"], log["active"])
            time.sleep(delay)
            with lock:
                log["active"] -= 1
            log["child"].append((role, task, [dict(m) for m in messages]))
            r = child_fn(role, task, n, messages)
        else:
            log["parent"].append([dict(m) for m in messages])
            r = next(it)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return log


def test_two_explorers_run_in_parallel_and_return_summaries(ws, monkeypatch):
    def child(role, task, n, msgs):
        if n == 1:
            return {"tool_calls": [call("read_file", path="m.py" if "double" in task else "tests/test_m.py")]}
        return {"content": f"- 요약: {task[:20]} 조사 끝"}
    log = router(monkeypatch, [
        {"tool_calls": [call("delegate", tasks=[{"role": "explore", "task": "double 함수가 하는 일"},
                                                {"role": "explore", "task": "테스트가 확인하는 것"}])]},
        {"content": "두 하위 에이전트 결과를 합쳤습니다."}], child, delay=0.4)
    s, out = session(ws, "ask")
    r = s.run_turn("병렬로 조사해줘")
    assert s.last_answer(r["values"]) == "두 하위 에이전트 결과를 합쳤습니다."
    tool_msg = [m for m in log["parent"][-1] if m["role"] == "tool"][-1]["content"]
    assert "[explore-1 · explore · done" in tool_msg and "[explore-2 · explore · done" in tool_msg
    assert "double 함수가 하는 일" in tool_msg or "조사 끝" in tool_msg
    assert log["max_active"] == 2                                           # 동시에 둘
    assert not any(m["role"] == "tool" and "1| def double" in m["content"] for m in log["parent"][-1])   # 중간 과정은 안 옴
    assert any("explore-1 done" in ln for ln in out)
    s.close()


def test_explorer_cannot_edit(ws, monkeypatch):
    def child(role, task, n, msgs):
        if n == 1:
            return {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]}
        return {"content": "편집이 막혔습니다."}
    router(monkeypatch, [{"tool_calls": [call("delegate", tasks=[{"role": "explore", "task": "고쳐 봐"}])]},
                         {"content": "끝"}], child)
    s, _ = session(ws, "auto")
    s.run_turn("조사")
    assert (ws / "m.py").read_text() == BUGGY
    s.close()


def test_worker_edits_only_its_files_and_parent_verifies(ws, monkeypatch):
    (ws / "other.py").write_text("y = 1\n", encoding="utf-8")

    def child(role, task, n, msgs):
        if n == 1:
            return {"tool_calls": [call("edit_file", path="other.py", old="y = 1", new="y = 2")]}   # 범위 밖
        if n == 2:
            return {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]}
        return {"content": "- m.py 고침\n- other.py 는 범위 밖이라 못 고침"}
    log = router(monkeypatch, [
        {"tool_calls": [call("delegate", tasks=[{"role": "worker", "task": "double 버그 고쳐", "files": ["m.py"]}])]},
        {"content": "하위 에이전트가 고쳤고 테스트가 통과합니다."}], child)
    s, _ = session(ws, "auto")
    r = s.run_turn("double 고쳐줘")
    v = r["values"]
    assert (ws / "m.py").read_text() == "def double(x):\n    return x + x\n" and (ws / "other.py").read_text() == "y = 1\n"
    assert "m.py" in v["turn_changed"] and v["verify_note"].startswith("harness ran")    # 부모 관문이 확인
    child_tool = [m["content"] for _, _, ms in log["child"] for m in ms if m["role"] == "tool"]
    assert any("outside your assigned files" in c for c in child_tool)
    assert "Files changed: m.py" in [m for m in log["parent"][-1] if m["role"] == "tool"][-1]["content"]
    s.close()


def test_worker_approval_goes_to_the_human_with_its_name(ws, monkeypatch):
    def child(role, task, n, msgs):
        if n == 1:
            return {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]}
        return {"content": "끝"}
    router(monkeypatch, [{"tool_calls": [call("delegate", tasks=[{"role": "worker", "task": "고쳐", "files": ["m.py"]}])]},
                         {"content": "완료"}], child)
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("고쳐", ask=lambda p: asked.append(p) or "n")
    assert asked and asked[0].get("agent") == "worker-1" and asked[0]["calls"][0]["tool"] == "edit_file"
    assert (ws / "m.py").read_text() == BUGGY
    s.close()


def test_delegate_validation():
    from pathlib import Path
    ws = Path(".").resolve()
    assert sub.validate_tasks([], ws, "ask", 0)[1]
    assert "worker" in sub.validate_tasks([{"role": "worker", "task": "x"}], ws, "ask", 0)[1]          # files 필요
    assert "disjoint" in sub.validate_tasks([{"role": "worker", "task": "a", "files": ["a.py"]},
                                             {"role": "worker", "task": "b", "files": ["a.py"]}], ws, "ask", 0)[1]
    assert "plan mode" in sub.validate_tasks([{"role": "worker", "task": "a", "files": ["a.py"]}], ws, "plan", 0)[1]
    assert "limit" in sub.validate_tasks([{"role": "explore", "task": "a"}], ws, "ask", sub.MAX_CHILDREN_PER_TURN)[1]
    ok, err = sub.validate_tasks([{"role": "explore", "task": "a"}], ws, "plan", 0)
    assert ok and not err                                                          # 플랜 모드에서도 조사는 된다


def test_subagents_have_no_delegate_tool(ws, monkeypatch):
    seen_tools = []

    def child(role, task, n, msgs):
        return {"content": "끝"}
    log = router(monkeypatch, [{"tool_calls": [call("delegate", tasks=[{"role": "explore", "task": "봐"}])]},
                               {"content": "완료"}], child)
    orig = L.llm.chat

    def spy(messages, **kw):
        seen_tools.append([t["function"]["name"] for t in kw.get("tools") or []])
        return orig(messages, **kw)
    monkeypatch.setattr(L.llm, "chat", spy)
    s, _ = session(ws, "ask")
    s.run_turn("봐")
    assert any("delegate" in t for t in seen_tools) and any(t and "delegate" not in t for t in seen_tools)
    s.close()


def test_parallel_reads_in_one_step(ws, monkeypatch):
    from test_chat import script
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="m.py"), call("read_file", path="tests/test_m.py")]},
                                {"content": "둘 다 봤습니다."}])
    s, _ = session(ws, "ask")
    s.run_turn("봐")
    tools = [m["content"] for m in seen[-1] if m["role"] == "tool"]
    assert len(tools) == 2 and "def double" in tools[0] and "test_double" in tools[1]
    s.close()
