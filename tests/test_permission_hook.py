"""PermissionRequest 훅: 사람에게 묻기 전에 스크립트가 허용·거부를 정한다(사람 전용 범주는 허용 못 함)."""
from test_chat import call, script, session, ws  # noqa: F401
from test_memory_hooks import _hooks, _script_file

DECIDER = r'''import sys, json
d = json.load(sys.stdin)
cmd = str(d["tool_input"].get("cmd", ""))
if cmd.startswith("make"):
    print(json.dumps({"decision": "allow", "reason": "make ok"}))
elif "push" in cmd:
    print(json.dumps({"decision": "deny", "reason": "no pushing"}))
else:
    print(json.dumps({"decision": "allow"}))
'''


def test_permission_request_hook_decides_before_the_human(ws, monkeypatch):
    _hooks(ws, {"PermissionRequest": [{"hooks": [{"command": _script_file(ws, "dec.py", DECIDER)}]}]})
    seen = script(monkeypatch, [{"tool_calls": [call("run", cmd="make build"), call("run", cmd="git push origin main"),
                                                call("edit_file", path="tests/test_m.py", old="def test_double",
                                                     new="def test_double_x")]},
                                {"content": "끝"}])
    s, _ = session(ws, "ask")
    asked = []
    s.run_turn("해줘", ask=lambda p: asked.append(p) or "n")
    text = "\n".join(m["content"] for m in seen[1] if m["role"] == "tool")
    assert "denied by a PermissionRequest hook: no pushing" in text
    assert [c["tool"] for p in asked for c in p["calls"]] == ["edit_file"]     # 테스트 편집은 훅이 허락해도 사람에게
    assert "make build" not in str(asked)                                       # 훅이 허락한 명령은 묻지 않음
    s.close()


def test_session_end_hook_runs_once_on_close(ws):
    marker = ws / "ended.txt"
    _hooks(ws, {"SessionEnd": [{"hooks": [{"command": _script_file(ws, "end.py",
                                                                  f"open(r'{marker}', 'a').write('x')\n")}]}]})
    s, _ = session(ws)
    s.close()
    s.close()
    assert marker.read_text() == "x"
