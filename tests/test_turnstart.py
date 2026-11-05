"""턴 시작(기준선 테스트)과 맥락 압축: 거짓 'PASSED before' 알림, 격리 안의 기준선·/goal 관문,
공급자 창 크기를 따르는 압축 기준, 요약 전 도구 출력 줄이기. API 호출 없음."""

import json
from pathlib import Path

from test_chat import call, session, ws  # noqa: F401

from cap import chat as C
from cap import compact as cmp
from cap import llm
from cap import loop as L
from cap import sandbox as sbx
from cap import tools as T
from cap.store import Shadow

OLD = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
       "from m import double\n\ndef test_old():\n    assert double(1) == 3\n")          # 원래부터 실패
NEW = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
       "from m import triple\n\ndef test_triple():\n    assert triple(2) == 6\n")       # 턴 전에는 import 오류


def stub(monkeypatch, replies, default=None, on_call=None):
    """대본 모델. 대본이 끝나면 default 를 계속 돌려준다. on_call(messages, kw) 가 답을 돌려주면 그것을 쓴다."""
    it = iter(replies)
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = on_call(messages, kw) if on_call else None
        if r is None:
            r = next(it, None) or default or {"content": "끝."}
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    return seen


def collection_ws(tmp_path):
    (tmp_path / "m.py").write_text("def double(x):\n    return 2 * x\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_old.py").write_text(OLD, encoding="utf-8")
    (tmp_path / "tests" / "test_new.py").write_text(NEW, encoding="utf-8")
    return tmp_path


def tool_after(seen, name):
    """name 도구 결과가 처음 보이는 모델 호출에서 그 결과 내용."""
    for msgs in seen:
        ids = {tc["id"] for m in msgs if m.get("tool_calls") for tc in m["tool_calls"] if tc["function"]["name"] == name}
        hit = [m["content"] for m in msgs if m["role"] == "tool" and m["tool_call_id"] in ids]
        if hit:
            return hit[-1]
    raise AssertionError(f"no {name} result")


# ---------------------------------------------------------------- 1. 기준선과 거짓 'PASSED before'

def test_failing_tests_normalises_backslash_paths():
    out = ("FAILED tests\\test_a.py::test_x[a\\b] - assert 1\nERROR tests\\sub\\test_b.py\n"
           "FAILED tests/test_c.py::test_y\n")
    assert cmp.failing_tests(out, 1) == ["tests/sub/test_b.py", "tests/test_a.py::test_x[a\\b]",
                                         "tests/test_c.py::test_y"]


def test_collection_error_does_not_hide_old_failure(tmp_path, monkeypatch):
    ws = collection_ws(tmp_path)
    seen = stub(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="    return 2 * x\n",
                             new="    return 2 * x\n\n\ndef triple(x):\n    return 3 * x\n")]},
        {"tool_calls": [call("run_tests")]}])
    s, _ = session(ws, approval="auto")
    s.run_turn("triple 을 만들어줘", ask=lambda p: "abort")
    after = tool_after(seen, "run_tests")
    assert "test_old" in after and "PASSED before this request" not in after     # test_old 는 원래 실패
    base = [r for r in s.ledger.read() if r["type"] == "baseline"]
    assert base and base[0]["complete"] is False
    assert "tests/test_new.py" in base[0]["failing"] and "tests/test_old.py::test_old" in base[0]["failing"]
    assert not list((ws / ".cap").glob("base-*"))                                # 기준선 사본은 지움
    s.close()


def test_incomplete_baseline_note_says_state_unknown(tmp_path, monkeypatch):
    ws = collection_ws(tmp_path)
    seen = stub(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="    return 2 * x\n",
                             new="    return 2 * x\n\n\ndef triple(x):\n    return 4 * x\n")]},   # 틀린 구현
        {"tool_calls": [call("run_tests")]}])
    s, _ = session(ws, approval="auto")
    s.run_turn("triple 을 만들어줘", ask=lambda p: "abort")
    after = tool_after(seen, "run_tests")
    assert "test_triple" in after and "PASSED before this request" not in after
    assert "could not be checked" in after
    s.close()


# ---------------------------------------------------------------- 2. 기준선·/goal 관문도 격리 안에서

