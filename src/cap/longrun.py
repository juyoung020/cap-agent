"""긴 작업(`cap go`): 스펙 하나를 단계로 나눠 사람이 자리를 비운 사이 끝까지 돈다.

설계: docs/cap/LONGRUN.md. 기억은 모델 밖에 둔다 — 스펙(무엇), 단계 그래프 상태(어디까지), 장부·progress.md(무슨 일).
단계마다 새 코딩 루프(새 창)를 만들고 그 단계에 필요한 것만 넣는다.

바깥 그래프(LangGraph, SQLite 체크포인트): START → pick → stage → pick … → final → END.
- pick: 질문함 답 반영·기한 확인·다음에 돌 단계 고르기(앞 단계가 끝난 것, 막힌 단계는 건너뜀).
- stage: 테스트 먼저 잠금 → 구현(재시도마다 방법을 바꿈) → 확인 명령 → 앞 단계 회귀 확인 → 단계 커밋.
- final: 모든 확인 명령을 다시 돌려 그 결과로만 요구사항 판을 만든다(거짓 완료 방지).

사람에게 물을 일이 생겨도 실행 전체를 세우지 않는다: 질문함(.cap/long/<id>/inbox.json)에 적고 그 행동은 거절하거나
그 단계만 '대기'로 두고 다른 단계를 계속 돈다. 답은 `cap inbox` 또는 폰(ntfy 답장 토픽)으로 받는다.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from . import llm
from .evalcheck import collect_test_files
from .policy import ALWAYS_HUMAN, classify_command
from .spec import FUNC_CATS, PERF_CATS, Item, Spec
from .store import Ledger, Shadow, cap_dir, now_id
from .tools import clip, run_shell

PRESETS = {
    # 묻지 않고 해도 되는 승인 범주. 지침 파일·기존 테스트 고치기·격리 밖 실행은 어느 프리셋도 못 푼다.
    "safe": set(),
    "normal": {"delete", "move"},
    "bold": {"delete", "move", "kill", "git_rewrite", "install", "network"},
}
PRESET_TEXT = {"safe": "안전: 파일 삭제·이동도 물어봄", "normal": "보통: 작업 폴더 안 삭제·이동은 허용",
               "bold": "과감: 설치·네트워크·프로세스 종료도 허용"}
DEFAULT_HOURS = 8.0
BREAKAWAY = 0x01000000          # CREATE_BREAKAWAY_FROM_JOB
MAX_ATTEMPTS = 3            # 단계당 구현 시도: 1 그대로 → 2 새 창+실패 원인 → 3 파일별로 잘게
TURNS_PER_ATTEMPT = 12
QUESTION_TIMEOUT_MIN = 30   # 답이 없으면 기본값으로
MAX_STAGES = 12
PACKET_MAX = 2400           # 단계 목표 글(작업 패킷) 최대 글자 수 — 9B 는 창 절반을 넘기면 둔해진다
TEST_FILE_RX = re.compile(r"([\w./\\-]*test[\w./\\-]*\.py)")
# 알림에서 가릴 키 모양: sk-…, ghp_…, xox…, 긴 영숫자 덩어리
KEYISH_RX = re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|xox[abp]-|AKIA)[\w-]{8,}|\b[A-Za-z0-9_\-]{32,}\b")


# ---------------------------------------------------------------- 파일

def run_dir(ws: Path, rid: str) -> Path:
    d = cap_dir(ws) / "long" / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def runs(ws: Path) -> list[str]:
    d = cap_dir(ws) / "long"
    return sorted(p.name for p in d.iterdir() if (p / "run.json").is_file()) if d.is_dir() else []


def latest(ws: Path) -> str | None:
    r = runs(ws)
    return r[-1] if r else None


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load_run(ws: Path, rid: str) -> dict:
    return _read(run_dir(ws, rid) / "run.json", {})


def save_run(ws: Path, rid: str, cfg: dict) -> None:
    _write(run_dir(ws, rid) / "run.json", cfg)


# ---------------------------------------------------------------- 질문함

def inbox(ws: Path, rid: str) -> list[dict]:
    return _read(run_dir(ws, rid) / "inbox.json", [])


def _save_inbox(ws: Path, rid: str, items: list[dict]) -> None:
    _write(run_dir(ws, rid) / "inbox.json", items)


def ask(ws: Path, rid: str, *, stage: str, kind: str, question: str, options: list[str], default: str,
        data: dict | None = None) -> dict:
    """질문을 질문함에 넣고 알린다. 같은 단계·같은 내용의 열린 질문이 있으면 그것을 돌려준다."""
    items = inbox(ws, rid)
    for q in items:
        if q["stage"] == stage and q["question"] == question and q.get("answer") is None:
            return q
    q = {"id": f"q{len(items) + 1}", "stage": stage, "kind": kind, "question": question, "options": options,
         "default": default, "data": data or {}, "created": time.time(), "answer": None}
    items.append(q)
    _save_inbox(ws, rid, items)
    notify(ws, f"CAP 질문 {q['id']}", f"[{stage}] {question}", rid=rid, question=q)
    return q


def answer(ws: Path, rid: str, qid: str, text: str, by: str = "user") -> dict | None:
    items = inbox(ws, rid)
    for q in items:
        if q["id"] == qid and q.get("answer") is None:
            opts = q.get("options") or []
            t = text.strip()
            if t.isdigit() and 1 <= int(t) <= len(opts):
                t = opts[int(t) - 1]
            q.update(answer=t, answered_at=time.time(), by=by)
            _save_inbox(ws, rid, items)
            return q
    return None


def open_questions(ws: Path, rid: str) -> list[dict]:
    return [q for q in inbox(ws, rid) if q.get("answer") is None]


def apply_timeouts(ws: Path, rid: str, minutes: float = QUESTION_TIMEOUT_MIN, now: float | None = None) -> list[dict]:
    """오래 답이 없는 질문에 기본값을 넣는다(사람이 자리에 없어도 실행이 계속 간다)."""
    now = now or time.time()
    done = []
    for q in open_questions(ws, rid):
        if now - q["created"] >= minutes * 60:
            done.append(answer(ws, rid, q["id"], q["default"], by="timeout"))
    return [q for q in done if q]


# ---------------------------------------------------------------- 알림 (데스크톱 + ntfy)

def _notify_cfg() -> dict:
    from .compat import settings
    v = settings().get("notify") or {}
    return v if isinstance(v, dict) else {}


def notify(ws: Path, title: str, body: str, rid: str = "", question: dict | None = None) -> None:
    """한 줄 요약만 보낸다(코드·키 없음). ~/.cap/settings.json: "notify": {"ntfy": "https://ntfy.sh/<토픽>",
    "ntfy_reply": "https://ntfy.sh/<답장 토픽>"} — 답장 토픽이 있으면 질문 알림에 선택지 버튼을 단다."""
    from .extras import desktop_notify
    body = KEYISH_RX.sub("<가림>", llm.scrub(body))[:300]
    title = KEYISH_RX.sub("<가림>", title)
    desktop_notify(title, body)
    cfg = _notify_cfg()
    url = str(cfg.get("ntfy") or "")
    if not url.startswith("https://"):
        return
    headers = {"Title": title.encode("utf-8").decode("latin-1", "replace"), "Tags": "robot"}
    reply = str(cfg.get("ntfy_reply") or "")
    if question and reply.startswith("https://"):
        acts = [f"http, {o[:20]}, {reply}, body={rid} {question['id']} {i}, clear=true"
                for i, o in enumerate(question.get("options", [])[:3], 1)]
        headers["Actions"] = "; ".join(acts).encode("utf-8").decode("latin-1", "replace")
    try:
        import urllib.request
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001 — 알림 실패로 실행을 멈추지 않는다
        pass


def poll_replies(ws: Path, rid: str) -> list[dict]:
    """ntfy 답장 토픽에서 '<실행id> <질문id> <답>' 줄을 읽어 질문함에 넣는다."""
    reply = str(_notify_cfg().get("ntfy_reply") or "")
    if not reply.startswith("https://"):
        return []
    cfg = load_run(ws, rid)
    since = cfg.get("reply_since") or "10m"
    got = []
    try:
        import urllib.request
        with urllib.request.urlopen(f"{reply.rstrip('/')}/json?poll=1&since={since}", timeout=10) as r:
            lines = r.read().decode("utf-8", "replace").splitlines()
    except Exception:  # noqa: BLE001
        return []
    for ln in lines:
        try:
            m = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if m.get("event") != "message":
            continue
        cfg["reply_since"] = m.get("id") or since
        parts = (m.get("message") or "").strip().split(None, 2)
        if len(parts) == 3 and parts[0] == rid:
            q = answer(ws, rid, parts[1], parts[2], by="phone")
            if q:
                got.append(q)
        elif len(parts) >= 2 and parts[0].startswith("q") and len(runs(ws)) == 1:
            q = answer(ws, rid, parts[0], " ".join(parts[1:]), by="phone")
            if q:
                got.append(q)
    save_run(ws, rid, cfg)
    return got


# ---------------------------------------------------------------- 단계 계획

STAGE_PROMPT = """You split a locked software spec into ordered stages for an autonomous coding agent with a small
context window. Each stage covers 1-3 requirement ids that belong together and lists the 1-3 files it will mainly edit.
"after" lists the numbers (1-based) of earlier stages it needs. Keep stages small and concrete; at most 10 stages.
Reply as JSON."""
STAGE_SCHEMA = {"type": "object", "properties": {"stages": {"type": "array", "items": {
    "type": "object", "properties": {"title": {"type": "string"},
                                     "requirements": {"type": "array", "items": {"type": "string"}},
                                     "files": {"type": "array", "items": {"type": "string"}},
                                     "after": {"type": "array", "items": {"type": "integer"}}},
    "required": ["title", "requirements", "files", "after"], "additionalProperties": False}}},
    "required": ["stages"], "additionalProperties": False}


def _checkable(sp: Spec) -> list[Item]:
    return [r for r in sp.requirements if r.check]


def split_requirements(sp: Spec) -> list[str]:
    """확인 명령 하나가 테스트 파일 여러 개를 가리키는 요구사항은 파일마다 나눈다(9B 가 기능을 한 줄로 뭉치곤 함).
    그래야 단계를 작게 나누고 어느 것이 통과했는지 따로 보인다. 돌려준 값: 바꾼 내용 메모."""
    out, notes = [], []
    for r in sp.requirements:
        files = test_files_of(r.check)
        if len(files) < 2:
            out.append(r)
            continue
        base = r.check
        for f in files:
            base = base.replace(f, "")
        base = " ".join(base.split())
        for f in files:
            stem = Path(f).stem.removeprefix("test_")
            out.append(Item("", r.cat, f"[{stem}] {r.text}", f"{base} {f}", r.target))
        notes.append(f"{r.id} 를 테스트 파일 {len(files)}개로 나눔")
    for i, r in enumerate(out, 1):
        r.id = f"R{i}"
    sp.requirements = out
    return notes


BARE_PYTEST_RX = re.compile(r"^\s*(python -m )?pytest(\s+-q)?(\s+-p no:cacheprovider)?\s*$")


def assign_test_files(sp: Spec, ws: Path) -> list[str]:
    """테스트가 아직 없는 프로젝트에서 '전체 pytest' 로만 확인하거나 확인 명령이 없는 기능 요구사항에 테스트 파일을 하나씩 정해 준다
    (tests/test_r1.py …). 그래야 단계마다 테스트를 먼저 쓰고 잠근 뒤 구현한다(쉬운 테스트로 통과하는 것 막기)."""
    if collect_test_files(ws):
        return []
    notes = []
    for r in sp.requirements:
        functional = any(p in r.cat.lower() for p in FUNC_CATS)
        bare = bool(r.check) and bool(BARE_PYTEST_RX.match(r.check)) and not any(p in r.cat.lower() for p in PERF_CATS)
        if bare or (not r.check and functional):
            f = f"tests/test_{r.id.lower()}.py"
            r.check = f"python -m pytest -q {f}"
            notes.append(f"{r.id} 확인 → {f} (테스트 먼저)")
    return notes


def fallback_stages(sp: Spec) -> list[dict]:
    reqs = _checkable(sp)
    out = []
    for i in range(0, len(reqs), 2):
        grp = reqs[i:i + 2]
        out.append({"title": " / ".join(r.text[:40] for r in grp), "requirements": [r.id for r in grp],
                    "files": [], "after": [len(out)] if out else []})
    return out


def normalize_stages(raw: list[dict], sp: Spec) -> list[dict]:
    """모델이 낸 단계를 코드로 바로잡는다: 없는 요구사항 id 빼기, 빠진 확인 가능 요구사항은 끝 단계로,
    앞 단계만 가리키게, 단계 수 제한. 모두 실패하면 기본 분할."""
    known = {r.id for r in _checkable(sp)}
    seen: set[str] = set()
    stages = []
    for s in raw[:MAX_STAGES]:
        reqs = [r for r in (s.get("requirements") or []) if r in known and r not in seen]
        if not reqs:
            continue
        seen |= set(reqs)
        files = [str(f).replace("\\", "/").strip() for f in (s.get("files") or []) if str(f).strip()][:3]
        stages.append({"title": str(s.get("title") or "")[:80] or reqs[0], "requirements": reqs, "files": files,
                       "after_raw": [int(a) for a in (s.get("after") or []) if isinstance(a, int)]})
    if not stages:
        return [{**s, "id": f"s{i + 1}", "after": [f"s{i}"] if i else []} for i, s in enumerate(fallback_stages(sp))]
    missing = [r.id for r in _checkable(sp) if r.id not in seen]
    if missing:
        stages.append({"title": "남은 요구사항 " + ", ".join(missing), "requirements": missing, "files": [],
                       "after_raw": [len(stages)]})
    out = []
    for i, s in enumerate(stages):
        after = sorted({f"s{a}" for a in s.pop("after_raw") if 1 <= a <= i})
        out.append({**s, "id": f"s{i + 1}", "after": after})
    return out


def plan_stages(sp: Spec, ws: Path, ledger=None) -> list[dict]:
    from .loop import ws_listing
    from .spec import render
    user = f"Spec:\n{clip(render(sp), 5000)}\n\nWorkspace files:\n{ws_listing(ws, 40)}"
    try:
        data = llm.chat_json([{"role": "system", "content": STAGE_PROMPT}, {"role": "user", "content": user}],
                             STAGE_SCHEMA, name="stages", max_tokens=1200, ledger=ledger, tag="stages")
        raw = data.get("stages") or []
    except Exception:  # noqa: BLE001 — 계획 실패는 기본 분할로
        raw = []
    return normalize_stages(raw, sp)


def stage_checks(sp: Spec, stage: dict) -> list[str]:
    by = {r.id: r for r in sp.requirements}
    out = []
    for rid in stage["requirements"]:
        c = by[rid].check if rid in by else ""
        if c and c not in out:
            out.append(c)
    return out


def test_files_of(cmd: str) -> list[str]:
    return [m.replace("\\", "/").split("::")[0] for m in TEST_FILE_RX.findall(cmd or "")]


# ---------------------------------------------------------------- 상태

def _merge(a: dict, b: dict) -> dict:
    out = {k: dict(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out[k] = {**out.get(k, {}), **v}
    return out


class LongState(TypedDict, total=False):
    stages: Annotated[dict, _merge]   # id → {status, attempts, notes, hint, commit, seconds, calls}
    allowed: list                     # 실행 중 사람이 허락한 승인 범주
    current: str
    status: str                       # running | done | deadline
    frozen: list                      # 잠근 테스트 파일


def board_line(ws: Path, rid: str, values: dict | None = None) -> str:
    """한 줄 상태판: 단계 3/7 · ✔9 ✗1 ·5 · 질문 1 · 약 2시간 남음."""
    cfg = load_run(ws, rid)
    st = (values or {}).get("stages") or cfg.get("last_stages") or {}
    stages = cfg.get("stages") or []
    n_done = sum(1 for s in stages if st.get(s["id"], {}).get("status") == "done")
    reqs = cfg.get("requirements") or {}
    ok = sum(1 for v in reqs.values() if v == "pass")
    bad = sum(1 for v in reqs.values() if v == "fail")
    rest = len(cfg.get("req_ids") or []) - ok - bad
    cur = (values or {}).get("current") or cfg.get("current") or ""
    left = ""
    if cfg.get("deadline") and cfg.get("state") in ("running", "started", "restarting", "created"):
        sec = cfg["deadline"] - time.time()
        left = f" · {'약 ' + _hm(sec) + ' 남음' if sec > 0 else '기한 지남'}"
    state = cfg.get("state", "")
    q = len(open_questions(ws, rid))
    return (f"[{rid}] {state} · 단계 {n_done}/{len(stages)}" + (f" (지금 {cur})" if cur and state == "running" else "")
            + f" · ✔{ok} ✗{bad} ·{rest} · 질문 {q}{left}")


def _hm(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


# ---------------------------------------------------------------- 실행 그래프

class Ctx:
    def __init__(self, ws: Path, rid: str, sp: Spec, printer: Callable[[str], None], home: Path | None = None):
        self.ws, self.rid, self.sp, self.out = ws, rid, sp, printer
        self.home = home or ws           # 실행 기록(.cap/long/<id>)이 있는 원래 작업 폴더; ws 는 코드를 고치는 곳(worktree)
        self.cfg = load_run(self.home, rid)
        self.shadow = Shadow(ws)
        self.ledger = Ledger(ws, f"long-{rid}")
        self.stages = {s["id"]: s for s in self.cfg["stages"]}
        self.preset = PRESETS.get(self.cfg.get("preset", "normal"), PRESETS["normal"])

    def log(self, line: str) -> None:
        self.out(line)
        with (run_dir(self.home, self.rid) / "progress.log").open("a", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now():%H:%M:%S} {line}\n")


def _git(ws: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_checks(ws: Path, cmds: list[str]) -> list[dict]:
    out = []
    for c in cmds:
        d = classify_command(c, ws)
        if d.verdict == "deny":
            out.append({"cmd": c, "ok": False, "tail": f"거부: {d.reason}"})
            continue
        code, text = run_shell(c, ws)
        tail = [ln for ln in text.strip().splitlines() if ln.strip()][-1:] or [""]
        out.append({"cmd": c, "ok": code == 0, "exit": code, "tail": tail[0][:160], "output": clip(text, 1500)})
    return out


def packet(ctx: Ctx, stage: dict, st: dict, done_ids: list[str], attempt: int, sub_goal: str = "") -> str:
    """새 창에 넣을 단계 목표 글: 맡은 요구사항·결정·앞 단계 요약·지난 실패. 대화 기록은 넘기지 않는다."""
    by = {r.id: r for r in ctx.sp.requirements}
    lines = [f"Stage {stage['id']} of a longer job: {stage['title']}",
             "Requirements for this stage (the harness checks them with the listed commands):"]
    for rid in stage["requirements"]:
        r = by.get(rid)
        if r:
            lines.append(f"- {rid} [{r.cat}] {r.text}" + (f"  (check: {r.check})" if r.check else ""))
    if ctx.sp.decisions:
        lines.append("Decisions to follow:")
        lines += [f"- [{d.cat}] {d.text}" for d in ctx.sp.decisions[:8]]
    if stage.get("files"):
        lines.append("Files this stage mainly edits: " + ", ".join(stage["files"]))
    if done_ids:
        lines.append("Already finished (do not redo or break them): " +
                     "; ".join(f"{i} {ctx.stages[i]['title']}" for i in done_ids))
    notes = st.get("notes") or []
    if notes:
        lines.append("What went wrong before:")
        lines += [f"- {n}" for n in notes[-2:]]
    if st.get("hint"):
        lines.append(f"Hint from the human: {st['hint']}")
    if attempt == 2:
        lines.append("Previous attempt failed. Start from the current files, make the smallest change that fixes the "
                     "failing check, and run it.")
    if sub_goal:
        lines.append(f"In this step only: {sub_goal}")
    lines.append("Edit one file at a time with small edits. Existing test files are locked.")
    return clip("\n".join(lines), PACKET_MAX)


def drive_loop(ctx: Ctx, goal: str, *, test_cmd: str, frozen: set[str], allowed: set[str], stage_id: str,
               write_scope: set[str] | None = None) -> dict:
    """코딩 루프 하나를 새 창으로 끝까지. 승인은 프리셋·허락받은 범주면 예, 아니면 질문함에 넣고 거절."""
    from langgraph.checkpoint.memory import InMemorySaver

    from .loop import build_loop
    by = {r.id: r for r in ctx.sp.requirements}
    reqs = [f"[{by[i].cat}] {by[i].text}" for i in ctx.stages[stage_id]["requirements"] if i in by]
    graph = build_loop(ws=ctx.ws, ledger=ctx.ledger, test_cmd=test_cmd, frozen=frozen,
                       preapproved=set(ctx.preset) | allowed, decisions=[f"[{d.cat}] {d.text}" for d in ctx.sp.decisions],
                       requirements=reqs, printer=lambda s: None, checkpointer=InMemorySaver(), mode="plan",
                       write_scope=write_scope, max_turns=TURNS_PER_ATTEMPT)
    cfg = {"configurable": {"thread_id": f"{ctx.rid}-{stage_id}-{now_id()}"}, "recursion_limit": 300}
    inp: Any = {"goal": goal}
    denied: list[str] = []
    calls0 = llm.STATS["calls"]
    while True:
        graph.invoke(inp, cfg)
        st = graph.get_state(cfg)
        intr = [i.value for t in st.tasks for i in t.interrupts]
        if not intr:
            break
        q = intr[0]
        if q.get("kind") == "approval":
            cats = {c.get("category") or "" for c in q.get("calls", [])}
            if cats and not (cats & ALWAYS_HUMAN) and cats <= (set(ctx.preset) | allowed):
                inp = Command(resume="y")
                continue
            what = "; ".join(f"{c['tool']} {json.dumps(c['args'], ensure_ascii=False)[:80]} ({c['reason']})"
                             for c in q.get("calls", []))
            ask(ctx.home, ctx.rid, stage=stage_id, kind="approval",
                question=f"허락할까요? {what}", options=["예", "아니오"], default="아니오",
                data={"categories": sorted(c for c in cats if c and c not in ALWAYS_HUMAN)})
            denied.append(what)
            inp = Command(resume="n")
        else:                      # 사람에게 넘김: 지금은 끝내고, 단계 재시도·질문함이 이어받는다
            inp = Command(resume="abort")
    v = graph.get_state(cfg).values or {}
    return {"status": v.get("status"), "reason": v.get("escalate_reason", ""), "denied": denied,
            "changed": list(v.get("changed") or []), "calls": llm.STATS["calls"] - calls0,
            "test_tail": (v.get("last_test_output") or "").strip().splitlines()[-1:] or [""]}


SPLIT_PROMPT = """Split one failing stage of a coding job into 2-4 small steps. Each step edits exactly one file.
Reply as JSON: {"steps": [{"file": "...", "goal": "..."}]}"""
SPLIT_SCHEMA = {"type": "object", "properties": {"steps": {"type": "array", "items": {
    "type": "object", "properties": {"file": {"type": "string"}, "goal": {"type": "string"}},
    "required": ["file", "goal"], "additionalProperties": False}}}, "required": ["steps"], "additionalProperties": False}


def split_steps(ctx: Ctx, stage: dict, notes: list[str]) -> list[dict]:
    user = packet(ctx, stage, {"notes": notes}, [], 3)
    try:
        data = llm.chat_json([{"role": "system", "content": SPLIT_PROMPT}, {"role": "user", "content": user}],
                             SPLIT_SCHEMA, name="split", max_tokens=600, ledger=ctx.ledger, tag="split")
        steps = [s for s in data.get("steps", []) if s.get("file") and s.get("goal")][:4]
    except Exception:  # noqa: BLE001
        steps = []
    return [{"file": s["file"].replace("\\", "/").strip("/"), "goal": s["goal"][:300]} for s in steps]


def ensure_tests(ctx: Ctx, stage: dict, frozen: set[str], allowed: set[str]) -> tuple[list[str], str]:
    """테스트 먼저: 확인 명령이 가리키는 테스트 파일이 없으면 테스트만 쓰게 하고, 있으면 잠근다.
    돌려준 값: (이 단계의 확인 명령들, 메모)."""
    checks = stage_checks(ctx.sp, stage)
    files = sorted({f for c in checks for f in test_files_of(c)})
    missing = [f for f in files if not (ctx.ws / f).is_file()]
    note = ""
    if missing:
        goal = (packet(ctx, stage, {}, [], 1) + "\n\nIn this step only: write the tests for these requirements in "
                + ", ".join(missing) + ". Do NOT implement the feature yet; the tests should fail until it exists.")
        r = drive_loop(ctx, goal, test_cmd="", frozen=frozen, allowed=allowed, stage_id=stage["id"],
                       write_scope=set(missing))
        made = [f for f in missing if (ctx.ws / f).is_file()]
        note = f"테스트 먼저 작성: {', '.join(made) or '없음'} ({r['calls']}회 호출)"
        pre = run_checks(ctx.ws, checks)
        if made and all(x["ok"] for x in pre):
            note += " — 구현 전에 이미 통과(테스트가 약할 수 있음)"
    for f in files:
        if (ctx.ws / f).is_file():
            frozen.add(f)
    return checks, note


def build_graph(ctx: Ctx):
    def pick(state: LongState) -> dict:
        stages = {k: dict(v) for k, v in (state.get("stages") or {}).items()}
        allowed = set(state.get("allowed") or [])
        # 질문함: 폰 답장·시간 지난 질문 → 답 반영
        poll_replies(ctx.home, ctx.rid)
        apply_timeouts(ctx.home, ctx.rid, ctx.cfg.get("question_timeout", QUESTION_TIMEOUT_MIN))
        upd: dict[str, dict] = {}
        for q in inbox(ctx.home, ctx.rid):
            if q.get("answer") is None or q.get("applied"):
                continue
            a = str(q["answer"]).strip()
            st = stages.get(q["stage"], {})
            yes = a.lower() in ("예", "y", "yes", "네", "1", "허락")
            if q["kind"] == "approval" and yes:
                allowed |= set(q.get("data", {}).get("categories") or [])
                if st.get("status") in ("parked", "failed"):
                    upd[q["stage"]] = {"status": "pending", "attempts": 0}
            elif q["kind"] == "stuck" and a not in ("건너뛰기", "skip", "2", ""):
                upd[q["stage"]] = {"status": "pending", "attempts": 0, "hint": a}
            elif q["kind"] == "stuck":
                upd[q["stage"]] = {"status": "skipped"}
            _mark_applied(ctx.home, ctx.rid, q["id"])
            ctx.log(f"  ↩ 질문 {q['id']} 답 '{a}' ({q.get('by')}) 반영")
        for k, v in upd.items():
            stages[k] = {**stages.get(k, {}), **v}
        cfg = load_run(ctx.home, ctx.rid)
        if cfg.get("deadline") and time.time() > cfg["deadline"]:
            ctx.log("⏰ 기한이 지나 멈춘다 — 지금까지 한 것으로 보고서를 쓴다.")
            return {"stages": upd, "allowed": sorted(allowed), "current": "", "status": "deadline"}
        done = {k for k, v in stages.items() if v.get("status") == "done"}
        for sid, s in ctx.stages.items():
            if stages.get(sid, {}).get("status", "pending") != "pending":
                continue
            if all(a in done for a in s["after"]):
                return {"stages": upd, "allowed": sorted(allowed), "current": sid}
        return {"stages": upd, "allowed": sorted(allowed), "current": ""}

    def stage(state: LongState) -> dict:
        sid = state["current"]
        s = ctx.stages[sid]
        st = dict((state.get("stages") or {}).get(sid, {}))
        allowed = set(state.get("allowed") or [])
        frozen = set(state.get("frozen") or []) | set(collect_test_files(ctx.ws))
        done_ids = [k for k, v in (state.get("stages") or {}).items() if v.get("status") == "done"]
        t0, calls0 = time.perf_counter(), llm.STATS["calls"]
        _set_cfg(ctx, current=sid, state="running")
        ctx.log(f"▶ 단계 {sid}: {s['title']}")
        checks, tnote = ensure_tests(ctx, s, frozen, allowed)
        if tnote:
            ctx.log("  " + tnote)
        if not checks:
            return _finish(ctx, sid, st, "skipped", "확인 명령이 없는 단계", t0, calls0, frozen)
        test_cmd = " && ".join(checks)
        notes = list(st.get("notes") or [])
        snap = ctx.shadow.snapshot(f"long {ctx.rid} {sid} start")
        attempt = int(st.get("attempts") or 0)
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            st.update(attempts=attempt, notes=notes)
            if attempt < 3:
                r = drive_loop(ctx, packet(ctx, s, st, done_ids, attempt), test_cmd=test_cmd, frozen=frozen,
                               allowed=allowed, stage_id=sid)
            else:                                  # 3번째: 파일 하나씩 잘게
                steps = split_steps(ctx, s, notes) or [{"file": "", "goal": "fix the failing check"}]
                ctx.log(f"  ✂ 잘게 나눠 다시: {len(steps)}걸음")
                r = {"denied": [], "reason": ""}
                for stp in steps:
                    scope = {stp["file"]} if stp["file"] and stp["file"] not in frozen else None
                    r = drive_loop(ctx, packet(ctx, s, st, done_ids, attempt, sub_goal=f"{stp['goal']} ({stp['file']})"),
                                   test_cmd=test_cmd, frozen=frozen, allowed=allowed, stage_id=sid, write_scope=scope)
            res = run_checks(ctx.ws, checks)
            if all(x["ok"] for x in res):
                broken = _regressions(ctx, done_ids)
                if not broken:
                    ctx.log(f"  ✔ 확인 통과 ({attempt}번째 시도)")
                    commit = _commit(ctx, sid, s)
                    return _finish(ctx, sid, {**st, "commit": commit}, "done", f"{attempt}번째 시도에 통과", t0, calls0, frozen)
                notes.append(f"this stage passed but broke earlier stages: {'; '.join(broken)}")
                ctx.log(f"  ↺ 앞 단계를 깨뜨려 되돌림: {'; '.join(broken)}")
                if snap:
                    ctx.shadow.restore(snap)
            else:
                bad = [x for x in res if not x["ok"]]
                why = r.get("reason") or ""
                notes.append(f"attempt {attempt}: " + "; ".join(f"`{x['cmd']}` → {x['tail']}" for x in bad)
                             + (f" ({why[:120]})" if why else ""))
                ctx.log(f"  ✗ {attempt}번째 시도 실패: {bad[0]['tail'][:100]}")
        q = ask(ctx.home, ctx.rid, stage=sid, kind="stuck",
                question=f"단계 {sid} '{s['title']}' 가 {MAX_ATTEMPTS}번 실패. 힌트를 주면 다시, 아니면 건너뜀. 마지막: "
                         + (notes[-1][:160] if notes else ""),
                options=["힌트 입력", "건너뛰기"], default="건너뛰기")
        ctx.log(f"  ⏸ 질문함 {q['id']} 에 넣고 다른 단계로")
        return _finish(ctx, sid, {**st, "notes": notes}, "parked", "사람 힌트 대기", t0, calls0, frozen)

    def route(state: LongState) -> str:
        if state.get("status") == "deadline":
            return "final"
        return "stage" if state.get("current") else "final"

    def final(state: LongState) -> dict:
        return {"status": "deadline" if state.get("status") == "deadline" else "done", "current": ""}

    g = StateGraph(LongState)
    g.add_node("pick", pick)
    g.add_node("stage", stage)
    g.add_node("final", final)
    g.add_edge(START, "pick")
    g.add_conditional_edges("pick", route, ["stage", "final"])
    g.add_edge("stage", "pick")
    g.add_edge("final", END)
    return g


def _mark_applied(ws: Path, rid: str, qid: str) -> None:
    items = inbox(ws, rid)
    for q in items:
        if q["id"] == qid:
            q["applied"] = True
    _save_inbox(ws, rid, items)


def _set_cfg(ctx: Ctx, **kv) -> None:
    cfg = load_run(ctx.home, ctx.rid)
    cfg.update(kv)
    save_run(ctx.home, ctx.rid, cfg)


def _finish(ctx: Ctx, sid: str, st: dict, status: str, line: str, t0: float, calls0: int, frozen: set[str]) -> dict:
    st = {**st, "status": status, "line": line, "seconds": round(time.perf_counter() - t0, 1),
          "calls": llm.STATS["calls"] - calls0}
    ctx.ledger.log("stage_end", stage=sid, status=status, line=line, seconds=st["seconds"], calls=st["calls"])
    cfg = load_run(ctx.home, ctx.rid)
    cfg.setdefault("last_stages", {})[sid] = {"status": status, "line": line}
    save_run(ctx.home, ctx.rid, cfg)
    write_progress(ctx, {**cfg["last_stages"]})
    return {"stages": {sid: st}, "current": "", "frozen": sorted(frozen)}


def _regressions(ctx: Ctx, done_ids: list[str]) -> list[str]:
    cmds = []
    for i in done_ids:
        for c in stage_checks(ctx.sp, ctx.stages[i]):
            if c not in cmds:
                cmds.append(c)
    return [f"{x['cmd']} → {x['tail'][:80]}" for x in run_checks(ctx.ws, cmds) if not x["ok"]]


def _commit(ctx: Ctx, sid: str, s: dict) -> str:
    if not ctx.cfg.get("branch") or not (ctx.ws / ".git").exists():
        return ""
    _git(ctx.ws, "add", "-A")
    r = _git(ctx.ws, "-c", "user.name=CAP", "-c", "user.email=cap@localhost", "commit", "-q", "-m",
             f"CAP 단계 {sid}: {s['title']}")
    if r.returncode != 0:
        return ""
    return _git(ctx.ws, "rev-parse", "--short", "HEAD").stdout.strip()


def write_progress(ctx: Ctx, last: dict) -> None:
    lines = [f"# 진행 상황 — {ctx.sp.title}", "", board_line(ctx.home, ctx.rid), ""]
    for sid, s in ctx.stages.items():
        v = last.get(sid, {})
        mark = {"done": "✔", "parked": "⏸", "skipped": "·", "failed": "✗"}.get(v.get("status", ""), " ")
        lines.append(f"- {mark} {sid} {s['title']} — {v.get('line', '대기')}")
    (run_dir(ctx.home, ctx.rid) / "progress.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 보고서

def final_report(ws: Path, rid: str, sp: Spec, values: dict, seconds: float, home: Path | None = None) -> str:
    """모든 확인 명령을 처음부터 다시 돌려 그 결과로만 요구사항 판을 만든다. ws = 코드 폴더, home = 실행 기록 폴더."""
    home = home or ws
    cfg = load_run(home, rid)
    stages = cfg.get("stages") or []
    sv = values.get("stages") or {}
    reqs = {}
    rows = []
    cache: dict[str, dict] = {}
    for r in sp.requirements:
        if not r.check:
            reqs[r.id] = "manual"
            rows.append(f"| {r.id} | {r.text[:60]} | · 사람 확인 | — |")
            continue
        if r.check not in cache:
            cache[r.check] = run_checks(ws, [r.check])[0]
        x = cache[r.check]
        reqs[r.id] = "pass" if x["ok"] else "fail"
        rows.append(f"| {r.id} | {r.text[:60]} | {'✔' if x['ok'] else '✗'} | `{r.check}` → {x['tail'][:60]} |")
    ok = sum(1 for v in reqs.values() if v == "pass")
    bad = sum(1 for v in reqs.values() if v == "fail")
    manual = sum(1 for v in reqs.values() if v == "manual")
    qs = open_questions(home, rid)
    cfg.update(requirements=reqs, req_ids=[r.id for r in sp.requirements])
    save_run(home, rid, cfg)
    head = (f"요구사항 {len(reqs)}개 중 **{ok}개 통과**, {bad}개 실패" + (f", {manual}개 사람 확인" if manual else "")
            + (f", 질문 {len(qs)}개가 답을 기다림" if qs else "") + ".")
    why = {"deadline": "기한이 되어 멈춤", "done": "더 돌 단계가 없음"}.get(values.get("status", ""), values.get("status", ""))
    next_ = ("다음: `cap inbox` 로 답하고 `cap go --resume " + rid + "`" if qs else
             (f"다음: 브랜치 `{cfg['branch']}` 를 보고 합치기 (`git log {cfg['branch']}`)" if cfg.get("branch") else
              "다음: 바뀐 파일 검토"))
    calls = sum(int((sv.get(s["id"]) or {}).get("calls") or 0) for s in stages)
    lines = [f"# CAP 긴 작업 보고서 — {sp.title}", "", head, f"{why} · 걸린 시간 {_hm(seconds)} · 모델 호출 {calls}번",
             next_, "", "## 단계", "", "| 단계 | 내용 | 결과 | 시도 | 시간 | 커밋 |", "|---|---|---|---|---|---|"]
    for s in stages:
        v = sv.get(s["id"]) or {}
        mark = {"done": "✔", "parked": "⏸ 대기", "skipped": "· 건너뜀"}.get(v.get("status", ""), "· 안 함")
        lines.append(f"| {s['id']} | {s['title'][:50]} | {mark} {v.get('line', '')[:40]} | {v.get('attempts', 0)} | "
                     f"{_hm(float(v.get('seconds') or 0))} | {v.get('commit', '')} |")
    lines += ["", "## 요구사항 (마지막에 처음부터 다시 확인한 결과)", "", "| id | 요구사항 | 결과 | 근거 |", "|---|---|---|---|", *rows]
    if qs:
        lines += ["", "## 답을 기다리는 질문", ""] + [f"- {q['id']} [{q['stage']}] {q['question']}" for q in qs]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 실행

def keep_awake(on: bool) -> None:
    """실행 중 Windows 절전 막기(화면은 꺼져도 됨)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:  # noqa: BLE001
        pass


