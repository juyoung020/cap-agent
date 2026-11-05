"""실사용(다른 프로젝트 사본에 실제 모델)에서 찾은 문제의 회귀 테스트."""
import json

from cap import compact as cmp
from cap import loop as L

PYTEST_OUT = """[python -m pytest -q → exit 1]
..F.F
=================================== FAILURES ===================================
____________________________ test_dead_six ____________________________

    def test_dead_six():
>       assert look(me, _K(0, 600, 6096, 0.0)).in_wez
E       assert False
E        +  where False = Engagement(ata=0.0, in_wez=False).in_wez

tests/test_wvr.py:42: AssertionError
""" + "x = 1\n" * 300 + """=========================== short test summary info ===========================
FAILED tests/test_wvr.py::test_dead_six - assert False
FAILED tests/test_wvr.py::test_the_vertical - assert not True
3 failed, 50 passed in 11.2s
"""


def _call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _turn(*rounds):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "fix the failing tests"}]
    for k, (name, args, out) in enumerate(rounds):
        cid = f"c{k}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [_call(cid, name, **args)]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
    return msgs


def test_failure_digest_keeps_what_failed_and_where():
    d = cmp.failure_digest(PYTEST_OUT)
    assert "FAILED tests/test_wvr.py::test_dead_six" in d and "E       assert False" in d
    assert "tests/test_wvr.py:42: AssertionError" in d and "x = 1" not in d


def test_pruned_test_output_keeps_failure_lines():
    long_read = "[engagement.py 줄 1-100 / 전체 240]\n" + "code\n" * 400
    msgs = _turn(("run_tests", {}, PYTEST_OUT),
                 ("run_tests", {}, PYTEST_OUT.replace("3 failed", "2 failed")),
                 ("read_file", {"path": "engagement.py"}, long_read),
                 ("read_file", {"path": "a.py"}, long_read),
                 ("read_file", {"path": "b.py"}, long_read))
    out = cmp.prune_turn(msgs, start=1, keep_rounds=2)
    first_test = out[3]["content"]                       # 첫 테스트 결과(오래된 것): 줄였지만 실패 줄은 남김
    assert first_test.startswith("[pruned] run_tests") and "FAILED tests/test_wvr.py::test_dead_six" in first_test
    assert out[5]["content"] == msgs[5]["content"]       # 가장 최근 테스트 결과는 그대로
    assert out[7]["content"].startswith("[pruned] read_file")
    assert cmp.normalize(out) == out


def test_pytest_through_run_counts_as_a_test():
    msgs = _turn(("run", {"cmd": "python -m pytest -q tests"}, PYTEST_OUT),
                 ("read_file", {"path": "a.py"}, "[a.py 줄 1-100 / 전체 240]\n" + "code\n" * 400),
                 ("read_file", {"path": "b.py"}, "[b.py 줄 1-100 / 전체 240]\n" + "code\n" * 400),
                 ("read_file", {"path": "c.py"}, "[c.py 줄 1-100 / 전체 240]\n" + "code\n" * 400))
    out = cmp.prune_turn(msgs, start=1, keep_rounds=2)
    assert out[3]["content"] == msgs[3]["content"]       # 이번 턴의 유일한(=최근) 테스트 결과
    assert out[5]["content"].startswith("[pruned] read_file")


def test_midturn_threshold_leaves_room_for_reading():
    assert L.MIDTURN_AT >= 0.7      # 16k 창에서 60% 는 시스템·도구 설명만으로 거의 차서 매번 압축했다


def test_testcmd_is_set_per_project_in_the_user_home(tmp_path):
    from cap import chat as C
    from cap import trust as TR
    ws = tmp_path / "proj"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    assert C.detect_test_cmd(ws) == "python -m pytest -q"
    TR.set_project_setting(ws, "test_cmd", "python -m pytest -q tests -x")
    assert C.detect_test_cmd(ws) == "python -m pytest -q tests -x"
    assert not (ws / ".cap" / "project_settings.json").exists()        # 저장소가 아니라 사용자 홈에
    out = []
    ui = C.UI(out=out.append)
    ui.tty = ui.color = False
    s = C.ChatSession(ws, None, "auto", ui)
    try:
        C.handle_command(s, ws, "/testcmd reset")
        assert C.detect_test_cmd(ws) == "python -m pytest -q" and any("test command" in ln for ln in out)
    finally:
        s.close()


def test_failed_test_output_starts_with_the_failure_summary():
    from cap import tools as T
    out = T.with_failures(PYTEST_OUT, 1)
    head = out.split("--- full output ---")[0]
    assert head.startswith("Failures (summary):") and "FAILED tests/test_wvr.py::test_dead_six" in head
    assert "E       assert False" in head and T.with_failures("ok", 0) == "ok"


def test_reading_the_same_file_again_and_again_gets_a_note():
    hist = []
    for k in range(3):
        hist.append({"key": f"read_file:{k}", "name": "read_file", "ok": True, "progress": False, "path": "eng.py"})
        warn, why = L.guard_check_after(hist)
    assert "read eng.py 3 times" in warn and not why
    hist.append({"key": "edit", "name": "edit_file", "ok": True, "progress": True})
    hist.append({"key": "read_file:9", "name": "read_file", "ok": True, "progress": False, "path": "eng.py"})
    assert "times in this request" not in L.guard_check_after(hist)[0]      # 고친 뒤에는 다시 셈


def test_pruned_read_keeps_a_map_of_definitions():
    body = "[eng.py 줄 100-170 / 전체 300]\n100| import math\n107| def _angle_to(a):\n115| def look(me, foe):\n" \
           + "".join(f"{k}|     x = {k}\n" for k in range(116, 170))
    short = cmp._prune_one("read_file", {"path": "eng.py"}, body)
    assert short.startswith("[pruned] read_file eng.py lines 100-170 of 300") and "_angle_to@107, look@115" in short


def test_search_in_one_file_with_no_match_suggests_the_whole_project(tmp_path):
    from cap import tools as T
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = T.ToolBox(tmp_path).execute("search", {"pattern": "def look", "path": "a.py"})["output"]
    assert "search the whole project" in out
    out = T.ToolBox(tmp_path).execute("search", {"pattern": "def look", "path": "."})["output"]
    assert "search the whole project" not in out
