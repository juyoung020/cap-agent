"""plan.yaml → LangGraph StateGraph 로 컴파일해 재생. 승인·사람 넘김은 interrupt → 터미널 y/n.

체크포인터: SqliteSaver(.cap/checkpoints.sqlite) — `cap resume <id>` 로 다른 프로세스에서도 이어서.
code 노드는 노드 함수 안에서 코딩 루프 하위 그래프를 invoke(같은 체크포인터 상속, interrupt 전파).
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import operator
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import llm
from .evalcheck import (check_evaluator, collect_test_files, find_targets, frozen_hashes, mutation_after_green)
from .guards import check as guard_check
from .guards import compile_decisions
from .loop import build_loop
from .plan import main_chain, render_mermaid
from .policy import ALWAYS_HUMAN, append_only, classify_command
from .spec import Spec
from .store import Ledger, Shadow, cap_dir, iter_files, rel, sha256_file
from .tools import clip, run_shell

PRE_CATEGORIES = {"delete", "move", "git_rewrite", "kill", "cd_outside", "read_outside"}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a or {})
    out.update(b or {})
    return out


class RunState(TypedDict, total=False):
    results: Annotated[dict, _merge]
    iters: Annotated[dict, _merge]
    status: str           # running | blocked | stopped | done
    start_sha: str
    report: str


def meets_target(out: str, target: str) -> tuple[bool, float | None]:
    """측정 출력의 마지막 숫자(또는 `time` 의 real)를 '<= 0.5' 같은 기준과 비교."""
    m = re.match(r"\s*(<=|>=|<|>)\s*([-+]?\d*\.?\d+)", target or "")
    rt = re.search(r"real\s+(\d+)m([\d.]+)s", out)
    nums = [str(int(rt.group(1)) * 60 + float(rt.group(2)))] if rt else re.findall(r"[-+]?\d*\.\d+|[-+]?\d+", out)
    if not m or not nums:
        return False, None
    val, lim = float(nums[-1]), float(m.group(2))
    return {"<=": val <= lim, ">=": val >= lim, "<": val < lim, ">": val > lim}[m.group(1)], val


def _pytest_ratio(out: str, code: int) -> float:
    p = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    e = re.search(r"(\d+) error", out)
    passed = int(p.group(1)) if p else 0
    bad = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
    if passed + bad == 0:
        return 1.0 if code == 0 else 0.0
    return passed / (passed + bad)


def build_run_graph(*, ws: Path, plan: dict, sp: Spec, ledger: Ledger, printer: Callable[[str], None] = print):
    test_cmd = plan.get("test_cmd") or sp.test_cmd()
    frozen = set(collect_test_files(ws))
    decisions = [f"[{d.cat}] {d.text}" for d in sp.decisions]
    guards, _manual = compile_decisions(sp.decisions)
    guards += compile_decisions(sp.requirements)[0]   # "X import 하지 않음" 같은 요구사항도 같은 틀로 감시
    nodes = plan["nodes"]
    by_id = {n["id"]: n for n in nodes}
    chain = main_chain(plan)
    total = len(nodes)
    order = {n["id"]: i + 1 for i, n in enumerate(nodes)}
    shadow = Shadow(ws)

    printed: set[str] = set()

    def head(n: dict) -> None:
        # interrupt 재개 때 노드 함수가 다시 실행되므로 같은 프로세스에서는 머리줄을 한 번만
        ledger.node = n["id"]
        if n["id"] in printed:
            return
        printed.add(n["id"])
        printer(f"▶ [{order[n['id']]}/{total}] {n['id']} ({n['kind']})" +
                (f" — {n.get('goal')}" if n.get("goal") else ""))
        ledger.node = n["id"]
        ledger.log("node_start", kind=n["kind"])

    def done(n: dict, status: str, line: str, **extra) -> dict:
        mark = {"pass": "✔", "done": "✔", "skipped": "·", "fail": "✗", "blocked": "⛔", "stopped": "⛔"}.get(status, "·")
        printer(f"  {mark} {n['id']}: {line}")
        ledger.log("node_end", status=status, line=line, **extra)
        return {"results": {n["id"]: {"status": status, "line": line, **extra}}}

    # ------------------------------------------------------------ 노드 구현
    def make(n: dict):
        kind = n["kind"]

        def start_node(state: RunState) -> dict:
            if state.get("start_sha"):
                return {}
            sha = shadow.snapshot("run start")
            ledger.log("run_start", sha=sha, frozen={k: v for k, v in frozen_hashes(ws, sorted(frozen)).items()})
            return {"start_sha": sha or "", "status": "running"}

        if kind == "code":
            def fn(state: RunState, config) -> dict:
                head(n)
                pre = PRE_CATEGORIES if n.get("approve") == "pre" else set()
                inner = build_loop(ws=ws, ledger=ledger, test_cmd=test_cmd, frozen=frozen, preapproved=pre,
                                   decisions=decisions, requirements=[f"[{r.cat}] {r.text}" for r in sp.requirements], guards=guards,
                                   printer=printer)
                goal = n.get("goal", "")
                if n.get("module"):
                    goal += f" (module: {n['module']})"
                r = inner.invoke({"goal": goal}, config)
                st = r.get("status")
                extra = {"turns": r.get("turns"), "edits": r.get("edit_seq"), "changed": r.get("changed"),
                         "checkpoints": r.get("checkpoints"), "summary": (r.get("summary") or "")[:300]}
                if st == "done":
                    # 첫 초록불 뒤 돌연변이 점수(정보용, LLM 0회)
                    targets = [c for c in (r.get("changed") or []) if c.endswith(".py") and c not in frozen
                               and (ws / c).is_file()]
                    if targets:
                        printer("    첫 초록불 뒤 평가기 돌연변이 점수 측정…")
                        try:
                            ms = mutation_after_green(ws, test_cmd, targets, printer=lambda s: printer(s))
                            ledger.log("mutation_after_green", **ms)
                            extra["mutation"] = ms
                        except Exception as exc:  # noqa: BLE001 - 정보용이라 실행을 멈추지 않는다
                            ledger.log("mutation_error", error=str(exc)[:300])
                    out = done(n, "done", f"루프 완료 (턴 {r.get('turns')}, 편집 {r.get('edit_seq')}, "
                                          f"바뀐 파일 {', '.join(r.get('changed') or []) or '없음'})", **extra)
                    return out
                out = done(n, "stopped", f"루프 중단: {st} — {r.get('escalate_reason', '')}", **extra)
                out["status"] = "stopped"
                return out
        elif kind in ("test", "bench"):
            def fn(state: RunState, config) -> dict:
                head(n)
                cmd = n.get("cmd") or test_cmd
                d = classify_command(cmd, ws)
                if d.verdict == "deny":
                    return done(n, "fail", f"명령 거부: {d.reason}")
                t0 = time.perf_counter()
                code, out = run_shell(cmd, ws)
                dt = time.perf_counter() - t0
                ok = code == 0
                extra: dict[str, Any] = {"cmd": cmd, "exit": code, "seconds": round(dt, 2)}
                if kind == "bench" and ok and n.get("goal"):
                    ok, val = meets_target(out, n["goal"])
                    if val is not None:
                        extra["value"] = val
                ledger.log("test", source="harness", cmd=cmd, exit=code, passed=ok, output=clip(out, 1500))
                tail = out.strip().splitlines()[-1] if out.strip() else ""
                return done(n, "pass" if ok else "fail", f"{cmd} → {'통과' if ok else '실패'} ({tail[:80]})", **extra)
        elif kind == "eval_check":
            def fn(state: RunState, config) -> dict:
                head(n)
                rep = check_evaluator(ws, n.get("cmd") or test_cmd, printer=printer)
                ledger.log("eval_check", **rep.to_dict())
                (cap_dir(ws) / "frozen.json").write_text(json.dumps(frozen_hashes(ws, sorted(frozen)), indent=2),
                                                         encoding="utf-8")
                if rep.blocked:
                    out = done(n, "blocked", "평가기가 약하다 → 루프를 시작하지 않음: " + " / ".join(rep.reasons),
                               reasons=rep.reasons, cheats=rep.cheats, mutation=rep.mutation)
                    out["status"] = "blocked"
                    return out
                new_frozen = set(collect_test_files(ws)) - frozen
                frozen.update(new_frozen)      # 검증을 통과한 평가기(에이전트가 쓴 테스트 포함)는 이후 동결
                if new_frozen:
                    ledger.log("freeze", files=sorted(new_frozen))
                note = "; ".join(rep.reasons)
                return done(n, "pass", f"평가기 검증 통과 ({rep.seconds}s, 치트 {len(rep.cheats)}종 모두 탈락"
                                       f"{', 돌연변이 ' + format(rep.mutation.get('score', 0), '.0%') if rep.mutation.get('total') else ''})"
                                       + (f" — {note}" if note else "") + f", 평가기 동결: {len(frozen)}개 파일",
                            cheats=rep.cheats, mutation=rep.mutation)
        elif kind == "eval_loop":
            def fn(state: RunState, config) -> dict:
                head(n)
                it = (state.get("iters") or {}).get(n["id"], 0) + 1
                cmd = n.get("cmd") or test_cmd
                code, out = run_shell(cmd, ws)
                ratio = _pytest_ratio(out, code)
                ledger.log("test", source="harness", cmd=cmd, exit=code, passed=code == 0, ratio=ratio)
                thr = float(n.get("threshold", 1.0))
                if ratio >= thr:
                    return {**done(n, "pass", f"통과율 {ratio:.0%} ≥ {thr:.0%} (반복 {it})"), "iters": {n["id"]: it}}
                if it >= int(n.get("max_iters", 3)):
                    out_ = done(n, "fail", f"통과율 {ratio:.0%} < {thr:.0%}, 최대 반복 {it} 도달")
                    out_["status"] = "stopped"
                    return {**out_, "iters": {n["id"]: it}}
                for b in n.get("body", []):
                    printed.discard(b)
                printer(f"  ↻ {n['id']}: 통과율 {ratio:.0%} < {thr:.0%} → 다시 ({it}/{n.get('max_iters')})")
                return {"iters": {n["id"]: it}, "results": {n["id"]: {"status": "running", "line": f"반복 {it}"}}}
        elif kind == "cleanup":
            def fn(state: RunState, config) -> dict:
                head(n)
                start = state.get("start_sha") or ""
                changed = shadow.changed_files(start) if start else []
                removed, flagged = [], []
                tmp_rx = re.compile(r"(\.tmp|\.bak|\.orig|\.rej|~)$|(^|/)(tmp_|scratch|debug_)[^/]*$")
                for f in changed:
                    p = ws / f
                    if p.is_file() and tmp_rx.search(f) and not (shadow.show(start, f) if start else None):
                        p.unlink()
                        removed.append(f)
                    elif p.is_file() and f.endswith(".py"):
                        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        run_ = 0
                        for k, ln in enumerate(lines, 1):
                            if re.match(r"^\s*#\s*(def |return\b|import |from \w+ import|print\(|if |for |\w+\s*=[^=])", ln):
                                run_ += 1
                                if run_ == 2:
                                    flagged.append(f"{f}:{k - 1} 주석 처리된 코드")
                            else:
                                run_ = 0
                ledger.log("cleanup", removed=removed, flagged=flagged)
                line = (f"임시 파일 {len(removed)}개 삭제" if removed else "잔재 없음") + \
                       (f", 확인 필요 {len(flagged)}건: {'; '.join(flagged[:3])}" if flagged else "")
                return done(n, "pass" if not flagged else "fail", line, removed=removed, flagged=flagged)
        elif kind == "docs":
            def fn(state: RunState, config) -> dict:
                head(n)
                start = state.get("start_sha") or ""
                gone: list[str] = []
                for f in (shadow.changed_files(start) if start else []):
                    if not f.endswith(".py"):
                        continue
                    old = shadow.show(start, f) or ""
                    new = (ws / f).read_text(encoding="utf-8", errors="replace") if (ws / f).is_file() else ""
                    gone += sorted(_defs(old) - _defs(new))
                if gone:   # 다른 파일로 옮겨 간 심볼은 사라진 게 아니다
                    alive = set()
                    for p in iter_files(ws):
                        if p.suffix == ".py":
                            alive |= _defs(p.read_text(encoding="utf-8", errors="replace"))
                    gone = sorted(set(gone) - alive)
                stale = []
                if gone:
                    for p in iter_files(ws):
                        if p.suffix.lower() == ".md":
                            t = p.read_text(encoding="utf-8", errors="replace")
                            hits = [g for g in gone if re.search(rf"\b{re.escape(g)}\b", t)]
                            if hits:
                                stale.append(f"{rel(ws, p)} ({', '.join(hits)})")
                ledger.log("docs", removed_symbols=gone, stale=stale)
                if stale:
                    return done(n, "fail", f"갱신 필요: {'; '.join(stale)}", stale=stale)
                return done(n, "pass", "문서 갱신 필요 없음" + (f" (사라진 심볼 {gone})" if gone else ""))
        elif kind == "changelog":
            def fn(state: RunState, config) -> dict:
                head(n)
                start = state.get("start_sha") or ""
                changed = [f for f in (shadow.changed_files(start) if start else []) if f != "CHANGELOG.md"]
                if not changed:
                    return done(n, "skipped", "바뀐 파일 없음")
                recs = ledger.read()
                tests = [r for r in recs if r["type"] == "test"]
                last = tests[-1] if tests else None
                date = _dt.date.today().isoformat()
                goals = [x.get("goal") for x in nodes if x["kind"] == "code" and x.get("goal")]
                entry = [f"## [{date}] CAP run {ledger.run_id}", "", "### Changed"]
                entry += [f"- {g}" for g in goals] + [f"- 파일: {', '.join(changed)}"]
                if last:
                    entry += ["", "### Verified", f"- `{last['cmd']}` → {'통과' if last['passed'] else '실패'} "
                                                 f"(장부 {ledger.run_id})"]
                p = ws / "CHANGELOG.md"
                old = p.read_text(encoding="utf-8") if p.is_file() else "# Changelog\n"
                head_, _, rest = old.partition("\n")
                p.write_text(head_ + "\n\n" + "\n".join(entry) + "\n" + rest, encoding="utf-8")
                return done(n, "pass", f"CHANGELOG.md 갱신 ({len(changed)}개 파일)")
        elif kind == "git":
            def fn(state: RunState, config) -> dict:
                head(n)
                if not (ws / ".git").exists():
                    return done(n, "skipped", "작업 폴더가 별도 git 저장소가 아님 → 건너뜀")
                action = n.get("goal") or "commit"
                msg = f"CAP: {sp.title}"
                if n.get("approve") != "pre":
                    ans = interrupt({"kind": "approval", "calls": [{"tool": "git", "args": {"action": action},
                                                                     "reason": "커밋/푸시", "category": "git"}]})
                    if str(ans).lower() not in ("y", "yes", "a", "always"):
                        return done(n, "skipped", "사용자가 거부")
                import subprocess
                subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
                p1 = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ws, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                c1, o1 = p1.returncode, (p1.stdout or "") + (p1.stderr or "")
                line = "커밋 " + ("성공" if c1 == 0 else f"실패: {o1.strip()[:80]}")
                if c1 == 0 and "push" in action:
                    printer("    알림: git push 실행")
                    c2, o2 = run_shell("git push", ws)
                    line += ", 푸시 " + ("성공" if c2 == 0 else f"실패: {o2.strip()[:80]}")
                ledger.log("git", action=action, ok=c1 == 0)
                return done(n, "pass" if c1 == 0 else "fail", line)
        elif kind == "ask":
            def fn(state: RunState, config) -> dict:
                head(n)
                ans = interrupt({"kind": "ask", "question": n.get("goal", "확인해 주세요")})
                ok = str(ans).strip().lower() in ("y", "yes", "ok", "a", "")
                ledger.log("ask", question=n.get("goal"), answer=str(ans))
                return done(n, "pass" if ok else "fail", f"사람 확인: {ans}")
        else:  # pragma: no cover
            def fn(state: RunState, config) -> dict:
                return done(n, "skipped", "모르는 종류")
        return start_node, fn

    g = StateGraph(RunState)
    start_fn = None
    for n in nodes:
        s, fn = make(n)
        start_fn = start_fn or s
        g.add_node(n["id"], fn)
    g.add_node("start", start_fn or (lambda s: {}))
    g.add_node("report", lambda s: {"status": s.get("status") if s.get("status") in ("blocked", "stopped") else "done"})

    def entry(n: dict) -> str:
        return n["body"][0] if n["kind"] == "eval_loop" else n["id"]

    def stop_or(nxt: str):
        def route(state: RunState) -> str:
            return "report" if state.get("status") in ("blocked", "stopped") else nxt
        return route

    g.add_edge(START, "start")
    g.add_conditional_edges("start", stop_or(entry(chain[0]) if chain else "report"))
    for i, n in enumerate(chain):
        nxt = entry(chain[i + 1]) if i + 1 < len(chain) else "report"
        if n["kind"] == "eval_loop":
            body = n["body"]
            for a, b in zip(body, body[1:]):
                g.add_conditional_edges(a, stop_or(b))
            g.add_conditional_edges(body[-1], stop_or(n["id"]))

            def loop_route(state: RunState, _n=n, _nxt=nxt) -> str:
                if state.get("status") in ("blocked", "stopped"):
                    return "report"
                r = (state.get("results") or {}).get(_n["id"], {})
                return _n["body"][0] if r.get("status") == "running" else _nxt
            g.add_conditional_edges(n["id"], loop_route)
        else:
            g.add_conditional_edges(n["id"], stop_or(nxt))
    g.add_edge("report", END)
    return g


def _defs(src: str) -> set[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {x.name for x in tree.body if isinstance(x, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))}


# ---------------------------------------------------------------- 실행 드라이버

def checkpointer(ws: Path):
    conn = sqlite3.connect(str(cap_dir(ws) / "checkpoints.sqlite"), check_same_thread=False)
    return SqliteSaver(conn), conn


def meta_path(ws: Path, run_id: str) -> Path:
    return cap_dir(ws) / "runs" / f"{run_id}.meta.json"


def write_meta(ws: Path, run_id: str, **kv) -> dict:
    p = meta_path(ws, run_id)
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"run_id": run_id}
    data.update(kv)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def ask_human(payload: dict, auto: str | None, printer=print) -> str:
    kind = payload.get("kind")
    if kind == "approval":
        printer("  ⚠ 승인 필요:")
        for c in payload.get("calls", []):
            printer(f"     - {c['tool']} {json.dumps(c['args'], ensure_ascii=False)[:200]}  ({c['reason']})")
        prompt = "    허용? [y]예 / [a]이번 실행 동안 항상 / [n]아니오 > "
    elif kind == "escalate":
        printer(f"  ⛔ 사람에게 넘김: {payload.get('reason')}")
        out = (payload.get("output") or "").strip().splitlines()
        for ln in out[-12:]:
            printer("     | " + ln)
        prompt = "    힌트를 입력하면 이어서, 그냥 엔터/abort 면 중단 > "
        if auto is not None:
            printer(prompt + "abort (자동)")
            return "abort"
    else:
        printer(f"  ? {payload.get('question')}")
        prompt = "    [y]확인 / [n]아니오 > "
    if auto is not None:
        if kind == "approval" and any(c.get("category") in ALWAYS_HUMAN for c in payload.get("calls", [])):
            # 지침 파일 쓰기는 자동 응답으로 허용하지 않는다(사람 채널이 없으면 거부, Hermes 07 §5-3 fail-closed)
            printer(prompt + "n (자동 응답으로는 지침 파일 쓰기를 허용하지 않음)")
            return "n"
        printer(prompt + auto + " (자동)")
        return auto
    try:
        return input(prompt)
    except EOFError:
        return "n"


def drive(*, ws: Path, plan: dict, sp: Spec, run_id: str, resume: bool, auto: str | None,
          printer=print, pause: bool = False) -> dict:
    ledger = Ledger(ws, run_id)
    graph = build_run_graph(ws=ws, plan=plan, sp=sp, ledger=ledger, printer=printer)
    saver, conn = checkpointer(ws)
    app = graph.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 500}
    t0 = time.perf_counter()
    calls0 = llm.STATS["calls"]
    write_meta(ws, run_id, status="running", started=write_meta(ws, run_id).get("started") or
               _dt.datetime.now().isoformat(timespec="seconds"))
    inp: Any = None if resume else {"results": {}, "iters": {}, "status": "running"}
    try:
        while True:
            for _ in app.stream(inp, cfg, stream_mode="updates"):
                pass
            st = app.get_state(cfg)
            pend = [i for t in st.tasks for i in t.interrupts]
            if not pend:
                break
            write_meta(ws, run_id, status="waiting", waiting_for=pend[0].value)
            if pause:
                v = pend[0].value
                printer(f"  ⏸ 사람 응답 대기({v.get('kind')}): " +
                        (v.get("reason") or v.get("question") or
                         "; ".join(f"{c['tool']} {json.dumps(c['args'], ensure_ascii=False)[:120]} ({c['reason']})"
                                   for c in v.get("calls", []))))
                printer(f"  상태는 체크포인터(.cap/checkpoints.sqlite)에 저장됨. 이어서: cap resume {run_id}")
                return {"status": "waiting", "values": {}, "report": ""}
            answer = ask_human(pend[0].value, auto, printer)
            inp = Command(resume=answer)
        st = app.get_state(cfg)
        values = st.values
    finally:
        conn.close()
    dt = time.perf_counter() - t0
    report = build_report(ws, plan, sp, ledger, values, run_seconds=dt, calls=llm.STATS["calls"] - calls0)
    rp = cap_dir(ws) / "runs" / f"{run_id}.report.md"
    rp.write_text(report, encoding="utf-8")
    status = values.get("status", "done")
    write_meta(ws, run_id, status=status, finished=_dt.datetime.now().isoformat(timespec="seconds"),
               seconds=round(dt, 1), llm_calls=llm.STATS["calls"] - calls0)
    statuses = {k: v.get("status", "") for k, v in (values.get("results") or {}).items()}
    (cap_dir(ws) / "runs" / f"{run_id}.mmd").write_text(render_mermaid(plan, statuses), encoding="utf-8")
    printer("")
    printer(report)
    return {"status": status, "values": values, "report": report}


# ---------------------------------------------------------------- 보고 (장부만 근거)

def requirement_status(ws: Path, sp: Spec, ledger: Ledger, printer=print, run_missing: bool = True) -> list[dict]:
    recs = ledger.read()
    last_edit = max((i for i, r in enumerate(recs) if r["type"] == "tool" and r.get("changed")), default=-1)
    rows = []
    py_files = [rel(ws, p) for p in iter_files(ws) if p.suffix == ".py"]
    for r in sp.requirements:
        rg, _ = compile_decisions([r])
        if rg:   # 결정 감시자 틀에 맞는 요구사항은 테스트가 아니라 import 검사로 확인
            viol = guard_check(rg, ws, py_files)
            ledger.log("guard_check", requirement=r.id, rule=rg[0].describe(), violations=viol, node="report")
            rows.append({"id": r.id, "cat": r.cat, "text": r.text, "mark": "✗" if viol else "✔",
                         "why": f"결정 감시자 import 검사({rg[0].describe()}, .py {len(py_files)}개): "
                                + (", ".join(f"{v['file']}:{v['line']}" for v in viol) if viol else "위반 없음")})
            continue
        if not r.check:
            from .spec import RX_DOC_FILE
            m = RX_DOC_FILE.search(r.text)
            start_sha = next((x.get("sha") for x in ledger.read() if x["type"] == "run_start"), None)
            if m and start_sha:
                name = m.group(0).replace("\\", "/")
                changed = Shadow(ws).changed_files(start_sha)
                hit = [c for c in changed if c == name or c.endswith("/" + name) or Path(c).name.lower() == Path(name).name.lower()
                       or (Path(c).stem.upper() == name.upper())]
                rows.append({"id": r.id, "cat": r.cat, "text": r.text, "mark": "✔" if hit else "✗",
                             "why": (f"그림자 git: {', '.join(hit)} 이 이번 실행에서 바뀜(내용은 사람 확인)" if hit
                                     else f"그림자 git: {name} 이 이번 실행에서 안 바뀜")})
                continue
            rows.append({"id": r.id, "cat": r.cat, "text": r.text, "mark": "·", "why": "수동 확인 항목(자동 확인 명령 없음)"})
            continue
        cmd = r.check

        def matching(i_r):
            i, x = i_r
            return x["type"] == "test" and i > last_edit and _same_cmd(x.get("cmd", ""), cmd, sp, bool(x.get("passed")))
        ev = [x for x in enumerate(recs) if matching(x)]
        if not ev and run_missing:
            code, out = run_shell(cmd, ws)
            ok = code == 0
            if ok and r.target:
                ok, _ = meets_target(out, r.target)
            ledger.log("test", source="report", cmd=cmd, exit=code, passed=ok, output=clip(out, 800),
                       node="report")
            recs = ledger.read()
            ev = [x for x in enumerate(recs) if matching(x)]
        if not ev:
            rows.append({"id": r.id, "cat": r.cat, "text": r.text, "mark": "·", "why": "증거 없음"})
            continue
        i, x = ev[-1]
        ok = bool(x.get("passed"))
        rows.append({"id": r.id, "cat": r.cat, "text": r.text, "mark": "✔" if ok else "✗",
                     "why": f"장부 #{i + 1} {x.get('source')} `{x.get('cmd')}` exit {x.get('exit')} ({x.get('ts', '')[11:19]})"})
    return rows


def _same_cmd(a: str, b: str, sp: Spec, passed: bool = False) -> bool:
    """같은 명령이거나, 전체 pytest 실행이 통과했고 확인 명령이 pytest 부분 실행이면 증거로 인정."""
    na, nb = " ".join(a.split()), " ".join(b.split())
    if na == nb:
        return True
    full = " ".join(sp.test_cmd().split())
    return passed and na == full and "pytest" in nb


def build_report(ws: Path, plan: dict, sp: Spec, ledger: Ledger, values: dict, run_seconds: float,
                 calls: int) -> str:
    rows = requirement_status(ws, sp, ledger)
    results = values.get("results") or {}
    recs = ledger.read()
    llm_calls = sum(1 for r in recs if r["type"] == "llm_call")
    frozen_now = frozen_hashes(ws, collect_test_files(ws))
    start = next((r for r in recs if r["type"] == "run_start"), {})
    frozen_before = start.get("frozen", {})
    tampered = [f for f, h in frozen_before.items() if frozen_now.get(f) != h]
    appended = []
    for f in list(tampered):   # 새 테스트만 덧붙인 것은 변조가 아니다(기존 테스트 그대로)
        orig = Shadow(ws).show(start.get("sha"), f) if start.get("sha") else None
        cur = (ws / f).read_text(encoding="utf-8", errors="replace") if (ws / f).is_file() else None
        if orig is not None and cur is not None and append_only(orig, cur)[0]:
            tampered.remove(f)
            appended.append(f"{f} ({append_only(orig, cur)[1]})")
    status = values.get("status", "done")
    all_ok = all(r["mark"] == "✔" for r in rows if r["mark"] != "·" or "수동" not in r["why"]) and \
        status not in ("blocked", "stopped") and not tampered and not any(v.get("status") == "fail" for v in results.values())
    lines = [f"# CAP 실행 보고 — {ledger.run_id}", "",
             f"- 결과: **{ {'blocked': '중단(평가기 검증 실패)', 'stopped': '중단'}.get(status, '완료' if all_ok else '완료(미충족 항목 있음)') }**",
             f"- 걸린 시간: {run_seconds:.0f}초, 모델 호출: {llm_calls}회 (장부 기준)",
             f"- 평가기(테스트 파일) 무결성: {'변경 없음' if not tampered else '변경됨! ' + ', '.join(tampered)}"
             + (f" / 새 테스트 추가만: {', '.join(appended)}" if appended else ""),
             "", "## 노드", ""]
    for n in plan["nodes"]:
        r = results.get(n["id"])
        if r:
            lines.append(f"- `{n['id']}` ({n['kind']}): {r.get('status')} — {r.get('line', '')}")
        else:
            lines.append(f"- `{n['id']}` ({n['kind']}): 실행 안 함")
    lines += ["", "## 요구사항 (✔ 는 장부의 실행 기록이 마지막 편집 뒤에 있을 때만)", ""]
    for r in rows:
        lines.append(f"- {r['mark']} [{r['cat']}] {r['text']}  — {r['why']}")
    guards, manual = compile_decisions(sp.decisions)
    if sp.decisions:
        start_sha = start.get("sha")
        changed = Shadow(ws).changed_files(start_sha) if start_sha else []
        viol = guard_check(guards, ws, changed)
        hits = [r for r in recs if r["type"] == "decision_violation"]
        lines += ["", f"## 결정 (기계 검사 {len(guards)}/{len(sp.decisions)}줄, 실행 중 위반 감지 {len(hits)}회)", ""]
        gids = {g.decision_id: g for g in guards}
        for d in sp.decisions:
            if d.id in gids:
                bad = [v for v in viol if v["decision"] == d.id]
                mark = "✗" if bad else "✔"
                why = ("위반: " + ", ".join(f"{v['file']}:{v['line']} {v['import']}" for v in bad)) if bad else                     f"위반 없음 ({gids[d.id].describe()}, 바뀐 파일 {len(changed)}개 검사)"
            else:
                mark, why = "·", "틀에 안 맞음 → 사람 확인"
            lines.append(f"- {mark} [{d.cat}] {d.text}  — {why}")
    blocked = [v for v in results.values() if v.get("status") == "blocked"]
    for b in blocked:
        lines += ["", "## 평가기 검증 실패 이유", ""] + [f"- {x}" for x in b.get("reasons", [])]
        if b.get("mutation", {}).get("survived"):
            lines.append(f"- 살아남은 돌연변이: {b['mutation']['survived']}")
    lines += ["", f"장부: `.cap/runs/{ledger.run_id}.jsonl`"]
    return "\n".join(lines) + "\n"