def drive(ws: Path, rid: str, printer: Callable[[str], None] = print) -> dict:
    """실행(또는 이어서). 체크포인트에서 이어 가고, 이미 끝난 실행이면 새 답을 반영해 다시 고른다."""
    from .runner import checkpointer
    from .spec import parse
    cfg = load_run(ws, rid)
    ws_run = Path(cfg.get("worktree") or ws)
    sp = parse(cfg["spec_text"])
    ctx = Ctx(ws_run, rid, sp, printer, home=ws)   # 질문함·상태 파일은 원래 작업 폴더의 .cap/long/<id> 하나에
    saver, conn = checkpointer(ws)
    app = build_graph(ctx).compile(checkpointer=saver)
    tcfg = {"configurable": {"thread_id": f"long-{rid}"}, "recursion_limit": 1000}
    t0 = time.perf_counter()
    keep_awake(True)
    _set_cfg(ctx, state="running", pid=os.getpid())
    try:
        snap = app.get_state(tcfg)
        if snap.next:
            inp: Any = None
        elif snap.values:
            inp = {**snap.values, "status": "running", "current": ""}
        else:
            inp = {"stages": {s["id"]: {"status": "pending", "attempts": 0} for s in cfg["stages"]},
                   "allowed": [], "status": "running", "frozen": []}
        app.invoke(inp, tcfg)
        values = app.get_state(tcfg).values
    finally:
        conn.close()
        keep_awake(False)
    seconds = float(cfg.get("seconds_before") or 0) + time.perf_counter() - t0
    report = final_report(ws_run, rid, sp, values, seconds, home=ws)
    (run_dir(ws, rid) / "report.md").write_text(report, encoding="utf-8")
    state = "waiting" if open_questions(ws, rid) else "finished"
    _set_cfg(ctx, state=state, current="", seconds_before=round(seconds, 1),
             last_stages={k: {"status": v.get("status"), "line": v.get("line", "")} for k, v in (values.get("stages") or {}).items()})
    write_progress(ctx, load_run(ws, rid).get("last_stages") or {})
    head = report.splitlines()[2] if len(report.splitlines()) > 2 else ""
    notify(ws, "CAP 긴 작업 " + ("질문 대기" if state == "waiting" else "끝"), head.replace("**", ""), rid=rid)
    printer("")
    printer(report)
    return {"status": state, "values": values, "report": report}


