"""헛도는 편집 줄이기: 바꿀 것이 없는 편집, 한 줄 바꾸려고 여러 줄을 다시 써 보낸 편집, 같은 내용을 다시 쓴 write_file.

실제 기록(`eval/results/traces/bench_fix1/dedup-cap-r0.jsonl`)에서 나온 경우들이다: import 한 줄을 바꾸려고 파일 거의 전체를
old·new 로 보낸 편집, old 와 new 가 같은 항목 하나 때문에 통째로 거부된 5개 묶음 편집."""

from cap import tools as T
from cap.tools import ToolBox, apply_edit, apply_edit_args, trim_edit
from test_chat import call, script, session, ws  # noqa: F401

BODY = "\n".join(f"    line_{k} = {k}" for k in range(1, 13))
SRC = f"from .common import format_record, escape\n\n\ndef to_json(rows):\n{BODY}\n    return rows\n"


def edit(tmp_path, args, text=SRC, name="a.py", **kw):
    (tmp_path / name).write_text(text, encoding="utf-8", newline="")
    box = ToolBox(tmp_path, **kw)
    return box, box.execute("edit_file", {"path": name, **args})


# ---------------------------------------------------------------- 1. 바꿀 것이 없는 편집

def test_noop_edit_says_nothing_changed_without_hint(tmp_path):
    box, r = edit(tmp_path, {"old": "def to_json(rows):", "new": "def to_json(rows):"})
    assert not r["ok"] and r["stage"] == "noop" and r["changed"] == []
    assert r["output"] == T.NOOP_EDIT
    assert "Closest lines" not in r["output"]
    assert box.edit_fails == {}                       # 찾기 실패가 아니므로 '3번째 실패' 안내로 이어지지 않는다
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == SRC


def test_noop_probe_before_approval_has_no_hint(tmp_path):
    (tmp_path / "a.py").write_text(SRC, encoding="utf-8")
    assert ToolBox(tmp_path).check_edit("a.py", "def to_json(rows):", "def to_json(rows):") == T.NOOP_EDIT


def test_batch_skips_identical_item_and_applies_the_rest(tmp_path):
    edits = [{"old": "    line_1 = 1", "new": "    line_1 = 10"},
             {"old": "    line_2 = 2", "new": "    line_2 = 2"},
             {"old": "    line_3 = 3", "new": "    line_3 = 30"}]
    _, r = edit(tmp_path, {"edits": edits})
    assert r["ok"] and r["stage"] == "3 edits: exact,same,exact"
    assert "Skipped edit #2 of 3" in r["output"]
    text = (tmp_path / "a.py").read_text(encoding="utf-8")
    assert "line_1 = 10" in text and "line_2 = 2\n" in text and "line_3 = 30" in text