def fake_shell(calls, code=1, out="FAILED tests/test_m.py::test_double - assert 1\n1 failed in 0.1s\n"):
    def run_shell(cmd, cwd, timeout=180, sandbox="", pyws=None):
        calls.append({"cmd": cmd, "cwd": str(cwd), "sandbox": sandbox})
        return code, out
    return run_shell


def baseline_sandbox(ws, monkeypatch, approval):
    calls = []
    monkeypatch.setattr(sbx, "backend", lambda mode=None: "appcontainer")
    monkeypatch.setattr(T, "run_shell", fake_shell(calls))       # ToolBox 와 기준선(compact)이 쓰는 것
    monkeypatch.setattr(L, "run_shell", fake_shell(calls))       # 하네스 검증 관문
    stub(monkeypatch, [
        {"tool_calls": [call("edit_file", path="m.py", old="return x + x + 1", new="return x + x")]},
        {"tool_calls": [call("run_tests")]}])
    s, _ = session(ws, approval=approval)
    s.run_turn("double 고쳐줘", ask=lambda p: "abort")
    s.close()
    base = [c for c in calls if "base-" in c["cwd"]]
    assert base, calls
    for c in base:
        assert (ws / ".cap").resolve() in Path(c["cwd"]).resolve().parents
        assert "--continue-on-collection-errors" in c["cmd"]
    return {c["sandbox"] for c in base}


def test_baseline_runs_in_sandbox(ws, monkeypatch):
    assert baseline_sandbox(ws, monkeypatch, "auto") == {"appcontainer"}


def test_baseline_bypass_has_no_sandbox(ws, monkeypatch):
    assert baseline_sandbox(ws, monkeypatch, "bypass") == {""}


def test_baseline_failures_copy_lives_under_workspace(ws, monkeypatch):
    sha = Shadow(ws).snapshot("t")
    calls = []
    monkeypatch.setattr(T, "run_shell", fake_shell(calls))
    fails, complete = cmp.baseline_failures(ws, sha, "python -m pytest -q", sandbox="appcontainer")
    assert fails == ["tests/test_m.py::test_double"] and complete
    assert calls[0]["sandbox"] == "appcontainer" and ".cap" in calls[0]["cwd"]
    assert not list((ws / ".cap").glob("base-*"))
    assert cmp.baseline_failures(ws, "", "python -m pytest -q") == ([], False)   # 스냅샷 없음 = 확인 못 함


def goal_gate_sandbox(ws, monkeypatch, approval):
    calls = []
    monkeypatch.setattr(sbx, "backend", lambda mode=None: "appcontainer")
    monkeypatch.setattr(C, "run_shell", fake_shell(calls, code=0, out="1 passed"))
    monkeypatch.setattr(C.llm, "chat_json", lambda *a, **k: {"verdict": "done", "reason": "ok"})
    stub(monkeypatch, [], default={"content": "다 했습니다."})
    s, _ = session(ws, approval=approval)
    r = s.run_goal("설명만 해줘", ask=lambda p: "y")
    s.close()
    assert r.startswith("goal done"), r
    return {c["sandbox"] for c in calls}


def test_goal_gate_runs_in_sandbox(ws, monkeypatch):
    assert goal_gate_sandbox(ws, monkeypatch, "auto") == {"appcontainer"}


def test_goal_gate_bypass_has_no_sandbox(ws, monkeypatch):
    assert goal_gate_sandbox(ws, monkeypatch, "bypass") == {""}


# ---------------------------------------------------------------- 3. 압축 기준은 공급자의 창 크기를 따른다

def test_compact_at_follows_provider(monkeypatch):
    for k in ("BASE_URL", "MODEL", "CONTEXT_WINDOW", "_client", "_key"):
        monkeypatch.setattr(llm, k, getattr(llm, k))
    monkeypatch.setattr(llm, "PROVIDER", dict(llm.PROVIDER))
    monkeypatch.setattr(llm, "CALIB", dict(llm.CALIB))
    monkeypatch.setattr(cmp, "COMPACT_AT", None)
    llm.use_provider("anthropic")
    assert cmp.compact_at() == 100000
    monkeypatch.setattr(cmp, "COMPACT_AT", 50)                     # 시험용 덮어쓰기는 그대로 된다
    assert cmp.compact_at() == 50