def make_worktree(ws: Path, rid: str) -> tuple[str, str]:
    """git 저장소면 새 브랜치 cap/go-<id> 의 worktree 에서 돈다(사용자는 원래 폴더에서 계속 일함)."""
    if not (ws / ".git").exists() or _git(ws, "rev-parse", "HEAD").returncode != 0:
        return "", ""
    branch, wt = f"cap/go-{rid}", cap_dir(ws) / "worktrees" / f"go-{rid}"
    r = _git(ws, "worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        return "", ""
    return branch, str(wt)


def create(ws: Path, sp: Spec, spec_text: str, stages: list[dict], *, preset: str = "normal",
           hours: float = DEFAULT_HOURS, use_worktree: bool = True) -> str:
    rid = now_id()
    branch, wt = make_worktree(ws, rid) if use_worktree else ("", "")
    cfg = {"id": rid, "title": sp.title, "spec_text": spec_text, "stages": stages, "preset": preset,
           "created": time.time(), "deadline": time.time() + hours * 3600 if hours else None,
           "branch": branch, "worktree": wt, "state": "created", "req_ids": [r.id for r in sp.requirements]}
    save_run(ws, rid, cfg)
    return rid


def estimate_minutes(stages: list[dict]) -> int:
    return max(10, 12 * len(stages))


def detach(ws: Path, rid: str) -> int:
    """백그라운드 감시 프로세스로 돌린다(창을 닫아도 계속). 로그: .cap/long/<id>/run.log"""
    log = run_dir(ws, rid) / "run.log"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) \
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args = [sys.executable, "-m", "cap.longrun", "supervise", str(ws), rid]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with log.open("a", encoding="utf-8") as f:
        try:   # 띄운 쪽(터미널·작업 묶음)이 정리돼도 같이 죽지 않게 작업 묶음에서 빠져나온다
            p = subprocess.Popen(args, cwd=ws, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=flags | (BREAKAWAY if os.name == "nt" else 0),
                                 start_new_session=os.name != "nt", env=env)
        except OSError:   # 묶음이 빠져나오기를 허락하지 않으면 그냥
            p = subprocess.Popen(args, cwd=ws, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=flags, start_new_session=os.name != "nt", env=env)
    cfg = load_run(ws, rid)
    cfg.update(state="started", supervisor=p.pid)
    save_run(ws, rid, cfg)
    return p.pid