def test_batch_real_failure_is_still_all_or_nothing(tmp_path):
    edits = [{"old": "    line_1 = 1", "new": "    line_1 = 10"},
             {"old": "    line_2 = 2", "new": "    line_2 = 2"},
             {"old": "no such line here", "new": "x"}]
    _, r = edit(tmp_path, {"edits": edits})
    assert not r["ok"] and "edit #3 of 3 failed (nothing was changed)" in r["output"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == SRC


def test_batch_of_only_identical_items_is_a_noop():
    r = apply_edit_args(SRC, {"edits": [{"old": "    line_1 = 1", "new": "    line_1 = 1"},
                                        {"old": "    line_2 = 2", "new": "    line_2 = 2"}]})
    assert not r.ok and r.stage == "noop" and "All 2 edits" in r.message


def test_batch_identical_plus_already_applied_changes_nothing(tmp_path):
    edits = [{"old": "    line_1 = 1", "new": "    line_1 = 1"},
             {"old": "    line_99 = 0", "new": "    return rows"}]
    _, r = edit(tmp_path, {"edits": edits})
    assert r["ok"] and r["stage"] == "already_applied" and r["changed"] == []
    assert "Skipped edit #1 of 2" in r["output"]


# ---------------------------------------------------------------- 2. 여러 줄을 다시 써 보낸 편집

WHOLE_OLD = SRC.rstrip("\n")
WHOLE_NEW = WHOLE_OLD.replace("import format_record, escape", "import escape")


def test_trim_finds_the_one_changed_line():
    after = apply_edit(SRC, WHOLE_OLD, WHOLE_NEW).new_text
    n_old, n_new, changed, so, sn = trim_edit(SRC, WHOLE_OLD, WHOLE_NEW, after)
    assert (n_old, n_new, changed) == (17, 17, 1)
    assert so == "from .common import format_record, escape" and sn == "from .common import escape"
    assert apply_edit(SRC, so, sn).new_text == after          # 보여 주는 짧은 편집은 같은 결과를 낸다


def test_whole_file_echo_gets_a_note_with_the_short_edit(tmp_path):
    box, r = edit(tmp_path, {"old": WHOLE_OLD, "new": WHOLE_NEW})
    assert r["ok"] and r["changed"] == ["a.py"]
    out = r["output"]
    assert out.startswith("편집 적용(exact)")                 # 맥락 압축이 첫 줄로 '적용됨'을 알아본다
    assert "[harness] This edit sent 17 lines of old and 17 lines of new to change 1 line." in out
    assert "old:\n```\nfrom .common import format_record, escape\n```" in out
    assert "new:\n```\nfrom .common import escape\n```" in out
    assert box.echo_notes == 1


def test_small_edit_gets_no_note(tmp_path):
    box, r = edit(tmp_path, {"old": "from .common import format_record, escape", "new": "from .common import escape"})
    assert r["ok"] and "[harness]" not in r["output"] and box.echo_notes == 0


def test_block_where_most_lines_change_gets_no_note(tmp_path):
    new = "\n".join(f"    line_{k} = {k * 2}" for k in range(1, 13))
    _, r = edit(tmp_path, {"old": BODY, "new": new})
    assert r["ok"] and "[harness]" not in r["output"]


def test_short_edit_widens_until_unique():
    # 바뀌는 줄이 파일에 두 번 있으면 앞뒤 줄을 붙여 고유해진 것만 보여 준다
    blk = "\n".join(f"    keep_{k} = {k}" for k in range(1, 6))
    text = f"def a():\n{blk}\n    x = 1\n    return x\n\n\ndef b():\n    x = 1\n    return 0\n"
    old = f"def a():\n{blk}\n    x = 1\n    return x"
    new = old.replace("    x = 1", "    x = 2")
    after = apply_edit(text, old, new).new_text
    _, _, changed, so, sn = trim_edit(text, old, new, after)
    assert changed == 1 and so == "    keep_5 = 5\n    x = 1\n    return x"
    assert apply_edit(text, so, sn).new_text == after


def test_inserted_lines_keep_an_anchor():
    old = WHOLE_OLD
    new = old.replace("    line_6 = 6\n", "    line_6 = 6\n    extra = 0\n")
    after = apply_edit(SRC, old, new).new_text
    _, _, changed, so, sn = trim_edit(SRC, old, new, after)
    assert changed == 1 and so == "    line_6 = 6\n    line_7 = 7" and sn == "    line_6 = 6\n    extra = 0\n    line_7 = 7"


def test_no_note_when_the_long_old_was_needed():
    # 같은 덩어리가 두 번 있어 긴 old 로만 고유해지는 경우: 줄일 수 없으므로 안내하지 않는다
    blk = "\n".join("    v = 0" for _ in range(9))
    text = f"def a():\n{blk}\n    return 1\n\n\ndef b():\n{blk}\n    return 1\n"
    old = f"def a():\n{blk}\n    return 1"
    new = f"def a():\n{blk}\n    return 2"
    after = apply_edit(text, old, new).new_text
    assert after is not None and trim_edit(text, old, new, after) is None


def test_batch_note_names_the_most_wasteful_item(tmp_path):
    later = lambda x: x.replace("line_12 = 12", "line_12 = 120")   # noqa: E731 — 첫 항목이 적용된 뒤의 글
    edits = [{"old": "    line_12 = 12", "new": "    line_12 = 120"},
             {"old": later(WHOLE_OLD), "new": later(WHOLE_NEW)}]
    _, r = edit(tmp_path, {"edits": edits})
    assert r["ok"] and "This edit sent 17 lines of old" in r["output"]
    assert "from .common import escape\n" in (tmp_path / "a.py").read_text(encoding="utf-8")


def test_note_is_capped_per_request_and_can_be_switched_off(tmp_path, monkeypatch):
    box, r = edit(tmp_path, {"old": WHOLE_OLD, "new": WHOLE_NEW}, echo_notes=T.ECHO_NOTES)
    assert r["ok"] and "[harness]" not in r["output"] and box.echo_notes == T.ECHO_NOTES
    monkeypatch.setenv("CAP_ABLATION", "no_echo")
    box, r = edit(tmp_path, {"old": WHOLE_OLD, "new": WHOLE_NEW})
    assert r["ok"] and "[harness]" not in r["output"] and box.echo_notes == 0


def test_crlf_file_still_gets_a_verified_short_edit(tmp_path):
    crlf = SRC.replace("\n", "\r\n")
    _, r = edit(tmp_path, {"old": WHOLE_OLD, "new": WHOLE_NEW}, text=crlf)
    assert r["ok"] and "old:\n```\nfrom .common import format_record, escape\n```" in r["output"]
    assert (tmp_path / "a.py").read_bytes().decode("utf-8") == crlf.replace("import format_record, escape", "import escape")


# ---------------------------------------------------------------- 3. write_file

LONG = "\n".join(f"v{k} = {k}" for k in range(30)) + "\n"


def write(tmp_path, content, before=LONG, **kw):
    (tmp_path / "w.py").write_text(before, encoding="utf-8", newline="")
    box = ToolBox(tmp_path, full_reads={"w.py"}, **kw)
    return box, box.execute("write_file", {"path": "w.py", "content": content})


def test_writing_the_same_content_reports_no_change(tmp_path):
    _, r = write(tmp_path, LONG)
    assert r["ok"] and r["changed"] == [] and "already has exactly this content" in r["output"]


def test_rewriting_a_long_file_for_one_line_gets_a_note(tmp_path):
    box, r = write(tmp_path, LONG.replace("v7 = 7", "v7 = 70"))
    assert r["ok"] and r["changed"] == ["w.py"] and r["output"].startswith("w.py 저장(")
    assert "[harness] This write_file sent all 30 lines of w.py to change 1." in r["output"]
    assert box.echo_notes == 1


def test_real_rewrite_and_short_file_get_no_note(tmp_path):
    _, r = write(tmp_path, "\n".join(f"w{k} = {k}" for k in range(30)) + "\n")
    assert r["ok"] and "[harness]" not in r["output"]
    _, r = write(tmp_path, "a = 2\nb = 2\n", before="a = 1\nb = 2\n")
    assert r["ok"] and "[harness]" not in r["output"]


def test_no_rewrite_note_after_edit_failures_sent_the_model_to_write_file(tmp_path):
    # 편집이 세 번 실패하면 하네스가 write_file 로 통째로 바꾸라고 한다 — 그 뒤에 '고쳐 써라'고 하면 말이 엇갈린다
    _, r = write(tmp_path, LONG.replace("v7 = 7", "v7 = 70"), edit_fails={"w.py": 3})
    assert r["ok"] and "[harness]" not in r["output"]


# ---------------------------------------------------------------- 4. 대화 루프: 횟수는 요청 안에서 이어지고 새 요청에서 0 으로

def big(n):
    return "\n".join([f"top = {n}"] + [f"k{k} = {k}" for k in range(12)])


def harness_notes(row):
    return [m["content"] for m in row if m["role"] == "tool" and "[harness] This edit sent" in m["content"]]


def test_count_carries_within_a_request_and_resets_on_the_next(ws, monkeypatch):
    monkeypatch.setattr(T, "ECHO_NOTES", 1)
    (ws / "big.py").write_text(big(0) + "\n", encoding="utf-8")
    step = lambda a, b: {"tool_calls": [call("edit_file", path="big.py", old=big(a), new=big(b))]}  # noqa: E731
    seen = script(monkeypatch, [step(0, 1), step(1, 2), {"content": "끝."}, {"content": "끝."},
                                step(2, 3), {"content": "끝."}, {"content": "끝."}])
    s, _ = session(ws, approval="auto")
    s.run_turn("top 을 바꿔줘", ask=lambda p: "abort")
    first = harness_notes(seen[1])
    assert len(first) == 1 and "to change 1 line." in first[0] and "old:\n```\ntop = 0\n```" in first[0]
    assert len(harness_notes(seen[2])) == 1            # 두 번째 편집에는 붙지 않는다(앞의 것 하나뿐)
    n_before = len(seen)
    s.run_turn("한 번 더", ask=lambda p: "abort")
    assert any("top = 2" in c for c in harness_notes(seen[n_before + 1]))   # 새 요청에서는 다시 알려 준다
    assert (ws / "big.py").read_text(encoding="utf-8").startswith("top = 3")
    s.close()
