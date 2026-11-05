"""Claude Code 처럼 스킬을 /이름 으로 바로 부르기."""
from pathlib import Path

from cap import chat as C


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_skills_are_slash_commands_like_claude_code(tmp_path):
    ws = tmp_path / "ws"
    _w(ws / ".claude" / "skills" / "release" / "SKILL.md", "---\nname: release\ndescription: cut a release\n---\nsteps\n")
    _w(ws / ".claude" / "skills" / "hidden" / "SKILL.md",
       "---\nname: hidden\ndescription: internal\nuser-invocable: false\n---\nx\n")
    names = [c for c, _, _ in C.all_commands(ws)]
    assert "/release" in names and "/hidden" not in names

    class S:
        ui = type("U", (), {"out": staticmethod(lambda *a, **k: None)})()
    _, _, text = C.handle_command(S(), ws, "/release v2.0")
    assert "Use the skill `release`" in text and "Task: v2.0" in text


def test_user_level_instructions_from_codex_and_claude(tmp_path):
    from cap import compat
    from cap.chat import load_instructions
    _w(compat.codex_home() / "AGENTS.md", "codex global rule")
    _w(compat.claude_home() / "CLAUDE.md", "claude global rule")
    _w(tmp_path / "AGENTS.md", "project rule")
    names, text = load_instructions(tmp_path)
    assert names == ["~/.codex/AGENTS.md", "~/.claude/CLAUDE.md", "AGENTS.md"]
    assert text.index("codex global") < text.index("project rule")


def test_config_command_shows_effective_settings(tmp_path):
    out = []
    ui = C.UI(out=out.append)
    ui.tty = ui.color = False
    s = C.ChatSession(tmp_path, None, "auto", ui)
    C.handle_command(s, tmp_path, "/config")
    text = "\n".join(out)
    for key in ("mode          auto", "sandbox", "model", "imports", "rules", "hooks", "mcp", "files"):
        assert key in text, key
    s.close()
