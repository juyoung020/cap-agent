"""성숙한 에이전트와의 차이를 메운 기능들(긴 출력 보관, Claude 형식 훅 결정, 맥락 초과 복구 등)."""
import sys
from pathlib import Path

from cap import tools as T


def test_long_output_is_saved_and_can_be_read_back(tmp_path):
    box = T.ToolBox(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=sk-verysecretvalue123\n", encoding="utf-8")
    script = tmp_path / "big.py"
    script.write_text("import os\nprint(os.environ.get('X','sk-verysecretvalue123'))\n"
                      "for i in range(3000):\n    print('line', i)\n", encoding="utf-8")
    r = box.execute("run", {"cmd": f'"{sys.executable}" big.py'})
    out = r["output"]
    assert "saved to .cap/outputs/" in out and "line 2999" in out and "line 1500" not in out
    rel = out.split("saved to ")[1].split(" ")[0]
    full = (tmp_path / rel).read_text(encoding="utf-8")
    assert "line 1500" in full and "sk-verysecretvalue123" not in full     # 파일에도 비밀은 가림
    got = box.execute("read_file", {"path": rel, "offset": 1500, "limit": 5})
    assert "line 1498" in got["output"] or "line 1499" in got["output"]


def test_short_output_is_not_saved(tmp_path):
    box = T.ToolBox(tmp_path)
    r = box.execute("run", {"cmd": f'"{sys.executable}" -c "print(1)"'})
    assert "saved to" not in r["output"] and not (tmp_path / ".cap" / "outputs").exists()


def test_old_outputs_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "OUTPUT_KEEP", 3)
    for _ in range(6):
        T.spill(tmp_path, "x" * (T.MAX_OUT + 10))
    assert len(list((tmp_path / ".cap" / "outputs").glob("out-*.txt"))) == 3


# ---------------------------------------------------------------- Claude Code 형식 훅 결정(hookSpecificOutput)
from test_chat import call, script, session, ws  # noqa: E402,F401
from test_memory_hooks import _hooks, _script_file  # noqa: E402

CLAUDE_PRE = r'''import sys, json
d = json.load(sys.stdin)
cmd = str(d["tool_input"].get("cmd", ""))
out = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
if cmd.startswith("make"):
    out["hookSpecificOutput"].update(permissionDecision="allow", permissionDecisionReason="make is fine")
elif "push" in cmd:
    out["hookSpecificOutput"].update(permissionDecision="deny", permissionDecisionReason="no pushing")
elif cmd.startswith("echo"):
    out["hookSpecificOutput"].update(permissionDecision="ask", permissionDecisionReason="check echo")
elif cmd.startswith("rm -rf"):
    out["hookSpecificOutput"]["updatedInput"] = {"command": "echo safe"}
    out["systemMessage"] = "rewrote a dangerous command"
print(json.dumps(out))
'''


