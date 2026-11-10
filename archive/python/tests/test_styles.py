"""출력 스타일: 기본 제공 + Claude Code 형식 파일, 고르면 매 요청에 덧붙는다(화면에는 원래 요청만)."""
from cap import chat as C
from cap import compat
from test_chat import script, session, ws  # noqa: F401


def test_output_style_is_added_to_each_request(ws, monkeypatch):
    d = ws / ".claude" / "output-styles"
    d.mkdir(parents=True)
    (d / "pirate.md").write_text("---\nname: pirate\ndescription: talk like a pirate\n---\nAnswer like a pirate.\n",
                                 encoding="utf-8")
    seen = script(monkeypatch, [{"content": "아르르"}, {"content": "보통"}])
    s, out = session(ws)
    C.handle_command(s, ws, "/output-style")
    assert any("pirate" in ln and "talk like a pirate" in ln for ln in out) and any("concise" in ln for ln in out)
    C.handle_command(s, ws, "/output-style pirate")
    r = s.run_turn("안녕")
    user = [m for m in seen[0] if m["role"] == "user"][-1]["content"]
    assert "[Output style: pirate] Answer like a pirate." in user
    assert C.shown_goal(r["values"]["turn_log"][-1]["goal"]) == "안녕"
    C.handle_command(s, ws, "/output-style default")
    s.run_turn("다시")
    user2 = [m for m in seen[1] if m["role"] == "user"][-1]["content"]
    assert "Output style" not in user2
    s.close()


def test_default_style_from_settings(ws):
    p = compat.cap_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"outputStyle": "concise"}', encoding="utf-8")
    s, _ = session(ws)
    assert s.output_style == "concise"
    s.close()



def test_exec_with_output_schema(ws, monkeypatch, capsys):
    import json
    from cap import llm
    script(monkeypatch, [{"content": "calc.py 를 고쳤고 테스트 1개가 통과했습니다."}])
    seen = {}

    def fake_chat_json(messages, schema, **kw):
        seen["schema"], seen["text"] = schema, messages[-1]["content"]
        return {"fixed": True, "files": []}
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    schema = {"type": "object", "properties": {"fixed": {"type": "boolean"}, "files": {"type": "array"}},
              "required": ["fixed", "files"]}
    code = C.one_shot(ws, "고쳐줘", approval="auto", as_json=True, schema=schema)
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and data["output"] == {"fixed": True, "files": []}
    assert seen["schema"] == schema and "calc.py 를 고쳤고" in seen["text"]
