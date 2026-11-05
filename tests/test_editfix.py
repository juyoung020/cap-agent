"""편집·쓰기 보강: 유니코드 문장부호 단계, 두 번 이스케이프 감지, 나눠 읽기 = 통째로 읽기, 편집 diff, write_file 내용 검사."""

from cap.tools import ToolBox, apply_edit, apply_edit_args

# ---------------------------------------------------------------- 1. unicode 단계

CURLY = "# note \u2014 keep\nmsg = \u201chello\u201d \u2014 ok\nn = 1\n"


def test_unicode_stage_matches_ascii_old_and_keeps_untouched_lines():
    r = apply_edit(CURLY, '# note - keep\nmsg = "hello" - ok\nn = 1', '# note - keep\nmsg = "hello" - ok\nn = 2')
    assert r.ok and r.stage == "unicode"
    assert r.new_text == "# note \u2014 keep\nmsg = \u201chello\u201d \u2014 ok\nn = 2\n"


def test_unicode_stage_reindents_and_keeps_file_line():
    text = "def f():\n    s = \u2018a\u2019\u2026\n    return s\n"
    r = apply_edit(text, "s = 'a'...\nreturn s", "s = 'a'...\nreturn s + 'b'")
    assert r.ok and r.stage == "unicode"
    assert r.new_text == "def f():\n    s = \u2018a\u2019\u2026\n    return s + 'b'\n"


def test_ideographic_space_with_curly_quotes():
    text = "title = \u201c\uc81c\ubaa9\u201d\u3000# \uc8fc\uc11d\nx = 1\n"
    r = apply_edit(text, 'title = "\uc81c\ubaa9" # \uc8fc\uc11d\nx = 1', 'title = "\uc81c\ubaa9" # \uc8fc\uc11d\nx = 2')
    assert r.ok and r.stage == "unicode"
    assert r.new_text.startswith("title = \u201c\uc81c\ubaa9\u201d\u3000# \uc8fc\uc11d\nx = 2")


def test_whitespace_only_difference_keeps_old_stage():
    # 전에 spaces 단계에서 맞던 편집은 그대로 spaces
    r = apply_edit("a\u3000=\u30001\nb = 2\n", "a = 1\nb = 2", "a = 1\nb = 3")
    assert r.ok and r.stage == "spaces"


def test_unicode_ambiguous_lists_places():
    text = "x = \u201ca\u201d\ny = 0\nx = \u201ca\u201d\n"
    r = apply_edit(text, 'x = "a"', 'x = "b"')
    assert not r.ok and "unicode" in r.message and "L1:" in r.message and "L3:" in r.message


def test_double_escaped_arguments_are_reported_not_fixed():
    text = "def f():\n    return 1\n"
    r = apply_edit(text, "def f():\\n    return 1", "def f():\\n    return 2")
    assert not r.ok and "escaped twice" in r.message and r.new_text is None
    r = apply_edit_args(text, {"edits": [{"old": "def f():\\n    return 1", "new": "x"}]})
    assert not r.ok and "escaped twice" in r.message


def test_literal_backslash_n_still_matches_exactly():
    text = 'print("a\\nb")\n'
    r = apply_edit(text, 'print("a\\nb")', 'print("a\\nc")')
    assert r.ok and r.stage == "exact"


# ---------------------------------------------------------------- 2. 나눠 읽기

BIG = "".join(f"line_{k} = {k}  # padding padding padding\n" for k in range(1, 401))


def _read_all_in_pages(box, path):
    off = 1
    while True:
        out = box.execute("read_file", {"path": path, "offset": off, "limit": 100})["output"]
        head = out.splitlines()[0]          # [path 줄 a-b / 전체 n]
        a, b = head.split("줄 ")[1].split(" /")[0].split("-")
        total = int(head.rsplit("전체 ", 1)[1].rstrip("]"))
        if int(b) >= total:
            return
        off = int(b) + 1


def test_pieced_reads_count_as_full_read(tmp_path):
    (tmp_path / "big.py").write_text(BIG, encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "big.py"})        # 한 번에는 MAX_OUT 을 넘어 잘린다
    assert box.check_write("big.py")
    _read_all_in_pages(box, "big.py")
    assert box.check_write("big.py") == ""
    assert box.execute("write_file", {"path": "big.py", "content": "x = 1\n"})["ok"]


def test_pieced_reads_reset_when_file_changes(tmp_path):
    fp = tmp_path / "big.py"
    fp.write_text(BIG, encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "big.py", "offset": 1, "limit": 100})
    box.execute("read_file", {"path": "big.py", "offset": 101, "limit": 100})
    fp.write_text(BIG.replace("line_1 = 1", "line_1 = 9"), encoding="utf-8")
    box.execute("read_file", {"path": "big.py", "offset": 201, "limit": 100})
    box.execute("read_file", {"path": "big.py", "offset": 301, "limit": 100})
    assert "big.py" not in box.full_reads and box.check_write("big.py")