def test_claude_style_pre_tool_hook_decides_and_rewrites(ws, monkeypatch):
    _hooks(ws, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": _script_file(ws, "pre.py", CLAUDE_PRE)}]}]})
    seen = script(monkeypatch, [{"tool_calls": [call("run", cmd="make build"), call("run", cmd="git push origin main"),
                                                call("run", cmd="rm -rf build")]},
                                {"content": "끝"}])
    s, out = session(ws, "ask")
    asked = []
    s.run_turn("해줘", ask=lambda p: asked.append(p) or "y")
    text = "\n".join(m["content"] for m in seen[1] if m["role"] == "tool")
    assert "blocked by a PreToolUse hook: no pushing" in text
    asked_cmds = [c["args"]["cmd"] for p in asked for c in p["calls"]]
    assert "make build" not in asked_cmds                 # 훅이 허락 → 묻지 않음
    assert "rm -rf build" not in str(asked)                # 고친 입력(echo safe)으로 판정·실행
    assert "safe" in text and any("rewrote a dangerous command" in ln for ln in out)
    s.close()


def test_claude_style_ask_makes_auto_mode_ask(ws, monkeypatch):
    _hooks(ws, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": _script_file(ws, "pre.py", CLAUDE_PRE)}]}]})
    script(monkeypatch, [{"tool_calls": [call("run", cmd="echo hi")]}, {"content": "끝"}])
    s, _ = session(ws, "auto")
    asked = []
    s.run_turn("해줘", ask=lambda p: asked.append(p) or "n")
    assert asked and "check echo" in str(asked)
    s.close()


def test_claude_style_permission_request_behavior(ws, monkeypatch):
    dec = ('import sys, json\nd = json.load(sys.stdin)\nb = "deny" if "push" in d["tool_input"]["cmd"] else "allow"\n'
           'print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", '
           '"decision": {"behavior": b, "message": "policy " + b}}}))\n')
    _hooks(ws, {"PermissionRequest": [{"hooks": [{"command": _script_file(ws, "dec.py", dec)}]}]})
    seen = script(monkeypatch, [{"tool_calls": [call("run", cmd="make build"), call("run", cmd="git push")]},
                                {"content": "끝"}])
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("해줘", ask=lambda p: asked.append(p) or "y")
    text = "\n".join(m["content"] for m in seen[1] if m["role"] == "tool")
    assert not asked and "policy deny" in text
    s.close()


def test_continue_false_blocks_and_tells_the_model_to_stop(ws, monkeypatch):
    stop = 'import json\nprint(json.dumps({"continue": False, "stopReason": "budget exhausted"}))\n'
    _hooks(ws, {"PreToolUse": [{"hooks": [{"command": _script_file(ws, "stop.py", stop)}]}]})
    seen = script(monkeypatch, [{"tool_calls": [call("run", cmd="make build")]}, {"content": "끝"}])
    s, _ = session(ws, "bypass")
    s.run_turn("해줘")
    text = "\n".join(m["content"] for m in seen[1] if m["role"] == "tool")
    assert "budget exhausted" in text and "stop working" in text
    s.close()


# ---------------------------------------------------------------- 맥락 초과 거절 → 추정 보정 + 요약 압축으로 재시도
from types import SimpleNamespace as NS  # noqa: E402

from cap import llm  # noqa: E402


def test_overflow_error_calibrates_from_the_server_count(monkeypatch):
    monkeypatch.setitem(llm.CALIB, "factor", 1.0)
    monkeypatch.setitem(llm.CALIB, "recent", [])
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "x " * 4000}]
    raw = llm.est_messages(msgs)
    llm.learn_from_overflow("This model's maximum context length is 16384 tokens. However, you requested 18000 tokens "
                            f"({raw * 2} in the messages, 1024 in the completion).", msgs, None)
    assert llm.CALIB["factor"] == 1.8          # 서버가 센 수가 추정의 2배 → 상한 1.8 까지 올림
    monkeypatch.setitem(llm.CALIB, "factor", 1.0)
    llm.learn_from_overflow("context length exceeded", msgs, None)
    assert abs(llm.CALIB["factor"] - 1.15) < 1e-9


def test_keep_tail_never_starts_with_an_orphan_tool_result():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal"}]
    for k in range(5):
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{k}", "type": "function",
                                                                       "function": {"name": "run", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"c{k}", "content": "out"}]
    kept = llm.keep_tail(msgs, 3)
    assert kept[1]["content"] == "goal" and kept[2]["role"] != "tool"


def test_chat_retries_with_the_compacted_messages(monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw["messages"])
        if len(calls) == 1:
            raise RuntimeError("Error code: 400 - This model's maximum context length is 16384 tokens")
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")], usage=None)
    monkeypatch.setattr(llm, "client", lambda: NS(chat=NS(completions=NS(create=create))))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}]
    seen = []

    def shrink(m):
        seen.append(len(m))
        return m[:2] + [{"role": "user", "content": "[summary] earlier steps"}]
    r = llm.chat(msgs, on_overflow=shrink)
    assert r["content"] == "ok" and seen == [4]
    assert calls[1][-1]["content"] == "[summary] earlier steps" and calls[1][1]["content"] == "goal"


# ---------------------------------------------------------------- MCP 리소스·프롬프트(실제 MCP 서버를 띄워서)
import json  # noqa: E402

import pytest  # noqa: E402

from cap import mcp_client as MC  # noqa: E402
from cap import chat as C  # noqa: E402

RES_SERVER = '''from mcp.server.mcpserver import MCPServer
app = MCPServer("docs")

@app.tool()
def ping() -> str:
    """Ping."""
    return "pong"

@app.resource("docs://style-guide", name="style-guide", description="Team coding style")
def style() -> str:
    return "Use snake_case. Max line length 100."

@app.prompt(description="Review a file for bugs")
def review(path: str, focus: str = "bugs") -> str:
    return f"Review {path} focusing on {focus}. List concrete problems with line numbers."

if __name__ == "__main__":
    app.run()
'''


