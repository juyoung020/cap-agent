"""스킬 찾아 설치하기 (네트워크 없이: GitHub 색인과 다운로드는 가짜로)."""
import json
from pathlib import Path

from cap import compat
from cap import memory as M
from cap import skillhub as H
from cap.policy import classify_mode
from test_chat import script, session, ws  # noqa: F401


def _local_market():
    sk = compat.claude_home() / "plugins" / "marketplaces" / "mkt" / "plugins" / "docs" / "skills" / "pdf-tools"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: fill pdf forms and merge pdf files\n---\n1. use pypdf\n",
                                 encoding="utf-8")
    (sk / "scripts").mkdir()
    (sk / "scripts" / "merge.py").write_text("print('merge')\n", encoding="utf-8")
    (sk / ".env").write_text("SECRET=1", encoding="utf-8")
    return sk


def _fake_github(monkeypatch):
    calls = []
    monkeypatch.setattr(H, "DEFAULT_SOURCES", ("anthropics/skills",))

    def idx(repo):
        calls.append(repo)
        if repo != "anthropics/skills":
            return []
        return [{"id": "anthropics/skills:skills/xlsx", "name": "xlsx", "description": "excel spreadsheets and charts",
                 "source": "github anthropics/skills", "files": ["skills/xlsx/SKILL.md", "skills/xlsx/ref/a.md"],
                 "branch": "main"}]
    monkeypatch.setattr(H, "_github_index", idx)

    class R:
        def __init__(self, body):
            self.status_code, self.content = 200, body
    bodies = {"skills/xlsx/SKILL.md": b"---\nname: xlsx\ndescription: excel spreadsheets and charts\n---\nuse openpyxl\n",
              "skills/xlsx/ref/a.md": b"reference"}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(bodies[url.split("/main/", 1)[1]]))
    return calls


def test_search_ranks_name_and_description(monkeypatch, tmp_path):
    _local_market()
    calls = _fake_github(monkeypatch)
    ok, out = H.search("pdf forms")
    assert ok and "pdf-tools" in out and "local Claude Code marketplace" in out and "xlsx" not in out
    ok, out = H.search("excel charts")
    assert ok and out.splitlines()[1].startswith("- anthropics/skills:skills/xlsx")
    n = len(calls)
    H.search("excel")                                   # 두 번째부터는 캐시
    assert len(calls) == n
    assert not H.search("zzzz-nothing")[0]


def test_install_local_and_github(monkeypatch, ws):
    sk = _local_market()
    _fake_github(monkeypatch)
    ok, msg = H.install(ws, f"local:{sk.resolve().as_posix()}")
    assert ok and "contains scripts (scripts/merge.py); they were not run" in msg
    dest = ws / ".cap" / "skills" / "pdf-tools"
    assert (dest / "SKILL.md").is_file() and (dest / "scripts" / "merge.py").is_file() and not (dest / ".env").exists()
    ok, msg = H.install(ws, "anthropics/skills:skills/xlsx", scope="user")
    assert ok and (compat.cap_home() / "skills" / "xlsx" / "ref" / "a.md").read_text() == "reference"
    assert {"pdf-tools", "xlsx"} <= set(M.find_skills(ws))
    assert not H.install(ws, "nope:x")[0]


def test_policy_search_free_install_always_human(tmp_path):
    assert classify_mode("skill_search", {"query": "x"}, tmp_path, "ask").verdict == "allow"
    for mode in ("ask", "auto"):
        d = classify_mode("skill_install", {"id": "a:b"}, tmp_path, mode)
        assert d.verdict == "ask" and d.category == "skill_install"
    assert classify_mode("skill_install", {"id": "a:b"}, tmp_path, "chat").verdict == "deny"


def test_agent_searches_installs_and_opens(ws, monkeypatch):
    sk = _local_market()
    _fake_github(monkeypatch)
    sid = f"local:{sk.resolve().as_posix()}"

    def tc(name, **args):
        return {"id": f"c_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    seen = script(monkeypatch, [{"tool_calls": [tc("skill_search", query="pdf forms")]},
                                {"tool_calls": [tc("skill_install", id=sid)]},
                                {"tool_calls": [{"id": "c_open", "type": "function",
                                                 "function": {"name": "skill", "arguments": json.dumps({"name": "pdf-tools"})}}]},
                                {"content": "pypdf 로 하면 됩니다."}])
    s, _ = session(ws, approval="auto")
    asked = []
    s.run_turn("pdf 양식 채우는 법 찾아서 알려줘", ask=lambda p: asked.append(p) or "y")
    assert [c["category"] for p in asked for c in p["calls"]] == ["skill_install"]   # 검색은 묻지 않고, 설치는 묻는다
    last_tool = [m for m in seen[3] if m["role"] == "tool"][-1]["content"]
    assert "use pypdf" in last_tool
    s.close()