def test_one_page_is_not_a_full_read(tmp_path):
    (tmp_path / "big.py").write_text(BIG, encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "big.py", "offset": 1, "limit": 100})
    assert box.check_write("big.py")
    assert not box.execute("write_file", {"path": "big.py", "content": "x = 1\n"})["ok"]


def test_run_changing_file_drops_read_ranges(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "a.py"})
    assert "a.py" in box.read_ranges
    box.execute("run", {"cmd": "python -c \"open('a.py','w').write('x = 2\\n')\""})
    assert "a.py" not in box.read_ranges and "a.py" not in box.full_reads


# ---------------------------------------------------------------- 3. 편집 diff

def test_edit_returns_diff(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    out = ToolBox(tmp_path).execute("edit_file", {"path": "a.py", "old": "b = 2", "new": "b = 20"})["output"]
    assert "@@" in out and "-b = 2" in out and "+b = 20" in out and "no need to re-read" in out


def test_big_edit_diff_is_truncated(tmp_path):
    (tmp_path / "a.py").write_text("".join(f"v{k} = {k}\n" for k in range(100)), encoding="utf-8")
    old = "".join(f"v{k} = {k}\n" for k in range(100))
    new = "".join(f"v{k} = {k * 2}  # doubled\n" for k in range(100))
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "a.py"})
    out = box.execute("edit_file", {"path": "a.py", "old": old, "new": new})["output"]
    body = out.split("no need to re-read")[0]
    assert "diff truncated" in out and "-v0 = 0" in out and "v99 = 198" not in out
    assert len(body.splitlines()) <= 28 and len(body) <= 1400


def test_several_edits_give_one_diff(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n\n\n\n\nb = 2\n", encoding="utf-8")
    out = ToolBox(tmp_path).execute("edit_file", {"path": "a.py", "edits": [
        {"old": "a = 1", "new": "a = 10"}, {"old": "b = 2", "new": "b = 20"}]})["output"]
    assert out.count("no need to re-read") == 1
    assert "+a = 10" in out and "+b = 20" in out and out.count("@@ ") == 2


def test_no_diff_ablation(tmp_path, monkeypatch):
    monkeypatch.setenv("CAP_ABLATION", "no_diff")
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    out = ToolBox(tmp_path).execute("edit_file", {"path": "a.py", "old": "a = 1", "new": "a = 2"})["output"]
    assert "@@" not in out and "re-read" not in out


# ---------------------------------------------------------------- 4. write_file 내용 검사

def test_bad_json_new_file_refused(tmp_path):
    r = ToolBox(tmp_path).execute("write_file", {"path": "a.json", "content": "{bad"})
    assert not r["ok"] and "not changed" in r["output"] and not (tmp_path / "a.json").exists()
    assert ToolBox(tmp_path).check_write("a.json", "{bad")              # 승인 전 드라이런도 거부


def test_already_broken_json_may_be_rewritten_broken(tmp_path):
    (tmp_path / "a.json").write_text("{bad", encoding="utf-8")
    box = ToolBox(tmp_path)
    box.execute("read_file", {"path": "a.json"})
    assert box.execute("write_file", {"path": "a.json", "content": "{still bad"})["ok"]


def test_edit_breaking_valid_toml_is_refused(tmp_path):
    src = '[tool]\nname = "x"\n'
    (tmp_path / "p.toml").write_text(src, encoding="utf-8")
    box = ToolBox(tmp_path)
    r = box.execute("edit_file", {"path": "p.toml", "old": 'name = "x"', "new": 'name = "x'})
    assert not r["ok"] and "not changed" in r["output"]
    assert (tmp_path / "p.toml").read_text(encoding="utf-8") == src
    assert box.check_edit("p.toml", 'name = "x"', 'name = "x')


def test_numbered_content_is_stripped(tmp_path):
    r = ToolBox(tmp_path).execute("write_file", {"path": "m.py", "content": "1| def f():\n2|     return 1\n3| \n"})
    assert r["ok"] and (tmp_path / "m.py").read_text(encoding="utf-8") == "def f():\n    return 1\n\n"


def test_mostly_numbered_content_is_refused(tmp_path):
    content = "1| a = 1\n2| b = 2\n3| c = 3\nd = 4\n"
    r = ToolBox(tmp_path).execute("write_file", {"path": "m.py", "content": content})
    assert not r["ok"] and "not changed" in r["output"] and not (tmp_path / "m.py").exists()


def test_yaml_with_custom_tag_allowed(tmp_path):
    content = "Resources:\n  B:\n    Properties:\n      Name: !Sub '${AWS::StackName}-b'\n"
    r = ToolBox(tmp_path).execute("write_file", {"path": "t.yaml", "content": content})
    assert r["ok"] and (tmp_path / "t.yaml").exists()
    assert ToolBox(tmp_path).check_write("u.yml", "a: [1, 2\n")         # 진짜 문법 오류는 거부