@pytest.mark.skipif(not MC.available(), reason="mcp package not installed")
def test_mcp_resources_and_prompts(ws, monkeypatch):
    (ws / "docs_srv.py").write_text(RES_SERVER, encoding="utf-8")
    (ws / ".cap").mkdir(exist_ok=True)
    (ws / ".cap" / "mcp.json").write_text(json.dumps({"mcpServers": {"docs": {
        "command": sys.executable, "args": [str(ws / "docs_srv.py")]}}}), encoding="utf-8")
    MC.trust(ws)
    rc = {"id": "c1", "type": "function", "function": {"name": "read_mcp_resource", "arguments": json.dumps(
        {"server": "docs", "uri": "docs://style-guide"})}}
    lc = {"id": "c0", "type": "function", "function": {"name": "list_mcp_resources", "arguments": "{}"}}
    seen = script(monkeypatch, [{"tool_calls": [lc, rc]}, {"content": "snake_case"}])
    s, out = session(ws, "ask")
    try:
        assert s.mcp.has_resources() and "/mcp__docs__review" in "\n".join(s.mcp.describe())
        asked = []
        s.run_turn("스타일 가이드 알려줘", ask=lambda p: asked.append(p) or "n")
        tools = [m["content"] for m in seen[-1] if m["role"] == "tool"]
        assert not asked                                             # 켜 둔 서버의 데이터 읽기는 묻지 않음
        assert "docs://style-guide" in tools[0] and "snake_case" in tools[1]
        assert tools[1].startswith('<data source="read_mcp_resource">')
        _, quit_, turn = C.handle_command(s, ws, "/mcp__docs__review app.py security")
        assert not quit_ and turn and "Review app.py focusing on security" in turn
        _, _, turn = C.handle_command(s, ws, "/mcp__docs__review")
        assert turn is None and any("needs: <path>" in ln for ln in out)
    finally:
        s.close()


# ---------------------------------------------------------------- cap exec --events(JSONL) · exec resume
def test_exec_events_stream_jsonl(ws, monkeypatch, capsys):
    script(monkeypatch, [{"tool_calls": [call("run", cmd="python -c \"print(7)\"")]},
                         {"content": "7 을 출력했습니다."}])
    code = C.one_shot(ws, "7 출력", approval="bypass", events=True)
    lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    types = [d["type"] for d in lines]
    assert types[0] == "thread.started" and types[1] == "turn.started" and types[-1] in ("turn.completed", "turn.failed")
    cmd = [d["item"] for d in lines if d["type"] == "item.completed" and d["item"]["type"] == "command_execution"]
    assert cmd and "print(7)" in cmd[0]["command"] and cmd[0]["exit_code"] == 0 and "7" in cmd[0]["aggregated_output"]
    msgs = [d["item"]["text"] for d in lines if d["type"] == "item.completed" and d["item"]["type"] == "agent_message"]
    assert "7 을 출력했습니다." in msgs[-1] and lines[-1]["result"]["session"] == lines[0]["thread_id"]
    assert len({d["item"]["id"] for d in lines if d["type"] == "item.completed"}) == len(
        [d for d in lines if d["type"] == "item.completed"])
    assert code in (0, 1)


def test_exec_resume_continues_the_last_session(ws, monkeypatch, capsys):
    seen = script(monkeypatch, [{"content": "첫 답: 비밀번호는 파랑"}, {"content": "아까 말한 건 파랑"}])
    C.one_shot(ws, "색 하나 정해", approval="auto", as_json=True)
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    C.one_shot(ws, "아까 뭐라고 했지?", approval="auto", as_json=True, resume="last")
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["session"] == first["session"]
    assert any("첫 답: 비밀번호는 파랑" in str(m.get("content")) for m in seen[1])     # 지난 대화가 이어짐
    assert C.one_shot(ws, "x", approval="auto", as_json=True, resume="99999999-999999") == 2


def test_cli_parses_exec_resume_and_events(monkeypatch):
    from cap import cli
    got = {}
    monkeypatch.setattr(C, "one_shot", lambda w, t, **kw: got.update(task=t, **kw) or 0)
    cli.main(["exec", "--events", "resume", "--last", "계속", "해"])
    assert got["task"] == "계속 해" and got["events"] and got["resume"] == "--last"
    cli.main(["exec", "그냥"])
    assert got["task"] == "그냥" and got["resume"] is None and not got["events"]


