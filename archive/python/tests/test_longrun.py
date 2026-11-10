"""긴 작업(cap go): 단계 나누기·질문함·막힌 단계만 대기·회귀 되돌리기·테스트 먼저·마지막 재확인 (모델 호출 없음)."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from cap import longrun as LR
from cap.spec import parse

SPEC = """# 두 기능

## 요구사항
- [기능] add 가 더한다  ⟨check: python -m pytest -q -p no:cacheprovider tests/test_add.py⟩
- [기능] mul 이 곱한다  ⟨check: python -m pytest -q -p no:cacheprovider tests/test_mul.py⟩
- [산출물] README 사용법

## 결정
- [규칙] 표준 라이브러리만
"""
ADD_T = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
MUL_T = "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from cap import extras
    monkeypatch.setattr(extras, "desktop_notify", lambda *a, **k: False)


def project(tmp_path: Path, stages=None, preset="normal") -> tuple[Path, str]:
    ws = tmp_path
    (ws / ".cap").mkdir()
    (ws / "tests").mkdir()
    (ws / "tests" / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n")
    (ws / "tests" / "test_add.py").write_text(ADD_T)
    (ws / "tests" / "test_mul.py").write_text(MUL_T)
    (ws / "calc.py").write_text("def add(a, b):\n    return 0\n\ndef mul(a, b):\n    return 0\n")
    sp = parse(SPEC)
    stages = stages or [{"id": "s1", "title": "add", "requirements": ["R1"], "files": ["calc.py"], "after": []},
                        {"id": "s2", "title": "mul", "requirements": ["R2"], "files": ["calc.py"], "after": []}]
    rid = LR.create(ws, sp, SPEC, stages, preset=preset, hours=1, use_worktree=False)
    return ws, rid


def fix(ws: Path, name: str, body: str) -> None:
    t = (ws / "calc.py").read_text()
    head, _, rest = t.partition(f"def {name}(a, b):\n")
    line, _, tail = rest.partition("\n")
    (ws / "calc.py").write_text(head + f"def {name}(a, b):\n    {body}\n" + tail)


def fake_loop(behaviour):
    """behaviour(stage_id, call_no, goal) → 파일을 고친다. drive_loop 대신(모델 없음)."""
    calls: dict[str, int] = {}
    goals: list[str] = []

    def run(ctx, goal, *, test_cmd, frozen, allowed, stage_id, write_scope=None):
        calls[stage_id] = calls.get(stage_id, 0) + 1
        goals.append(goal)
        behaviour(ctx.ws, stage_id, calls[stage_id], goal)
        return {"status": "done", "reason": "", "denied": [], "changed": [], "calls": 1, "test_tail": [""]}
    run.calls, run.goals = calls, goals
    return run


def test_normalize_stages_fixes_model_output():
    sp = parse(SPEC)
    out = LR.normalize_stages([{"title": "a", "requirements": ["R1", "R9"], "files": ["x.py"], "after": [3]},
                               {"title": "dup", "requirements": ["R1"], "files": [], "after": []}], sp)
    assert [s["requirements"] for s in out] == [["R1"], ["R2"]]          # 없는 id·중복 제거, 빠진 R2 는 끝 단계로
    assert out[0]["after"] == [] and out[1]["after"] == ["s1"]           # 앞 단계만 가리킨다
    fb = LR.normalize_stages([], sp)
    assert fb and fb[0]["requirements"] == ["R1", "R2"]                  # 모델이 아무것도 못 내면 기본 분할


def test_runs_all_stages_and_rechecks_at_the_end(tmp_path, monkeypatch):
    ws, rid = project(tmp_path)
    monkeypatch.setattr(LR, "drive_loop", fake_loop(
        lambda w, sid, n, g: fix(w, "add" if sid == "s1" else "mul", "return a + b" if sid == "s1" else "return a * b")))
    res = LR.drive(ws, rid, printer=lambda s: None)
    st = res["values"]["stages"]
    assert st["s1"]["status"] == "done" and st["s2"]["status"] == "done"
    assert "요구사항 3개 중 **2개 통과**, 0개 실패, 1개 사람 확인" in res["report"]
    assert res["status"] == "finished"
    assert "✔9" not in LR.board_line(ws, rid) and "✔2 ✗0" in LR.board_line(ws, rid)
    assert (LR.run_dir(ws, rid) / "progress.md").is_file()


def test_stuck_stage_waits_in_inbox_while_others_finish(tmp_path, monkeypatch):
    ws, rid = project(tmp_path)
    loop = fake_loop(lambda w, sid, n, g: fix(w, "mul", "return a * b") if sid == "s2" else None)
    monkeypatch.setattr(LR, "drive_loop", loop)
    monkeypatch.setattr(LR, "split_steps", lambda ctx, s, notes: [{"file": "calc.py", "goal": "fix add"}])
    res = LR.drive(ws, rid, printer=lambda s: None)
    st = res["values"]["stages"]
    assert st["s1"]["status"] == "parked" and st["s2"]["status"] == "done"   # 막힌 단계 하나가 전체를 세우지 않는다
    assert loop.calls["s1"] == LR.MAX_ATTEMPTS
    assert "Previous attempt failed" in loop.goals[1] and "What went wrong before" in loop.goals[1]
    assert "In this step only: fix add" in loop.goals[2]                      # 3번째는 잘게 나눈 걸음
    qs = LR.open_questions(ws, rid)
    assert len(qs) == 1 and qs[0]["kind"] == "stuck" and res["status"] == "waiting"
    # 사람이 힌트를 주면 그 단계만 다시
    LR.answer(ws, rid, qs[0]["id"], "add 는 a + b 를 돌려줘")
    loop2 = fake_loop(lambda w, sid, n, g: fix(w, "add", "return a + b") if "a + b 를 돌려줘" in g else None)
    monkeypatch.setattr(LR, "drive_loop", loop2)
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["values"]["stages"]["s1"]["status"] == "done" and "s2" not in loop2.calls
    assert res["status"] == "finished" and "**2개 통과**" in res["report"]


def test_unanswered_question_takes_default_after_timeout(tmp_path):
    ws, rid = project(tmp_path)
    q = LR.ask(ws, rid, stage="s1", kind="stuck", question="?", options=["힌트 입력", "건너뛰기"], default="건너뛰기")
    assert LR.apply_timeouts(ws, rid, minutes=30, now=time.time() + 60) == []
    done = LR.apply_timeouts(ws, rid, minutes=30, now=time.time() + 31 * 60)
    assert done[0]["id"] == q["id"] and done[0]["answer"] == "건너뛰기" and done[0]["by"] == "timeout"
    assert LR.answer(ws, rid, "q1", "다시") is None                        # 이미 답한 질문


def test_approved_category_is_used_on_rerun(tmp_path, monkeypatch):
    ws, rid = project(tmp_path, stages=[{"id": "s1", "title": "add", "requirements": ["R1"], "files": [], "after": []}])
    seen = []

    def loop(ctx, goal, *, test_cmd, frozen, allowed, stage_id, write_scope=None):
        seen.append(set(allowed))
        if "install" in allowed:
            fix(ctx.ws, "add", "return a + b")
        else:
            LR.ask(ctx.home, ctx.rid, stage=stage_id, kind="approval", question="허락할까요? pip install x",
                   options=["예", "아니오"], default="아니오", data={"categories": ["install"]})
        return {"status": "done", "reason": "", "denied": [], "changed": [], "calls": 1, "test_tail": [""]}
    monkeypatch.setattr(LR, "drive_loop", loop)
    monkeypatch.setattr(LR, "split_steps", lambda *a: [])
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["values"]["stages"]["s1"]["status"] == "parked"
    approval = [q for q in LR.open_questions(ws, rid) if q["kind"] == "approval"][0]
    LR.answer(ws, rid, approval["id"], "1")                                   # 1 = 예
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["values"]["stages"]["s1"]["status"] == "done" and "install" in seen[-1]


def test_stage_that_breaks_an_earlier_one_is_rolled_back(tmp_path, monkeypatch):
    ws, rid = project(tmp_path, stages=[
        {"id": "s1", "title": "add", "requirements": ["R1"], "files": [], "after": []},
        {"id": "s2", "title": "mul", "requirements": ["R2"], "files": [], "after": ["s1"]}])

    def beh(w, sid, n, g):
        if sid == "s1":
            fix(w, "add", "return a + b")
        elif n == 1:                       # 첫 시도: mul 은 고쳤지만 add 를 깨뜨림
            fix(w, "mul", "return a * b")
            fix(w, "add", "return a - b")
        else:
            fix(w, "mul", "return a * b")
    loop = fake_loop(beh)
    monkeypatch.setattr(LR, "drive_loop", loop)
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["values"]["stages"]["s2"]["status"] == "done" and loop.calls["s2"] == 2
    assert "broke earlier stages" in loop.goals[-1]
    assert "a + b" in (ws / "calc.py").read_text()


def test_tests_are_written_first_then_locked(tmp_path, monkeypatch):
    ws, rid = project(tmp_path, stages=[{"id": "s1", "title": "add", "requirements": ["R1"], "files": [], "after": []}])
    (ws / "tests" / "test_add.py").unlink()
    frozen_seen = []

    def loop(ctx, goal, *, test_cmd, frozen, allowed, stage_id, write_scope=None):
        frozen_seen.append((set(write_scope or []), set(frozen)))
        if write_scope:                       # 테스트만 쓰는 걸음
            (ctx.ws / "tests" / "test_add.py").write_text(ADD_T)
        else:
            fix(ctx.ws, "add", "return a + b")
        return {"status": "done", "reason": "", "denied": [], "changed": [], "calls": 1, "test_tail": [""]}
    monkeypatch.setattr(LR, "drive_loop", loop)
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert frozen_seen[0][0] == {"tests/test_add.py"}                           # 먼저 테스트만
    assert "tests/test_add.py" in frozen_seen[1][1]                             # 구현 때는 잠김
    assert res["values"]["stages"]["s1"]["status"] == "done"


def test_final_report_rechecks_instead_of_trusting_stage_results(tmp_path):
    ws, rid = project(tmp_path)
    vals = {"stages": {"s1": {"status": "done", "line": "통과"}, "s2": {"status": "done", "line": "통과"}}, "status": "done"}
    rep = LR.final_report(ws, rid, parse(SPEC), vals, 10)
    assert "**0개 통과**, 2개 실패" in rep                                     # 단계가 'done' 이라도 지금 확인이 실패하면 ✗


def test_deadline_stops_and_reports(tmp_path, monkeypatch):
    ws, rid = project(tmp_path)
    cfg = LR.load_run(ws, rid)
    cfg["deadline"] = time.time() - 1
    LR.save_run(ws, rid, cfg)
    monkeypatch.setattr(LR, "drive_loop", fake_loop(lambda *a: None))
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["values"]["status"] == "deadline" and "기한이 되어 멈춤" in res["report"]


def test_phone_buttons_and_replies(tmp_path, monkeypatch):
    ws, rid = project(tmp_path)
    from cap import compat
    monkeypatch.setattr(compat, "settings", lambda: {"notify": {"ntfy": "https://ntfy.sh/out",
                                                                "ntfy_reply": "https://ntfy.sh/in"}})
    sent = []

    class R:
        def __init__(self, body=b""):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=10):
        if isinstance(req, str):           # 답장 읽기
            msg = {"event": "message", "id": "m1", "message": f"{rid} q1 1"}
            return R((json.dumps(msg) + "\n").encode())
        sent.append(req)
        return R()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    LR.ask(ws, rid, stage="s1", kind="approval", question="허락할까요? " + "sk-" + "abcdef" * 4,
           options=["예", "아니오"], default="아니오")
    req = sent[0]
    assert "sk-abcdef" not in req.data.decode()                                # 키 모양은 가린다
    assert f"body={rid} q1 1" in req.headers["Actions"]
    got = LR.poll_replies(ws, rid)
    assert got[0]["answer"] == "예" and got[0]["by"] == "phone"


def test_commit_per_stage_on_work_branch(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws)
    (ws / ".gitignore").write_text(".cap/\n")
    w2, _ = project(ws)
    subprocess.run(["git", "add", "-A"], cwd=ws)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws)
    rid = LR.create(ws, parse(SPEC), SPEC, LR.load_run(ws, LR.latest(ws))["stages"], hours=1)
    cfg = LR.load_run(ws, rid)
    assert cfg["branch"] == f"cap/go-{rid}" and Path(cfg["worktree"]).is_dir()
    monkeypatch.setattr(LR, "drive_loop", fake_loop(
        lambda w, sid, n, g: fix(w, "add" if sid == "s1" else "mul", "return a + b" if sid == "s1" else "return a * b")))
    res = LR.drive(ws, rid, printer=lambda s: None)
    assert res["status"] == "finished"
    log = subprocess.run(["git", "log", "--oneline", cfg["branch"]], cwd=ws, capture_output=True, text=True,
                         encoding="utf-8").stdout
    assert "CAP 단계 s1" in log and "CAP 단계 s2" in log
    assert "return 0" in (ws / "calc.py").read_text()                          # 원래 폴더는 그대로
    subprocess.run(["git", "worktree", "remove", "--force", cfg["worktree"]], cwd=ws)


def test_go_dry_shows_summary(tmp_path, monkeypatch):
    from cap import cli
    from cap import spec as specmod
    (tmp_path / ".cap").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(specmod, "draft", lambda task, ws, ledger=None: (parse(SPEC), []))
    monkeypatch.setattr(LR, "plan_stages", lambda sp, ws, ledger=None: LR.normalize_stages([], sp))
    out = []
    monkeypatch.setattr(cli, "_out", lambda s="": out.append(s))
    assert cli.main(["go", "두 기능", "--dry", "--yes"]) == 0
    text = "\n".join(out)
    assert "요구사항 3개 (자동 확인 2개) · 단계 1개 · 보통" in text and "--dry" in text


def test_requirement_naming_several_test_files_is_split():
    sp = parse("# t\n\n## 요구사항\n- [기능] 전부  ⟨check: python -m pytest -q tests/test_a.py tests/test_b.py⟩\n"
               "- [성능] 빠름  ⟨check: python bench.py⟩\n")
    notes = LR.split_requirements(sp)
    assert [r.id for r in sp.requirements] == ["R1", "R2", "R3"]
    assert sp.requirements[0].check == "python -m pytest -q tests/test_a.py"
    assert sp.requirements[1].check == "python -m pytest -q tests/test_b.py" and sp.requirements[1].text.startswith("[b]")
    assert sp.requirements[2].check == "python bench.py" and notes == ["R1 를 테스트 파일 2개로 나눔"]


def test_bare_pytest_checks_get_their_own_test_file_when_no_tests_exist(tmp_path):
    sp = parse("# t\n\n## 요구사항\n- [기능] 하나  ⟨check: python -m pytest -q⟩\n- [기능] 둘\n"
               "- [성능] 빠름  ⟨check: python -m pytest -q⟩\n")
    notes = LR.assign_test_files(sp, tmp_path)
    assert [r.check for r in sp.requirements] == ["python -m pytest -q tests/test_r1.py",
                                                  "python -m pytest -q tests/test_r2.py", "python -m pytest -q"]
    assert len(notes) == 2
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    sp2 = parse("# t\n\n## 요구사항\n- [기능] 하나  ⟨check: python -m pytest -q⟩\n")
    assert LR.assign_test_files(sp2, tmp_path) == [] and sp2.requirements[0].check == "python -m pytest -q"
