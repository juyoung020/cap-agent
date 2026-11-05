"""상태 줄: Claude Code statusLine 형식의 명령 출력 첫 줄을 대화창 아래에."""
import asyncio
import json
import sys
from pathlib import Path

from cap import compat, statusline as SL
from cap import tui

SCRIPT = '''import sys, json
d = json.load(sys.stdin)
print("\\x1b[32m" + d["model"]["display_name"] + " @ " + d["mode"] + "\\x1b[0m")
print("second line ignored")
'''


def _setup(tmp_path):
    script = tmp_path / "status.py"
    script.write_text(SCRIPT, encoding="utf-8")
    cmd = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    p = compat.cap_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": cmd}}), encoding="utf-8")
    return cmd


def test_render_first_line_without_colors(tmp_path):
    cmd = _setup(tmp_path)
    assert SL.command() == cmd
    out = SL.render(tmp_path, SL.info(tmp_path, "s1", "auto", "qwen3.5-9b", 100, 16384))
    assert out == "qwen3.5-9b @ auto"


def test_claude_status_line_needs_import_hooks(tmp_path):
    (compat.claude_home()).mkdir(parents=True, exist_ok=True)
    (compat.claude_home() / "settings.json").write_text(json.dumps({"statusLine": {"command": "echo claude"}}))
    assert SL.command() == ""
    p = compat.cap_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"import": {"hooks": True}}), encoding="utf-8")
    assert SL.command() == "echo claude"


def test_tui_footer_shows_status_line(tmp_path):
    _setup(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    async def go():
        app = tui.make_app(ws, approval="auto")
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(50):
                await pilot.pause(0.1)
                if app.status_text:
                    break
            assert app.status_text.endswith("@ auto")
            assert app.status_text in tui.plain_text(app.query_one("#stats").render())
    asyncio.run(go())
