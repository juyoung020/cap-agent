"""계획 = 그래프. `.cap/plan.yaml` 생성(AI, json_schema 강제)·검증·자동 부착·글자/Mermaid 표시·말로 수정."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import llm
from .spec import Spec
from .store import cap_dir

KINDS = ["code", "test", "bench", "eval_check", "eval_loop", "docs", "cleanup", "changelog", "git", "ask"]
AUTO_KINDS = {"eval_check", "test", "bench", "ask", "cleanup", "docs", "changelog", "git"}
MAX_NODES = 20
ICON = {"code": "✎", "test": "✓", "bench": "⏱", "eval_check": "⚖", "eval_loop": "↻", "docs": "📄",
        "cleanup": "🧹", "changelog": "📝", "git": "⎇", "ask": "?"}

NODE_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"},
    "kind": {"type": "string", "enum": KINDS},
    "goal": {"type": "string"},
    "module": {"type": "string"},
    "cmd": {"type": "string"},
    "approve": {"type": "string", "enum": ["pre", "ask", "never"]},
    "body": {"type": "array", "items": {"type": "string"}},
    "threshold": {"type": "number"},
    "max_iters": {"type": "integer"},
}, "required": ["id", "kind", "goal", "module", "cmd", "approve", "body", "threshold", "max_iters"],
    "additionalProperties": False}
PLAN_SCHEMA = {"type": "object", "properties": {"nodes": {"type": "array", "items": NODE_SCHEMA}},
               "required": ["nodes"], "additionalProperties": False}

PLAN_PROMPT = """You turn a locked spec into an execution graph (JSON). Nodes run top to bottom.
Node kinds (use only these):
- code: implement/fix one module. goal = concrete change in one sentence (English or Korean). module = file stem.
- test: run a check command (cmd). - bench: run a measure command (cmd).
- eval_loop: repeat the nodes listed in body until the test pass ratio >= threshold, at most max_iters times.
  The body nodes stay in the list BEFORE the eval_loop node. Example:
  {"id":"fix","kind":"code",...}, {"id":"t","kind":"test","cmd":"python -m pytest -q",...},
  {"id":"loop","kind":"eval_loop","body":["fix","t"],"threshold":1.0,"max_iters":3,...}
