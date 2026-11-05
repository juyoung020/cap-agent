import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _isolated_user_home(tmp_path_factory, monkeypatch):
    """사용자 기억·스킬·훅(~/.cap)은 실제 홈 대신 빈 임시 폴더를 보게 한다 — 이 PC 설정에 따라 결과가 바뀌지 않게."""
    home = tmp_path_factory.mktemp("home") / ".cap"
    monkeypatch.setenv("CAP_SKIP_SETUP_CHECK", "1")        # 처음 설정 안내는 test_setup.py 에서만
    from cap import hooks, mcp_client, memory
    monkeypatch.setattr(memory, "user_home", lambda: home)
    monkeypatch.setattr(hooks, "user_file", lambda: home / "hooks.json")
    monkeypatch.setattr(mcp_client, "user_file", lambda: home / "mcp.json")
    from cap import sandbox
    monkeypatch.setitem(sandbox.STATE, "mode", "off")      # 격리는 시험마다 직접 켠다(OS 에 따라 결과가 바뀌지 않게)
    from cap import compat                                  # 이 PC 의 Claude Code·Codex 설정도 보지 않게
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setattr(compat, "claude_home", lambda: other / ".claude")
    monkeypatch.setattr(compat, "claude_json", lambda: other / ".claude.json")
    monkeypatch.setattr(compat, "codex_home", lambda: other / ".codex")
    from cap import pluginhub, skillhub                     # 기본 원격 출처(GitHub)는 시험에서 읽지 않는다
    monkeypatch.setattr(pluginhub, "DEFAULT_MARKETS", ())
    monkeypatch.setattr(skillhub, "DEFAULT_SOURCES", ())
