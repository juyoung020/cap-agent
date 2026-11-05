"""코딩 루프 하위 그래프를 가짜 모델(대본)로 돌린다. API 호출 없음."""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from cap import loop as L
from cap.store import Ledger

BUGGY = "def double(x):\n    return x + x + 1\n"
TEST = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from m import double\n\ndef test_double():\n    for i in range(5):\n        assert double(i) == 2 * i\n")


def call(name, **args):
    return {"id": f"c_{name}_{len(json.dumps(args))}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def script(monkeypatch, replies):
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append(messages)
        r = next(it)
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "m.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(TEST, encoding="utf-8")
    (tmp_path / ".cap").mkdir()
    return tmp_path


def build(ws, **kw):
    return L.build_loop(ws=ws, ledger=Ledger(ws, "t"), test_cmd="python -m pytest -q -p no:cacheprovider",
                        frozen={"tests/test_m.py"}, preapproved=set(), decisions=[], printer=lambda s: None,
                        checkpointer=InMemorySaver(), **kw)


def run(app, inp, tid="1"):
    cfg = {"configurable": {"thread_id": tid}}
    app.invoke(inp, cfg)
    st = app.get_state(cfg)
    return st, [i.value for t in st.tasks for i in t.interrupts], cfg


def test_verify_gate_runs_tests_when_model_skips_them(ws, monkeypatch):
    script(monkeypatch, [
        {"tool_calls": [call("read_file", path="m.py")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "고쳤습니다."},          # 테스트 없이 완료 선언
    ])
    st, intr, _ = run(build(ws), {"goal": "fix double"})
    assert not intr
    assert st.values["status"] == "done"
    recs = Ledger(ws, "t").read()
    assert any(r["type"] == "test" and r["source"] == "harness" and r["passed"] for r in recs)


def test_verify_gate_feeds_failure_back(ws, monkeypatch):
    seen = script(monkeypatch, [
        {"content": "이미 맞습니다."},        # 아무것도 안 고침 → 하네스가 실패를 돌려줌
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return 2 * x")]},
        {"tool_calls": [call("run_tests")]},
        {"content": "done"},
    ])
    st, intr, _ = run(build(ws), {"goal": "fix double"})
    assert st.values["status"] == "done"
    assert any("FAIL" in m["content"] for m in seen[1] if m["role"] == "user")


def test_frozen_test_edit_denied(ws, monkeypatch):
    script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="tests/test_m.py", old="2 * i", new="2 * i + 1")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "done"},
    ])
    st, _, _ = run(build(ws), {"goal": "fix"})
    assert "2 * i\n" in (ws / "tests" / "test_m.py").read_text()
    assert st.values["status"] == "done"
    assert any(r["type"] == "permission" and r["verdict"] == "deny" for r in Ledger(ws, "t").read())


def test_approval_interrupt_and_resume(ws, monkeypatch):
    (ws / "junk.txt").write_text("x")
    script(monkeypatch, [
        {"tool_calls": [call("run", cmd="rm junk.txt")]},
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "done"},
    ])
    app = build(ws)
    st, intr, cfg = run(app, {"goal": "clean and fix"})
    assert intr and intr[0]["kind"] == "approval"
    assert (ws / "junk.txt").exists()          # 승인 전에는 실행 안 됨
    app.invoke(Command(resume="y"), cfg)
    assert not (ws / "junk.txt").exists()
    assert app.get_state(cfg).values["status"] == "done"


def test_same_failure_three_times_escalates(ws, monkeypatch):
    script(monkeypatch, [{"content": "done"}] * 5)
    app = build(ws)
    st, intr, cfg = run(app, {"goal": "fix"})
    assert intr and intr[0]["kind"] == "escalate"
    app.invoke(Command(resume="abort"), cfg)
    assert app.get_state(cfg).values["status"] == "escalated"


def test_checkpoint_snapshot_recorded(ws, monkeypatch):
    script(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"content": "done"},
    ])
    st, _, _ = run(build(ws), {"goal": "fix"})
    cps = st.values["checkpoints"]
    assert len(cps) >= 2 and cps[-1]["sha"]