def supervise(ws: Path, rid: str, max_restarts: int = 20) -> int:
    """일하는 프로세스가 죽으면(서버 장애·예외) 기다렸다가 체크포인트에서 다시 시작한다."""
    for n in range(max_restarts + 1):
        print(f"[{_dt.datetime.now():%H:%M:%S}] worker start #{n + 1}", flush=True)
        code = subprocess.call([sys.executable, "-m", "cap.longrun", "work", str(ws), rid], cwd=ws)
        if code == 0:
            return 0
        cfg = load_run(ws, rid)
        if cfg.get("deadline") and time.time() > cfg["deadline"]:
            break
        wait = 300 if code == 3 else 60
        print(f"worker exit {code}; restart in {wait}s", flush=True)
        cfg.update(state="restarting", restarts=n + 1)
        save_run(ws, rid, cfg)
        time.sleep(wait)
    notify(ws, "CAP 긴 작업 멈춤", f"{rid}: 다시 시작해도 계속 실패. cap go --resume {rid}", rid=rid)
    return 1


def _work(ws: Path, rid: str) -> int:
    try:
        drive(ws, rid, printer=lambda s: print(s, flush=True))
        return 0
    except llm.ModelCallError as exc:
        print(f"model server problem: {exc}", flush=True)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {llm.scrub(str(exc))[:500]}", flush=True)
        return 2


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] in ("supervise", "work"):
        ws, rid = Path(argv[1]), argv[2]
        from .setup import apply_user_default
        apply_user_default()                       # cap setup 으로 고른 공급자·모델
        if os.environ.get("CAP_PROVIDER"):
            llm.use_provider(os.environ["CAP_PROVIDER"])
        return supervise(ws, rid) if argv[0] == "supervise" else _work(ws, rid)
    print("usage: python -m cap.longrun supervise|work <ws> <run id>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