# ---------------------------------------------------------------- 백그라운드 명령에 입력 보내기(Codex write_stdin)
from cap import extras as X  # noqa: E402
from cap.policy import classify_mode  # noqa: E402


def test_write_answers_a_prompt_in_a_background_command(tmp_path):
    (tmp_path / "ask.py").write_text("ans = input('Delete build? [y/N] ')\nprint('answer was', ans)\n", encoding="utf-8")
    pid = X.bg_start(tmp_path, f'"{sys.executable}" ask.py')
    try:
        ok, out = X.bg_write(tmp_path, pid, "y", wait=5)
        assert ok and "answer was y" in out
        ok, out = X.bg_write(tmp_path, pid, "again", wait=0.2)
        assert not ok and "exited" in out
    finally:
        X.bg_kill_all(tmp_path)


def test_input_to_a_shell_is_judged_as_a_command(tmp_path):
    pid = X.bg_start(tmp_path, "bash")
    other = X.bg_start(tmp_path, f'"{sys.executable}" -c "input()"')
    try:
        bad = {"action": "write", "id": pid, "text": "git push origin main"}
        assert classify_mode("process", bad, tmp_path, "auto").verdict == "ask"       # 셸에 넣는 글 = 명령
        assert classify_mode("process", bad, tmp_path, "chat").verdict == "deny"
        assert classify_mode("process", {"action": "write", "id": pid, "text": "ls"}, tmp_path, "auto").verdict == "allow"
        assert classify_mode("process", {"action": "write", "id": other, "text": "git push"}, tmp_path,
                             "auto").verdict == "allow"                                 # 스크립트의 질문에 답하기
    finally:
        X.bg_kill_all(tmp_path)