- docs, cleanup, changelog: housekeeping. - git: commit (goal "commit" or "commit_push"). - ask: ask the human (goal = question).
Usually: one code node per module that must change, then a test node. Keep it small (2-8 nodes).
Unused fields: "" / [] / 0. approve: "ask" unless the user said otherwise. ids: short lowercase words."""


def plan_path(ws: Path) -> Path:
    return cap_dir(ws) / "plan.yaml"


def _clean_node(n: dict) -> dict:
    out = {"id": re.sub(r"[^a-z0-9_]", "_", str(n.get("id", "")).strip().lower()) or "n",
           "kind": n.get("kind")}
    for k in ("goal", "module", "cmd"):
        v = (n.get(k) or "").strip() if isinstance(n.get(k), str) else ""
        if v:
            out[k] = v
    out["approve"] = n.get("approve") if n.get("approve") in ("pre", "ask", "never") else "ask"
    if out["kind"] == "eval_loop":
        out["body"] = [str(b) for b in (n.get("body") or [])]
        out["threshold"] = float(n.get("threshold") or 1.0)
        out["max_iters"] = int(n.get("max_iters") or 3)
    if n.get("auto"):
        out["auto"] = True
    return out


def validate(plan: dict) -> list[str]:
    errs = []
    nodes = plan.get("nodes", [])
    if not nodes:
        errs.append("노드가 없다")
    if len(nodes) > MAX_NODES:
        errs.append(f"노드가 {MAX_NODES}개를 넘는다")
    ids = [n.get("id") for n in nodes]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errs.append(f"중복 id: {sorted(dup)}")
    for n in nodes:
        if n.get("kind") not in KINDS:
            errs.append(f"{n.get('id')}: 허용되지 않은 종류 {n.get('kind')}")
        if n.get("kind") == "code" and not n.get("goal"):
            errs.append(f"{n['id']}: code 노드에 goal 이 없다")
        if n.get("kind") in ("test", "bench") and not n.get("cmd") and not plan.get("test_cmd"):
            errs.append(f"{n['id']}: 명령(cmd)이 없다")
        if n.get("kind") == "eval_loop":
            body = n.get("body") or []
            if not body:
                errs.append(f"{n['id']}: eval_loop 에 body 가 없다")
            for b in body:
                if b not in ids:
                    errs.append(f"{n['id']}: body 의 {b} 가 없다")
                elif ids.index(b) > ids.index(n["id"]):
                    errs.append(f"{n['id']}: body 노드 {b} 는 루프보다 앞에 있어야 한다")
    return errs


def _uniq_id(nodes: list[dict], base: str) -> str:
    ids = {n["id"] for n in nodes}
    if base not in ids:
        return base
    k = 2
    while f"{base}{k}" in ids:
        k += 1
    return f"{base}{k}"


def attach(plan: dict, sp: Spec, has_tests: bool = True) -> tuple[dict, list[str]]:
    """자동 부착 규칙(코드가 강제). removed_by_user 에 있는 종류는 다시 붙이지 않는다."""
    plan = copy.deepcopy(plan)
    nodes: list[dict] = plan["nodes"]
    removed = set(plan.get("removed_by_user", []))
    notes: list[str] = []
    test_cmd = plan.get("test_cmd") or sp.test_cmd()
    plan["test_cmd"] = test_cmd
    has_code = any(n["kind"] == "code" for n in nodes)
    body_ids = {b for n in nodes if n["kind"] == "eval_loop" for b in n.get("body", [])}

    def after_last(kinds: tuple) -> int:
        """kinds 중 마지막 노드의 위치(eval_loop body 안이면 그 루프 노드 위치)."""
        loop_of = {b: n["id"] for n in nodes if n["kind"] == "eval_loop" for b in n.get("body", [])}
        pos = -1
        for i, x in enumerate(nodes):
            if x["kind"] in kinds:
                j = next(k for k, y in enumerate(nodes) if y["id"] == loop_of[x["id"]]) if x["id"] in loop_of else i
                pos = max(pos, j)
        return pos if pos >= 0 else len(nodes) - 1

    def add(idx: int, node: dict, why: str) -> None:
        node["id"] = _uniq_id(nodes, node["id"])
        node.setdefault("approve", "ask")
        node["auto"] = True
        nodes.insert(idx, node)
        notes.append(f"+ {node['id']} ({node['kind']}) — {why}")

    # 1) eval_loop 앞에 eval_check
    for i, n in enumerate(list(nodes)):
        if n["kind"] == "eval_loop" and "eval_check" not in removed:
            j = nodes.index(n)
            first = min((nodes.index(x) for x in nodes if x["id"] in n.get("body", [])), default=j)
            if not any(x["kind"] == "eval_check" for x in nodes[:first]):
                add(first, {"id": "check", "kind": "eval_check", "cmd": test_cmd}, "eval_loop 앞 평가기 검증")
    # 2) 기능 확인 명령이 있는 code 그래프 → 첫 code 앞에 eval_check (평가기 먼저 검증)
    if has_tests and has_code and sp.functional and "eval_check" not in removed and \
            not any(n["kind"] == "eval_check" for n in nodes):
        first = next(i for i, n in enumerate(nodes) if n["kind"] == "code")
        add(first, {"id": "check", "kind": "eval_check", "cmd": test_cmd}, "코딩 루프 전에 평가기(테스트) 검증")
    # 3) 각 code 뒤에 test (eval_loop body 안의 code 는 body 에 test 가 있으면 생략)
    if "test" not in removed:
        for n in [x for x in nodes if x["kind"] == "code"]:
            i = nodes.index(n)
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            if nxt and nxt["kind"] == "test":
                continue
            if n["id"] in body_ids:
                continue
            add(i + 1, {"id": f"test_{n['id']}", "kind": "test", "cmd": test_cmd}, f"{n['id']} 뒤 기능 확인")
    # 3b) 테스트가 아직 없는 새 프로젝트 → 평가기는 에이전트가 쓴다. 첫 초록불 직후(첫 code 뒤 test 다음)에 검증
    if not has_tests and has_code and "eval_check" not in removed and not any(n["kind"] == "eval_check" for n in nodes):
        first = next(i for i, n in enumerate(nodes) if n["kind"] == "code")
        at = first + 1
        while at < len(nodes) and nodes[at]["kind"] == "test":
            at += 1
        add(at, {"id": "check", "kind": "eval_check", "cmd": test_cmd},
            "테스트 없는 새 프로젝트: 에이전트가 쓴 테스트를 첫 초록불 직후 검증")
    # 4) 성능 항목 → 마지막 code(또는 test) 뒤 bench
    if "bench" not in removed and has_code:
        for r in sp.perf:
            if r.check and not any(x["kind"] == "bench" and x.get("cmd") == r.check for x in nodes):
                add(after_last(("code", "test", "eval_loop")) + 1, {"id": f"bench_{r.id.lower()}", "kind": "bench", "cmd": r.check,
                               "goal": r.target or ""}, f"성능 항목 {r.id}")
    # 5) 수동 확인 항목 → ask
    if "ask" not in removed:
        for r in sp.manual:
            if not any(x["kind"] == "ask" and r.id in x.get("goal", "") for x in nodes):
                add(after_last(("code", "test", "bench", "eval_loop")) + 1, {"id": f"ask_{r.id.lower()}", "kind": "ask",
                               "goal": f"[{r.id}] 확인해 주세요: {r.text}"}, f"수동 확인 항목 {r.id}")
    # 6) 코드가 바뀌는 그래프 → 끝에 cleanup → docs → changelog (git 앞)
    if has_code:
        git_idx = next((i for i, x in enumerate(nodes) if x["kind"] == "git"), len(nodes))
        for kind, nid, why in (("cleanup", "tidy", "잔재 검사"), ("docs", "docs", "문서 갱신 확인"),
                               ("changelog", "log", "CHANGELOG")):
            if kind in removed or any(x["kind"] == kind for x in nodes):
                continue
            git_idx = next((i for i, x in enumerate(nodes) if x["kind"] == "git"), len(nodes))
            add(git_idx, {"id": nid, "kind": kind}, why)
    # 7) 커밋·푸시 요청 → git (사전 승인은 사용자가 체크해야만)
    if sp.wants_commit() and "git" not in removed and not any(x["kind"] == "git" for x in nodes):
        add(len(nodes), {"id": "git", "kind": "git", "goal": "commit_push" if sp.wants_push() else "commit",
                         "approve": "ask"}, "스펙에 커밋·푸시 요청")
    plan["nodes"] = nodes
    return plan, notes


def main_chain(plan: dict) -> list[dict]:
    body = {b for n in plan["nodes"] if n["kind"] == "eval_loop" for b in n.get("body", [])}
    return [n for n in plan["nodes"] if n["id"] not in body]


def generate(sp: Spec, spec_text: str, ledger=None) -> dict:
    user = f"Spec:\n{spec_text}\n\nTest command: {sp.test_cmd()}\nReturn the graph JSON."
    data = llm.chat_json([{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": user}],
                         PLAN_SCHEMA, name="plan", max_tokens=1500, ledger=ledger, tag="plan")
    from .spec import restore_quotes
    nodes = [_clean_node(n) for n in data.get("nodes", [])][:MAX_NODES]
    for n in nodes:
        if n.get("goal"):
            n["goal"] = restore_quotes(n["goal"], sp.raw)
    return {"nodes": nodes}


KIND_WORDS = {
    "eval_check": ["eval_check", "평가기", "평가 검증"], "test": ["test", "테스트"], "bench": ["bench", "벤치", "성능"],
    "docs": ["docs", "문서"], "cleanup": ["cleanup", "정리", "잔재"], "changelog": ["changelog", "체인지로그", "변경 기록"],
    "git": ["git", "커밋", "푸시", "commit", "push"], "ask": ["ask", "질문", "확인 요청", "수동 확인"],
}


RX_REMOVE = re.compile(r"빼|삭제|없애|제거|지워|말고|생략|remove|drop|delete|without|skip", re.I)


def mentions(instruction: str, kind: str) -> bool:
    """지시문이 그 종류를 '빼라'고 했는지: 종류 언급 + 제거 동사."""
    low = instruction.lower()
    return bool(RX_REMOVE.search(low)) and any(w in low for w in KIND_WORDS.get(kind, [kind]))


def modify(plan: dict, instruction: str, sp: Spec, ledger=None) -> dict:
    cur = [{k: v for k, v in n.items() if k != "auto"} for n in plan["nodes"]]
    user = (f"Current graph nodes (JSON):\n{json.dumps(cur, ensure_ascii=False)}\n\n"
            f"Test command: {plan.get('test_cmd') or sp.test_cmd()}\n"
            f"User instruction: {instruction}\n\nReturn the full updated node list. Change only what was asked. "
            f"For a lint step use kind=test with cmd like 'python -m ruff check .'.")
    data = llm.chat_json([{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": user}],
                         PLAN_SCHEMA, name="plan", max_tokens=1500, ledger=ledger, tag="plan_modify")
    new_nodes = [_clean_node(n) for n in data.get("nodes", [])][:MAX_NODES]
    old_auto = {n["id"]: n for n in plan["nodes"] if n.get("auto")}
    new_ids = {n["id"] for n in new_nodes}
    new_kinds = {n["kind"] for n in new_nodes}
    removed = set(plan.get("removed_by_user", []))
    for nid, n in old_auto.items():
        # 지시문에 그 종류가 언급됐을 때만 '사용자가 뺌'으로 기록(9B 가 엉뚱하게 지운 자동 노드는 다시 붙는다)
        if nid not in new_ids and n["kind"] not in new_kinds and mentions(instruction, n["kind"]):
            removed.add(n["kind"])
    from .spec import restore_quotes
    for n in new_nodes:
        if n.get("goal"):
            n["goal"] = restore_quotes(n["goal"], sp.raw)
    for n in new_nodes:              # 사용자가 다시 넣은 종류는 해제
        if n["id"] in old_auto:
            n["auto"] = True
        removed.discard(n["kind"]) if n["kind"] in new_kinds and n["id"] not in old_auto else None
    out = {**plan, "nodes": new_nodes, "removed_by_user": sorted(removed)}
    if re.search(r"eval_loop|반복|repeat|loop", instruction, re.I) and             not any(n["kind"] == "eval_loop" for n in new_nodes):
        out = wrap_loop(out, instruction)
    return out


def wrap_loop(plan: dict, instruction: str) -> dict:
    """9B 가 eval_loop 를 못 만들었을 때의 코드 보정: code·test 노드를 몸통으로 하는 eval_loop 를 붙인다."""
    nodes = [dict(n) for n in plan["nodes"]]
    body = [n["id"] for n in nodes if n["kind"] in ("code", "test")]
    if not any(n["kind"] == "code" for n in nodes):
        return plan
    m_thr = re.search(r"(\d+(?:\.\d+)?)\s*%|([01](?:\.\d+)?)", instruction)
    thr = 1.0
    if m_thr:
        thr = float(m_thr.group(1)) / 100 if m_thr.group(1) else float(m_thr.group(2))
    m_it = re.search(r"(?:최대|max(?:imum)?)\s*(\d+)|(\d+)\s*(?:번|회|times)", instruction, re.I)
    iters = int(next(g for g in m_it.groups() if g)) if m_it else 3
    last = max(i for i, n in enumerate(nodes) if n["id"] in body)
    nodes.insert(last + 1, {"id": _uniq_id(nodes, "loop"), "kind": "eval_loop", "approve": "ask", "body": body,
                            "threshold": max(0.0, min(1.0, thr)), "max_iters": max(1, iters), "fixed_by_code": True})
    return {**plan, "nodes": nodes}


# ---------------------------------------------------------------- 표시

def _desc(n: dict, plan: dict) -> str:
    k = n["kind"]
    if k == "code":
        return n.get("goal", "") + (f"  [module: {n['module']}]" if n.get("module") else "")
    if k in ("test", "bench", "eval_check"):
        s = n.get("cmd") or plan.get("test_cmd", "")
        return s + (f"  (기준 {n['goal']})" if k == "bench" and n.get("goal") else "")
    if k == "eval_loop":
        return f"{' → '.join(n.get('body', []))} 반복, 통과율 ≥ {n.get('threshold')} (최대 {n.get('max_iters')}회)"
    if k == "git":
        return n.get("goal") or "commit"
    return n.get("goal", "")


def render_text(plan: dict) -> str:
    lines = [f"계획 그래프  (테스트 명령: {plan.get('test_cmd', '')})"]
    chain = main_chain(plan)
    body_of = {n["id"]: n for n in plan["nodes"] if n["kind"] == "eval_loop"}
    by_id = {n["id"]: n for n in plan["nodes"]}
    step = 0
    for i, n in enumerate(chain):
        if n["kind"] == "eval_loop":
            lines.append("  ┌─ 반복 ──────────────────────────")
            for b in body_of[n["id"]].get("body", []):
                bn = by_id[b]
                step += 1
                lines.append(f"  │ {step:2d}. {ICON.get(bn['kind'], '·')} {bn['id']:<12} [{bn['kind']}]  {_desc(bn, plan)}")
            lines.append(f"  └─ {ICON['eval_loop']} {n['id']} [eval_loop] {_desc(n, plan)}")
        else:
            step += 1
            auto = " (자동)" if n.get("auto") else ""
            appr = "  사전승인" if n.get("approve") == "pre" else ""
            lines.append(f"  {step:2d}. {ICON.get(n['kind'], '·')} {n['id']:<12} [{n['kind']}]{auto}{appr}  {_desc(n, plan)}")
        if i < len(chain) - 1:
            lines.append("       │")
    if plan.get("removed_by_user"):
        lines.append(f"  (사용자가 뺀 자동 노드 종류: {', '.join(plan['removed_by_user'])})")
    return "\n".join(lines)


def render_mermaid(plan: dict, status: dict[str, str] | None = None) -> str:
    status = status or {}
    out = ["flowchart TD", "  START([start])"]
    chain = main_chain(plan)
    by_id = {n["id"]: n for n in plan["nodes"]}

    def label(n: dict) -> str:
        d = _desc(n, plan).replace('"', "'")[:60]
        st = status.get(n["id"], "")
        return f'{n["id"]}["{ICON.get(n["kind"], "")} {n["id"]} ({n["kind"]}){" · " + st if st else ""}<br/>{d}"]'

    prev = "START"
    for n in chain:
        if n["kind"] == "eval_loop":
            body = n.get("body", [])
            for b in body:
                out.append("  " + label(by_id[b]))
            out.append("  " + label(n))
            out.append(f"  {prev} --> {body[0]}")
            for a, b in zip(body, body[1:]):
                out.append(f"  {a} --> {b}")
            out.append(f"  {body[-1]} --> {n['id']}")
            out.append(f"  {n['id']} -. 기준 미달 .-> {body[0]}")
        else:
            out.append("  " + label(n))
            out.append(f"  {prev} --> {n['id']}")
        prev = n["id"]
    out.append("  END([end])")
    out.append(f"  {prev} --> END")
    for n in plan["nodes"]:
        st = status.get(n["id"], "")
        color = {"pass": "#d4f7d4", "done": "#d4f7d4", "fail": "#ffd6d6", "blocked": "#ffd6d6",
                 "running": "#fff3c4", "skipped": "#eeeeee"}.get(st.split(" ")[0] if st else "")
        if color:
            out.append(f"  style {n['id']} fill:{color}")
        elif n.get("auto"):
            out.append(f"  style {n['id']} stroke-dasharray: 4 3")
    return "\n".join(out) + "\n"


def save(ws: Path, plan: dict) -> Path:
    p = plan_path(ws)
    p.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (cap_dir(ws) / "plan.mmd").write_text(render_mermaid(plan), encoding="utf-8")
    return p


def load(ws: Path) -> dict:
    p = plan_path(ws)
    if not p.is_file():
        raise SystemExit("계획이 없다. 먼저 `cap graph`.")
    plan = yaml.safe_load(p.read_text(encoding="utf-8"))
    errs = validate(plan)
    if errs:
        raise SystemExit("plan.yaml 검증 실패:\n  " + "\n  ".join(errs))
    return plan
