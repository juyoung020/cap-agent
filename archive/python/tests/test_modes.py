"""모드 5개(대화·승인·플랜·오토·바이패스)의 권한표: 허용(allow) / 승인(ask) / 금지(deny)."""

import pytest

from cap.policy import (APPROVAL_MODES, MODE_CYCLE, MODE_PROMPT, classify_mode, next_mode, normalize_mode)

READ = ("read_file", {"path": "a.py"})
SEARCH = ("search", {"pattern": "def"})
TODO = ("todo", {"items": [{"text": "x"}]})
EDIT = ("edit_file", {"path": "a.py", "old": "x", "new": "y"})
WRITE = ("write_file", {"path": "new.py", "content": "x = 1\n"})
TESTS = ("run_tests", {})
SAFE = ("run", {"cmd": "git status"})
OTHER = ("run", {"cmd": "python x.py"})
RISKY = ("run", {"cmd": "rm junk.txt"})
NET = ("run", {"cmd": "pip install requests"})
# 하드 금지: 어느 모드든 deny
FORBID = [("run", {"cmd": "rm -rf /"}), ("write_file", {"path": "../out.py", "content": ""}),
          ("write_file", {"path": ".env", "content": ""}), ("read_file", {"path": ".env"}),
          ("run", {"cmd": "cat .env"})]
INSTR = ("write_file", {"path": "AGENTS.md", "content": "x"})

#            읽기     검색     todo     편집     새 파일  테스트   읽기명령  기타명령  위험     설치     지침 파일
TABLE = {
    "chat":   ["allow", "allow", "allow", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny"],
    "ask":    ["allow", "allow", "allow", "ask", "ask", "allow", "allow", "ask", "ask", "ask", "ask"],
    "plan":   ["allow", "allow", "allow", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny"],
    "auto":   ["allow", "allow", "allow", "allow", "allow", "allow", "allow", "allow", "ask", "ask", "ask"],
    "bypass": ["allow", "allow", "allow", "allow", "allow", "allow", "allow", "allow", "allow", "allow", "ask"],
}
CASES = [READ, SEARCH, TODO, EDIT, WRITE, TESTS, SAFE, OTHER, RISKY, NET, INSTR]


@pytest.mark.parametrize("mode", list(TABLE))
def test_mode_table(mode, tmp_path):
    got = [classify_mode(t, a, tmp_path, mode).verdict for t, a in CASES]
    assert got == TABLE[mode]


@pytest.mark.parametrize("mode", APPROVAL_MODES)
def test_hard_forbid_in_every_mode(mode, tmp_path):
    for t, a in FORBID:
        assert classify_mode(t, a, tmp_path, mode).verdict == "deny", (mode, t, a)


def test_session_always_answers(tmp_path):
    assert classify_mode(*EDIT, tmp_path, "ask", {"edit"}).verdict == "allow"
    k = classify_mode(*OTHER, tmp_path, "ask").category
    assert classify_mode(*OTHER, tmp_path, "ask", {k}).verdict == "allow"
    assert classify_mode(*RISKY, tmp_path, "auto", {"delete"}).verdict == "allow"
    assert classify_mode(*EDIT, tmp_path, "plan", {"edit"}).verdict == "deny"     # 플랜에선 [a] 로도 못 푼다


def test_deny_reason_tells_model_the_mode(tmp_path):
    assert "plan mode" in classify_mode(*EDIT, tmp_path, "plan").reason
    assert "chat mode" in classify_mode(*OTHER, tmp_path, "chat").reason
    assert "todo" in MODE_PROMPT["plan"] and "chat" in MODE_PROMPT["chat"]


def test_names_aliases_and_cycle():
    assert APPROVAL_MODES == ("chat", "ask", "plan", "auto", "bypass")
    assert normalize_mode("auto-edit") == normalize_mode("full-auto") == "auto"
    assert normalize_mode("플랜") == "plan" and normalize_mode(None) == "ask"
    with pytest.raises(ValueError):
        normalize_mode("nope")
    m, seen = "ask", []
    for _ in range(len(MODE_CYCLE)):
        m = next_mode(m)
        seen.append(m)
    assert sorted(seen) == sorted(APPROVAL_MODES) and seen[-1] == "ask"