# ---------------------------------------------------------------- 노트북 읽기 · 클립보드 그림
NB = {"cells": [
    {"cell_type": "markdown", "source": ["# Title\n", "intro"]},
    {"cell_type": "code", "execution_count": 3, "source": "x = 1\nprint(x)",
     "outputs": [{"output_type": "stream", "text": ["1\n"]},
                 {"output_type": "display_data", "data": {"image/png": "AAAA", "text/plain": ["<Figure>"]}}]},
    {"cell_type": "code", "source": "1/0", "outputs": [{"output_type": "error", "ename": "ZeroDivisionError",
                                                        "evalue": "division by zero"}]}],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def test_notebook_is_read_as_cells_not_json(tmp_path):
    (tmp_path / "a.ipynb").write_text(json.dumps(NB), encoding="utf-8")
    box = T.ToolBox(tmp_path)
    out = box.execute("read_file", {"path": "a.ipynb"})["output"]
    assert "cell 1 [markdown]" in out and "# Title" in out and "cell 2 [code] (run 3)" in out
    assert "print(x)" in out and "\n" in out and "[image/png output not shown]" in out
    assert "ZeroDivisionError: division by zero" in out and '"cell_type"' not in out
    assert "a.ipynb" not in box.full_reads            # 원문(JSON)을 본 게 아니라 통째로 덮어쓰기는 막힌 채


def test_clipboard_image_saves_png(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        path = args[-1].split(".Save('")[1].split("',")[0]
        Path(path).write_bytes(b"\x89PNG fake")
        return NS(returncode=0, stdout=b"ok", stderr=b"")
    monkeypatch.setattr(X.os, "name", "nt")
    monkeypatch.setattr(X.subprocess, "run", fake_run)
    p = X.clipboard_image(tmp_path)
    assert p is not None and p.parent == tmp_path / ".cap" / "images" and p.read_bytes().startswith(b"\x89PNG")
    assert seen["args"][:4] == ["powershell", "-NoProfile", "-STA", "-Command"]


# ---------------------------------------------------------------- 중간에 멈춘 턴을 체크포인트부터 이어 하기(/continue)
def test_turn_that_crashed_midway_continues_from_the_checkpoint(ws, monkeypatch):
    import cap.loop as L
    replies = iter([{"tool_calls": [call("read_file", path="m.py")]}, "crash",
                    {"content": "m.py 를 읽었습니다. 다 했습니다."}])
    seen = []

    def fake_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        r = next(replies)
        if r == "crash":
            raise RuntimeError("server went away")
        return {"content": r.get("content", ""), "tool_calls": r.get("tool_calls", []), "finish_reason": "stop",
                "usage": {}, "budget_actions": [], "seconds": 0.0}
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    with pytest.raises(RuntimeError):
        s.run_turn("m.py 읽고 알려줘")
    sid = s.sid
    s.close()
    out = []
    ui = C.UI(out=out.append)
    ui.tty = ui.color = False
    s2 = C.ChatSession(ws, sid, "auto", ui)            # 다른 프로세스에서 다시 연 것과 같다
    try:
        assert s2.stopped_midway() == "agent"
        C.recap(s2)
        assert any("/continue" in ln for ln in out)
        _, _, turn = C.handle_command(s2, ws, "/continue")
        assert turn == C.CONTINUE
        r = s2.run_turn(turn)
        assert r["values"]["status"] == "done" and "다 했습니다" in s2.last_answer(r["values"])
        assert any(m["role"] == "tool" and "def " in m["content"] for m in seen[-1])   # 읽은 결과를 그대로 이어 받음
        assert s2.stopped_midway() == ""
        _, _, turn = C.handle_command(s2, ws, "/continue")
        assert turn is None and "Nothing to continue" in out[-1]
    finally:
        s2.close()


# ---------------------------------------------------------------- '이 프로젝트에서는 다시 묻지 않기'
from cap import trust as TR  # noqa: E402


def test_dont_ask_again_in_this_project_persists_across_sessions(ws, monkeypatch):
    cmd = f'"{sys.executable}" -c "print(41+1)"'
    script(monkeypatch, [{"tool_calls": [call("run", cmd=cmd)]}, {"content": "42"}])
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("돌려", ask=lambda p: asked.append(p) or "p")
    cat = asked[0]["calls"][0]["category"]
    assert cat.startswith("exec:") and TR.project_approvals(ws) == [cat]
    assert C.project_ok(asked[0]["calls"])
    s.close()
    seen = script(monkeypatch, [{"tool_calls": [call("run", cmd=cmd)]}, {"content": "42"}])
    s2, out = session(ws, "ask")                      # 새 세션: 같은 명령은 묻지 않는다
    asked2 = []
    s2.run_turn("또 돌려", ask=lambda p: asked2.append(p) or "n")
    assert not asked2 and any("42" in m["content"] for m in seen[-1] if m["role"] == "tool")
    C.handle_command(s2, ws, "/permissions forget")
    assert TR.project_approvals(ws) == [] and any("Forgot 1" in ln for ln in out)
    s2.close()


def test_human_only_categories_are_never_saved(tmp_path):
    assert not TR.add_project_approval(tmp_path, "test_edit") and not TR.add_project_approval(tmp_path, "sandbox_escape")
    assert TR.project_approvals(tmp_path) == []
    assert not C.project_ok([{"category": "edit"}]) and C.project_ok([{"category": "mcp:docs"}])


# ---------------------------------------------------------------- 긴 작업 끝 알림(Windows 토스트)
def test_desktop_notify_builds_a_toast_and_can_be_turned_off(monkeypatch):
    from cap import compat
    runs = []
    monkeypatch.setattr(X.os, "name", "nt")
    monkeypatch.setattr(X.subprocess, "Popen", lambda args, **kw: runs.append(args))
    assert X.desktop_notify("CAP finished (70s)", "it's done")
    script_ = runs[0][-1]
    assert "ToastText02" in script_ and "'CAP finished (70s)'" in script_ and "'it''s done'" in script_
    p = compat.cap_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"notifications": false}', encoding="utf-8")
    assert not X.desktop_notify("x", "y") and len(runs) == 1


# ---------------------------------------------------------------- .gitignore 를 따르는 파일 목록
import subprocess  # noqa: E402

from cap.store import iter_files, rel  # noqa: E402


def test_iter_files_respects_gitignore(tmp_path):
    for name, body in {"a.py": "x", "out/big.bin": "x", "logs/run.log": "x", "src/b.py": "x", ".gitignore": "out/\n*.log\n",
                       "new_untracked.py": "x"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "add", "a.py", "src/b.py", ".gitignore"], cwd=tmp_path)
    (tmp_path / ".cap").mkdir()
    (tmp_path / ".cap" / "x.json").write_text("{}", encoding="utf-8")
    got = sorted(rel(tmp_path, f) for f in iter_files(tmp_path))
    assert got == [".gitignore", "a.py", "new_untracked.py", "src/b.py"]
    (tmp_path / "a.py").unlink()                                # 지운 추적 파일은 빠진다
    assert "a.py" not in {rel(tmp_path, f) for f in iter_files(tmp_path)}


