from cap.tools import ToolBox, apply_edit

SRC = "def f(x):\n    if x < 0:\n        return 0\n    return x  \n\n\ndef g():\n    if x < 0:\n        return 0\n"


def test_exact():
    r = apply_edit("a = 1\nb = 2\n", "b = 2", "b = 3")
    assert r.ok and r.stage == "exact" and r.new_text == "a = 1\nb = 3\n"


def test_ambiguous_rejected():
    r = apply_edit(SRC, "    if x < 0:\n        return 0", "    if x <= 0:\n        return 0")
    assert not r.ok and "곳" in r.message


def test_trailing_whitespace_stage():
    r = apply_edit(SRC, "    return x\n    \n", "    return x * 2\n")
    assert r.ok and r.stage == "rstrip"
    assert "    return x * 2" in r.new_text


def test_indent_stage_reindents_new():
    text = "class A:\n    def m(self):\n        a = 1\n        b = 2\n"
    r = apply_edit(text, "def m(self):\n    a = 1", "def m(self):\n    a = 10")
    assert r.ok and r.stage == "indent"
    assert "    def m(self):\n        a = 10\n        b = 2" in r.new_text


def test_already_applied_is_success():
    r = apply_edit("total = 2\n", "total = 1", "total = 2")
    assert r.ok and r.stage == "already_applied" and r.new_text is None
    # 8자 미만 new 는 파일에 우연히 있을 수 있어 '이미 적용'으로 치지 않는다(Hermes 07 §4-7)
    assert not apply_edit("def f(x):\n    return x\n", "nope", "x").ok


def test_not_found():
    r = apply_edit("def divide(a, b):\n    return a // b\n", "def divide(a,b):\n    return a/b", "z")
    assert not r.ok


def test_crlf_file():
    r = apply_edit("a = 1\r\nb = 2\r\n", "b = 2 \n", "b = 3\n")
    assert r.ok and r.new_text == "a = 1\r\nb = 3\r\n"


def test_write_file_requires_full_read(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    box = ToolBox(tmp_path)
    assert not box.execute("write_file", {"path": "a.py", "content": "y"})["ok"]
    assert box.execute("read_file", {"path": "a.py"})["ok"]
    assert box.execute("write_file", {"path": "a.py", "content": "y"})["ok"]
    assert box.execute("write_file", {"path": "new.py", "content": "z"})["ok"]


def test_partial_read_not_enough(tmp_path):
    (tmp_path / "a.py").write_text("1\n2\n3\n", encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "a.py", "offset": 2, "limit": 1})
    assert not box.execute("write_file", {"path": "a.py", "content": "y"})["ok"]


def test_edit_tool_records_stage(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1   \n", encoding="utf-8")
    box = ToolBox(tmp_path)
    r = box.execute("edit_file", {"path": "a.py", "old": "def f():\n    return 1\n", "new": "def f():\n    return 2\n"})
    assert r["ok"] and r["stage"] == "rstrip" and r["changed"] == ["a.py"]


def test_ambiguous_shows_line_numbers():
    r = apply_edit("a = 1\nb = 2\na = 1\n", "a = 1", "a = 3")
    assert not r.ok and "L1:" in r.message and "L3:" in r.message


def test_syntax_error_introduced_by_edit_is_reported(tmp_path):
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    box = ToolBox(tmp_path)
    res = box.execute("edit_file", {"path": "m.py", "old": "    return x", "new": "    return (x"})
    assert res["ok"] and res["syntax_error"] and "문법 오류" in res["output"]
    res = box.execute("edit_file", {"path": "m.py", "old": "    return (x", "new": "    return x + 1"})
    assert res["ok"] and not res["syntax_error"]


def test_syntax_note_ignores_already_broken_file(tmp_path):
    (tmp_path / "m.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    res = ToolBox(tmp_path).execute("edit_file", {"path": "m.py", "old": "    pass", "new": "    return 1"})
    assert res["ok"] and not res["syntax_error"]