def test_context_command_shows_current_threshold(ws, monkeypatch):
    monkeypatch.setattr(cmp, "COMPACT_AT", None)
    monkeypatch.setattr(llm, "CONTEXT_WINDOW", 200000)
    s, _ = session(ws)
    assert any("Auto-compacts at ~100,000 tokens" in ln for ln in s.context_lines())
    s.close()


# ---------------------------------------------------------------- 4. 요약 전에 이번 턴의 오래된 출력부터 줄인다

def tc(name, k, **args):
    return {"id": f"c{k}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def turn_msgs():
    big = "\n".join(f"{i}| line {i} " + "x" * 40 for i in range(1, 120))
    run_out = "[exit 1]\n" + "\n".join(f"row {i}" for i in range(400)) + "\n1 failed, 2 passed in 0.5s"
    fail = "old 텍스트를 찾지 못했다. " + "가장 비슷한 줄: " + "y" * 600
    rounds = [
        (tc("read_file", 1, path="a.py"), f"<data source=\"read_file\">\n[a.py 줄 1-119 / 전체 119]\n{big}\n</data>"),
        (tc("run", 2, cmd="python -m pytest -q"), f"<data source=\"run\">\n{run_out}\n</data>"),
        (tc("edit_file", 3, path="a.py", old="zzz", new="q"), fail),
        (tc("search", 4, pattern="line 1"), "<data source=\"search\">\n" + "\n".join(f"a.py:{i}: line" + "z" * 30 for i in range(40)) + "\n</data>"),
        (tc("read_file", 5, path="b.py"), f"<data source=\"read_file\">\n[b.py 줄 1-119 / 전체 119]\n{big}\n</data>"),
        (tc("run_tests", 6), f"<data source=\"run_tests\">\n[python -m pytest -q → exit 1]\n{run_out}\n</data>"),
    ]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "고쳐줘"}]
    for call_, out in rounds:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call_]})
        msgs.append({"role": "tool", "tool_call_id": call_["id"], "content": out})
    return msgs


def test_prune_turn_shortens_old_outputs_only():
    msgs = turn_msgs()
    new = cmp.prune_turn(msgs, 1, keep_rounds=2)
    assert cmp.normalize(new) == new and len(new) == len(msgs)
    assert [m.get("tool_calls") for m in new] == [m.get("tool_calls") for m in msgs]   # 인자 그대로
    assert new[-4:] == msgs[-4:]                                                     # 마지막 2묶음 그대로
    read, run, edit, search = new[3]["content"], new[5]["content"], new[7]["content"], new[9]["content"]
    assert read.startswith("<data source=\"read_file\">\n[pruned] read_file a.py lines 1-119 of 119") and read.endswith("</data>")
    assert "read it again" in read and len(read) < 300
    assert "`python -m pytest -q` → exit 1" in run and "1 failed, 2 passed" in run and run.endswith("</data>")
    assert edit.startswith(msgs[7]["content"][:300]) and len(edit) < len(msgs[7]["content"])   # 실패는 앞부분 남김
    assert "search `line 1`" in search and "40 matching lines" in search
    assert cmp.prune_turn(msgs, len(msgs) - 5) == msgs                               # 이번 턴에 2묶음뿐


def test_prune_turn_successful_edit_is_one_line():
    msgs = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [tc("edit_file", 1, path="a.py", old="a", new="b")]},
            {"role": "tool", "tool_call_id": "c1", "content": "편집 적용(exact)\n" + "lint " * 200}]
    msgs += [{"role": "assistant", "content": "", "tool_calls": [tc("todo", k, items=[])]} for k in (2, 3)]
    msgs.insert(4, {"role": "tool", "tool_call_id": "c2", "content": "ok"})
    msgs.append({"role": "tool", "tool_call_id": "c3", "content": "ok"})
    new = cmp.prune_turn(msgs, 0)
    assert new[2]["content"] == "[pruned] edit_file a.py: applied — 편집 적용(exact)"