def test_iter_files_without_git_skips_common_build_dirs(tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.js").write_text("x", encoding="utf-8")
    (tmp_path / "m.py").write_text("x", encoding="utf-8")
    assert [rel(tmp_path, f) for f in iter_files(tmp_path)] == ["m.py"]


# ---------------------------------------------------------------- 모델 공급자(Codex model_providers)
import http.server  # noqa: E402
import threading  # noqa: E402


def test_provider_switch_uses_its_own_url_and_key_only(monkeypatch):
    from cap import compat
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["auth"] = self.headers.get("Authorization", "")
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": got["body"]["model"],
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "local hello"}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p = compat.cap_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"model_providers": {"mine": {"base_url": f"http://127.0.0.1:{srv.server_port}/v1",
                                                          "model": "tiny-coder", "context_window": 8192}}}),
                 encoding="utf-8")
    for k in ("BASE_URL", "MODEL", "CONTEXT_WINDOW", "_client", "_key"):
        monkeypatch.setattr(llm, k, getattr(llm, k))
    monkeypatch.setattr(llm, "PROVIDER", dict(llm.PROVIDER))
    monkeypatch.setenv("KAU_API_KEY", "kau-secret-should-not-leak")
    try:
        assert "tiny-coder" in llm.use_provider("mine") and llm.CONTEXT_WINDOW == 8192
        r = llm.chat([{"role": "user", "content": "hi"}])
        assert r["content"] == "local hello" and got["body"]["model"] == "tiny-coder"
        assert "kau-secret" not in got["auth"] and "chat_template_kwargs" not in got["body"]   # 수업 서버 키·전용 인자 안 감
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        llm.use_provider("openai")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            llm._load_key()
        with pytest.raises(ValueError):
            llm.use_provider("nope")
    finally:
        srv.shutdown()


def test_provider_command_lists_builtins(ws):
    s, out = session(ws)
    try:
        C.handle_command(s, ws, "/provider")
        text = "\n".join(out)
        assert "kau" in text and "ollama" in text and "localhost:11434" in text and "← current" in text
    finally:
        s.close()


# ---------------------------------------------------------------- read_file 로 그림 보기
def test_read_file_on_an_image_shows_it_to_the_model(ws, monkeypatch):
    png = ws / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    seen = script(monkeypatch, [{"tool_calls": [call("read_file", path="shot.png")]}, {"content": "빨간 화면입니다"}])
    s, _ = session(ws, "auto")
    try:
        s.run_turn("shot.png 봐줘")
        msgs = seen[-1]
        tool = [m for m in msgs if m["role"] == "tool"][0]["content"]
        assert "attached right after" in tool and "\x89PNG" not in tool
        after = [m for m in msgs if m["role"] == "user" and "[[image:" in str(m["content"])]
        assert after and "--- @shot.png (image) ---" in after[-1]["content"]
        sent = llm.with_images(msgs)
        assert any(isinstance(m["content"], list) and any(p.get("type") == "image_url" for p in m["content"])
                   for m in sent)
    finally:
        s.close()


# ---------------------------------------------------------------- MCP 그림 결과 · @서버:uri · /compact 지시 · exec 옵션
IMG_SERVER = r'''import base64
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent
app = MCPServer("shots")
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()

@app.tool()
def screenshot() -> list:
    """Take a screenshot."""
    return [ImageContent(type="image", data=PNG, mimeType="image/png")]

@app.resource("notes://todo", name="todo", description="todo list")
def todo() -> str:
    return "1. ship v0.6"

if __name__ == "__main__":
    app.run()
'''


