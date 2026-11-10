import textwrap

from cap import evalcheck as E

MOD = textwrap.dedent('''
    def add(a, b):
        return a + b

    def clamp(x, lo, hi):
        if x < lo:
            return lo
        if x > hi:
            return hi
        return x
''')

STRONG = textwrap.dedent('''
    from mod import add, clamp

    def test_add():
        for a in range(-3, 4):
            for b in range(-3, 4):
                assert add(a, b) == a + b

    def test_clamp():
        for x in range(-5, 6):
            assert clamp(x, -2, 2) == max(-2, min(2, x))
''')

WEAK = textwrap.dedent('''
    from mod import add, clamp

    def test_add():
        assert add(2, 3) == 5

    def test_clamp():
        assert clamp(5, 0, 3) == 3
''')

VERY_WEAK = textwrap.dedent('''
    from mod import add, clamp

    def test_runs():
        add(1, 2)
''')

CMD = "python -m pytest -q -p no:cacheprovider"


def make(tmp_path, tests):
    (tmp_path / "mod.py").write_text(MOD, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n")
    (tmp_path / "tests" / "test_mod.py").write_text(tests, encoding="utf-8")
    return tmp_path


def quiet(_s):
    pass


def test_strong_tests_pass_check(tmp_path):
    ws = make(tmp_path, STRONG)
    rep = E.check_evaluator(ws, CMD, printer=quiet)
    assert rep.targets == ["mod.py"]
    assert not rep.blocked, rep.reasons
    assert all(v.startswith("탈락") for v in rep.cheats.values())
    assert rep.mutation["score"] >= 0.6


def test_lookup_cheat_catches_example_only_tests(tmp_path):
    ws = make(tmp_path, WEAK)
    rep = E.check_evaluator(ws, CMD, printer=quiet)
    assert rep.blocked
    assert rep.cheats["lookup"].startswith("통과")
    assert rep.cheats["const"].startswith("탈락")


def test_assert_free_tests_blocked(tmp_path):
    ws = make(tmp_path, VERY_WEAK)
    rep = E.check_evaluator(ws, CMD, printer=quiet)
    assert rep.blocked
    assert rep.cheats["const"].startswith("통과")


def test_original_untouched(tmp_path):
    ws = make(tmp_path, WEAK)
    E.check_evaluator(ws, CMD, printer=quiet)
    assert (ws / "mod.py").read_text(encoding="utf-8") == MOD


def test_assert_table(tmp_path):
    (tmp_path / "test_x.py").write_text(WEAK)
    t = E.assert_table(tmp_path, ["test_x.py"])
    assert t["add"][repr((2, 3))] == 5 and t["clamp"][repr((5, 0, 3))] == 3


def test_mutants_change_code():
    ms = E.mutants(MOD)
    ops = {m["op"] for m in ms}
    assert {"cmp", "return"} <= ops
    assert all(m["src"] != MOD for m in ms)


def test_no_tests_blocked(tmp_path):
    (tmp_path / "mod.py").write_text(MOD)
    rep = E.check_evaluator(tmp_path, CMD, printer=quiet)
    assert rep.blocked