def midturn_session(ws, monkeypatch, summary, enough=True):
    """read_file·run 6묶음(출력이 큼) 뒤 작은 호출 하나. 그 다음 단계 직전에 창을 줄여 이번 턴 압축이 돌게 한다.
    enough: 기준을 (줄인 뒤 추정)과 (줄이기 전 추정)의 가운데에 둔다. 아니면 줄인 뒤 추정보다 낮게."""
    monkeypatch.setattr(llm, "CONTEXT_WINDOW", 10_000_000)       # 그 전에는 압축이 돌지 않게
    for k in range(4):
        (ws / f"big{k}.py").write_text("\n".join(f"value_{k}_{i} = {i}  # " + "z" * 40 for i in range(400)),
                                       encoding="utf-8")
    script = [{"tool_calls": [call("read_file", path=f"big{k}.py")]} for k in range(4)]
    script += [{"tool_calls": [call("run", cmd=f"python -c \"for i in range(900): print('row', i, {k})\"")]}
               for k in range(2)]
    script += [{"tool_calls": [call("read_file", path="m.py")]}]
    calls = {"agent": 0, "compact": 0}
    agent_seen = []

    def on_call(messages, kw):
        if kw.get("tag") == "compact_midturn":
            calls["compact"] += 1
            if summary is None:
                raise AssertionError("pruning alone should have been enough")
            return {"content": summary}
        if kw.get("tag") != "agent":
            return {"content": "끝."}
        calls["agent"] += 1
        agent_seen.append([dict(m) for m in messages])
        if calls["agent"] == 7:            # 6묶음이 다 보임 → 다음 단계(작은 묶음 하나 더 붙음)에서 압축되게 창을 정한다
            start = next(i for i, m in enumerate(messages) if m["role"] == "user")
            full = llm.est_messages(messages, kw.get("tools"))
            pruned = llm.est_messages(cmp.prune_turn(messages, start, keep_rounds=1), kw.get("tools"))
            limit = (pruned + full) / 2 if enough else pruned * 0.8
            monkeypatch.setattr(llm, "CONTEXT_WINDOW", int(limit / L.MIDTURN_AT))
        if calls["agent"] <= len(script):
            return script[calls["agent"] - 1]
        return {"content": "끝."}
    stub(monkeypatch, [], on_call=on_call)
    s, _ = session(ws, approval="bypass")
    r = s.run_turn("큰 파일들을 살펴봐")
    s.close()
    assert len(agent_seen) >= 8, len(agent_seen)
    return calls, agent_seen, r, s


def test_midturn_pruning_alone_avoids_summary(ws, monkeypatch):
    calls, seen, r, s = midturn_session(ws, monkeypatch, summary=None)
    assert calls["compact"] == 0
    before, after = seen[6], seen[7]            # 6묶음 + 작은 묶음 하나가 붙은 뒤의 호출이 줄어 있다
    assert cmp.normalize(after) == after
    tools_after = [m for m in after if m["role"] == "tool"]
    assert sum("[pruned]" in m["content"] for m in tools_after) == 5
    assert all(m["content"].startswith("<data source=") for m in tools_after)
    assert "[pruned]" not in tools_after[-2]["content"] and "[pruned]" not in tools_after[-1]["content"]
    assert [m.get("tool_calls") for m in after if m["role"] == "assistant"][:6] == \
        [m.get("tool_calls") for m in before if m["role"] == "assistant"][:6]
    log = [x for x in s.ledger.read() if x["type"] == "compact"]
    assert log and log[0].get("pruned") is True and log[0]["after"] < log[0]["before"]


def test_midturn_summarises_when_pruning_is_not_enough(ws, monkeypatch):
    calls, seen, r, s = midturn_session(ws, monkeypatch, summary="1) read files", enough=False)
    assert calls["compact"] == 1
    assert any("Summary of your earlier steps" in (m.get("content") or "") for m in seen[7])


def test_midturn_empty_summary_keeps_pruned_copy(ws, monkeypatch):
    calls, seen, r, s = midturn_session(ws, monkeypatch, summary="", enough=False)
    assert calls["compact"] == 1
    before, after = seen[6], seen[7]
    assert sum("[pruned]" in (m.get("content") or "") for m in after) == 5
    assert sum(len(m.get("content") or "") for m in after) < sum(len(m.get("content") or "") for m in before)