@pytest.mark.skipif(not MC.available(), reason="mcp package not installed")
def test_mcp_image_result_and_resource_mention(ws, monkeypatch):
    (ws / "shots_srv.py").write_text(IMG_SERVER, encoding="utf-8")
    (ws / ".cap").mkdir(exist_ok=True)
    (ws / ".cap" / "mcp.json").write_text(json.dumps({"mcpServers": {"shots": {
        "command": sys.executable, "args": [str(ws / "shots_srv.py")]}}}), encoding="utf-8")
    MC.trust(ws)
    shot = {"id": "c1", "type": "function", "function": {"name": "mcp__shots__screenshot", "arguments": "{}"}}
    seen = script(monkeypatch, [{"tool_calls": [shot]}, {"content": "검은 화면"}, {"content": "1번은 v0.6"}])
    s, _ = session(ws, "bypass")
    try:
        s.run_turn("화면 찍어서 봐줘")
        tool = [m for m in seen[-1] if m["role"] == "tool"][0]["content"]
        assert "attached right after" in tool and ".cap/images/mcp-" in tool
        img = [m for m in seen[-1] if m["role"] == "user" and "[[image:" in str(m["content"])]
        assert img and list((ws / ".cap" / "images").glob("mcp-*.png"))
        s.run_turn("@shots:notes://todo 첫 항목이 뭐야?")
        last_user = [m for m in seen[-1] if m["role"] == "user"][-1]["content"]
        assert "--- @shots:notes://todo (MCP resource) ---" in last_user and "1. ship v0.6" in last_user
    finally:
        s.close()


def test_compact_passes_the_users_focus(ws, monkeypatch):
    seen = script(monkeypatch, [{"content": "답1"}, {"content": "답2"}, {"content": "요약: 결정 A"}])
    s, out = session(ws, "auto")
    try:
        s.run_turn("첫 요청")
        s.run_turn("둘째 요청")
        C.handle_command(s, ws, "/compact keep the decision about A")
        assert "keep the decision about A" in seen[-1][-1]["content"] and any("Compacted" in ln for ln in out)
    finally:
        s.close()


def test_exec_max_turns_and_output_file(ws, monkeypatch, tmp_path):
    from cap import cli
    import cap.loop as L
    got = {}
    monkeypatch.setattr(C, "one_shot", lambda w, t, **kw: got.update(task=t, **kw) or 0)
    cli.main(["exec", "--max-turns", "3", "-o", "last.txt", "고쳐"])
    assert got["task"] == "고쳐" and got["max_turns"] == 3 and got["out_file"] == "last.txt"
    assert cli.main(["exec", "--max-turns", "x", "고쳐"]) == 2
    monkeypatch.undo()
    monkeypatch.setattr(L, "MAX_TURNS", L.MAX_TURNS)
    script(monkeypatch, [{"content": "다 했어요"}])
    out = tmp_path / "answer.txt"
    C.one_shot(ws, "해줘", approval="auto", as_json=True, max_turns=5, out_file=str(out))
    assert out.read_text(encoding="utf-8") == "다 했어요" and L.MAX_TURNS == 5


# ---------------------------------------------------------------- 문서의 그래프 그림 = 실제 컴파일된 그래프
def test_documented_graph_matches_the_compiled_graph():
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "cap" / "graph.mmd").read_text(encoding="utf-8").strip()
    assert C.loop_mermaid() == doc, "graph changed: run `cap loop-graph > docs/cap/graph.mmd` and update README"
    assert doc in (root / "README.md").read_text(encoding="utf-8")


