"""plan → StateGraph 컴파일·재생(모델 호출 없는 노드만)."""

from pathlib import Path

from cap import runner
from cap.spec import parse

SPEC = "# t\n\n## 요구사항\n- [기능] 통과  ⟨check: python -m pytest -q⟩\n\n## 결정\n- [규칙] numpy import 금지\n"


def ws_with(tmp_path: Path) -> Path:
    (tmp_path / ".cap" / "runs").mkdir(parents=True)
    return tmp_path


def run(ws, plan, auto="y"):
    return runner.drive(ws=ws, plan=plan, sp=parse(SPEC), run_id="r1", resume=False, auto=auto,
                        printer=lambda s: None)


def test_eval_loop_repeats_until_threshold(tmp_path):
    ws = ws_with(tmp_path)
    (ws / "bump.py").write_text("import pathlib\np = pathlib.Path('n.txt')\n"
                                "p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\n")
    (ws / "check.py").write_text("import pathlib, sys\nsys.exit(0 if int(pathlib.Path('n.txt').read_text()) >= 2 else 1)\n")
    plan = {"test_cmd": "python check.py", "nodes": [
        {"id": "bump", "kind": "test", "cmd": "python bump.py"},
        {"id": "loop", "kind": "eval_loop", "body": ["bump"], "cmd": "python check.py", "threshold": 1.0,
         "max_iters": 5}]}
    res = run(ws, plan)
    assert res["status"] == "done"
    assert (ws / "n.txt").read_text() == "2"          # 몸통이 두 번 돌았다
    assert res["values"]["iters"]["loop"] == 2


def test_eval_loop_gives_up_after_max_iters(tmp_path):
    ws = ws_with(tmp_path)
    plan = {"test_cmd": "python -c \"import sys; sys.exit(1)\"", "nodes": [
        {"id": "noop", "kind": "test", "cmd": "python -c \"print(1)\""},
        {"id": "loop", "kind": "eval_loop", "body": ["noop"], "threshold": 1.0, "max_iters": 2}]}
    res = run(ws, plan)
    assert res["status"] == "stopped"
    assert res["values"]["iters"]["loop"] == 2


def test_weak_evaluator_blocks_before_code(tmp_path):
    ws = ws_with(tmp_path)
    (ws / "m.py").write_text("def f(x):\n    return x\n")
    (ws / "tests").mkdir()
    (ws / "tests" / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n")
    (ws / "tests" / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f(3) == 9\n")
    plan = {"test_cmd": "python -m pytest -q -p no:cacheprovider", "nodes": [
        {"id": "check", "kind": "eval_check"},
        {"id": "fix", "kind": "code", "goal": "square"}]}   # code 는 실행되면 안 된다(모델 호출 없음)
    res = run(ws, plan)
    assert res["status"] == "blocked"
    assert "fix" not in res["values"]["results"]
    assert "외운 표" in res["report"]


def test_ask_node_interrupt_auto_answer(tmp_path):
    ws = ws_with(tmp_path)
    plan = {"test_cmd": "python -c \"print(1)\"", "nodes": [{"id": "q", "kind": "ask", "goal": "확인?"}]}
    res = run(ws, plan, auto="y")
    assert res["values"]["results"]["q"]["status"] == "pass"


def test_git_node_commits_in_own_repo(tmp_path):
    import subprocess
    ws = ws_with(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=ws)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws)
    (ws / "a.txt").write_text("hi")
    plan = {"test_cmd": "python -c \"print(1)\"", "nodes": [{"id": "git", "kind": "git", "goal": "commit"}]}
    res = run(ws, plan, auto="y")
    assert res["values"]["results"]["git"]["status"] == "pass"
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
    assert "CAP:" in log