def test_resume_last_is_not_taken_as_a_task(monkeypatch):
    from cap import cli
    got = {}
    monkeypatch.setattr(C, "repl", lambda w, approval="ask", resume=None: got.update(resume=resume) or 0)
    monkeypatch.setattr(C, "one_shot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("taken as a task")))
    cli.main(["--plain", "--resume", "last"])
    assert got.get("resume") == "last"


# ---------------------------------------------------------------- apply_patch(Codex 형식)
PATCH = """*** Begin Patch
*** Update File: m.py
@@ def double(x):
-    return x + x + 1
+    return x + x
*** Add File: util/helpers.py
+def triple(x):
+    return 3 * x
*** Delete File: old.txt
*** End Patch"""


def test_apply_patch_updates_adds_and_deletes_all_or_nothing(ws):
    (ws / "old.txt").write_text("bye\n", encoding="utf-8")
    box = T.ToolBox(ws)
    r = box.execute("apply_patch", {"patch": PATCH})
    assert r["ok"], r["output"]
    assert "return x + x\n" in (ws / "m.py").read_text(encoding="utf-8") and "+ 1" not in (ws / "m.py").read_text()
    assert (ws / "util" / "helpers.py").read_text(encoding="utf-8") == "def triple(x):\n    return 3 * x\n"
    assert not (ws / "old.txt").exists() and set(r["changed"]) == {"m.py", "util/helpers.py", "old.txt"}
    before = (ws / "m.py").read_text(encoding="utf-8")
    bad = PATCH.replace("*** Delete File: old.txt\n", "").replace("util/helpers.py", "util/other.py") \
        .replace("-    return x + x + 1", "-    return nothing_like_this").replace("+    return x + x", "+    return x * 2")
    r2 = box.execute("apply_patch", {"patch": bad})
    assert not r2["ok"] and "did not match" in r2["output"]
    assert (ws / "m.py").read_text(encoding="utf-8") == before and not (ws / "util" / "other.py").exists()


def test_apply_patch_move_and_pure_addition(ws):
    box = T.ToolBox(ws)
    p = ("*** Begin Patch\n*** Update File: m.py\n*** Move to: core/m2.py\n@@\n+\n+VERSION = 2\n*** End Patch")
    r = box.execute("apply_patch", {"patch": p})
    assert r["ok"], r["output"]
    assert not (ws / "m.py").exists() and (ws / "core" / "m2.py").read_text(encoding="utf-8").endswith("VERSION = 2\n")
    assert not box.execute("apply_patch", {"patch": "*** Update File: x\n"})["ok"]          # Begin/End 없음


def test_apply_patch_permission_is_the_strictest_file(ws):
    from cap.policy import classify_mode
    ok = {"patch": "*** Begin Patch\n*** Add File: a.py\n+x = 1\n*** End Patch"}
    assert classify_mode("apply_patch", ok, ws, "auto").verdict == "allow"
    assert classify_mode("apply_patch", ok, ws, "ask").verdict == "ask"
    assert classify_mode("apply_patch", ok, ws, "chat").verdict == "deny"
    sneaky = {"patch": "*** Begin Patch\n*** Add File: a.py\n+x = 1\n*** Add File: ../outside.py\n+x\n*** End Patch"}
    assert classify_mode("apply_patch", sneaky, ws, "bypass").verdict == "deny"         # 폴더 밖은 늘 거부
    tests = {"patch": "*** Begin Patch\n*** Update File: tests/test_m.py\n@@\n-def test_double():\n+def test_x():\n"
                      "*** End Patch"}
    assert classify_mode("apply_patch", tests, ws, "auto").category == "test_edit"      # 테스트 편집은 사람에게
    agents = {"patch": "*** Begin Patch\n*** Add File: AGENTS.md\n+rules\n*** End Patch"}
    assert classify_mode("apply_patch", agents, ws, "bypass").verdict == "ask"         # 지침 파일은 늘 사람
    delete = {"patch": "*** Begin Patch\n*** Delete File: m.py\n*** End Patch"}
    assert classify_mode("apply_patch", delete, ws, "auto").verdict == "ask"            # 지우기는 auto 에서도 묻기


def test_apply_patch_is_offered_only_off_the_class_server(ws, monkeypatch):
    seen_tools = []

    def fake_chat(messages, tools=None, **kw):
        seen_tools.append({t["function"]["name"] for t in tools or []})
        return {"content": "끝", "tool_calls": [], "finish_reason": "stop", "usage": {}, "budget_actions": [],
                "seconds": 0.0}
    import cap.loop as L
    monkeypatch.setattr(L.llm, "chat", fake_chat)
    s, _ = session(ws, "auto")
    s.run_turn("hi")
    s.close()
    assert "apply_patch" not in seen_tools[-1]
    monkeypatch.setitem(llm.PROVIDER, "name", "openai")
    s, _ = session(ws, "auto")
    s.run_turn("hi")
    s.close()
    assert "apply_patch" in seen_tools[-1]


def test_find_ws_stops_at_the_git_root(tmp_path):
    from cap.store import find_ws
    (tmp_path / ".cap").mkdir()                       # 저장소 바깥(위)의 .cap
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo)
    assert find_ws(repo / "src") == (repo / "src").resolve()       # 위 폴더의 .cap 을 잡지 않는다
    (repo / ".cap").mkdir()
    assert find_ws(repo / "src") == repo.resolve()                  # 저장소 안의 .cap 은 잡는다
    plain = tmp_path / "plain" / "sub"
    plain.mkdir(parents=True)
    assert find_ws(plain) == tmp_path.resolve()                     # git 이 아니면 예전처럼 위로 찾는다
