"""cap 터미널 명령.

기본: `cap` (대화형 코딩 에이전트), `cap "할 일"` / `cap exec "할 일"` (한 번 실행) — chat.py
선택 기능(스펙 → 그래프 → 재생): init · spec · graph · run · resume · status · view · rollback
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import llm
from . import plan as planmod
from . import spec as specmod
from .evalcheck import collect_test_files
from .policy import normalize_mode
from .store import Ledger, Shadow, cap_dir, find_ws, now_id

SUBCOMMANDS = {"init", "spec", "graph", "run", "resume", "status", "view", "rollback"}
TOP_HELP = """사용법
  cap                         대화형 코딩 에이전트(현재 폴더). 할 일을 쓰면 바로 읽고·고치고·실행·테스트
  cap "할 일"  /  cap exec "할 일"   대화창 없이 한 번 실행하고 끝
  cap --resume [세션|번호]     최근(또는 지정한) 세션 이어서
  cap setup                   처음 설정: 모델 공급자(OpenAI·Anthropic·Gemini·OpenRouter·Ollama·LM Studio·수업 서버·직접)·키·모델
  cap sandbox [setup|undo]    명령 격리 상태 / 준비(Windows: WSL·관리자 권한 불필요) / 이 폴더에 준 권한 되돌리기
  cap --version
  옵션  --mode chat|ask|plan|auto|bypass   (기본 ask)   --plain  줄 모드 강제   --sandbox on|off|wsl  명령 격리   --profile <이름>  설정 묶음
        (호환: -a/--approval 같은 값, auto-edit·full-auto·--full-auto = auto)

  실제 터미널에서 cap 은 전체 화면 대화창(Shift+Tab 또는 Alt+M 으로 모드 전환).
  파이프·비대화 입력, cap "할 일", cap exec, --plain 은 줄 모드.

  chat    대화: 질문·설명만. 읽기·검색만, 편집·명령 실행 안 함
  ask     승인(기본): 편집·명령마다 승인(읽기·테스트 명령만 바로)
  plan    플랜: 읽기·검색만 해서 계획(todo)을 세움 → 승인하면 그 계획대로 실행
  auto    오토: 폴더 안 편집·명령은 바로, 위험 명령(삭제·이동·설치·네트워크·git 되돌리기·커밋·푸시)만 승인
  bypass  바이패스: 승인 없이 전부 실행
  하드 금지(작업 폴더 밖 쓰기, .env 등 비밀 파일, rm -rf / 같은 파괴 명령)는 어느 모드든 막힌다.

대화창 명령: /help /status /diff /undo /clear /resume /plan /quit  (선택: /spec /graph /run)

선택 기능(스펙 → 그래프 → 재생):
  cap init | spec "<과제>" | graph | run | resume [run] | status | view | rollback <run> <번호>
  각 명령 도움말: cap <명령> -h"""


def _out(s: str = "") -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("utf-8", "replace").decode(sys.stdout.encoding or "utf-8", "replace"), flush=True)


def _ask(prompt: str, auto: str | None = None) -> str:
    if auto is not None:
        _out(prompt + auto + " (자동)")
        return auto
    try:
        return input(prompt)
    except EOFError:
        return ""


def _need_ws() -> Path:
    ws = find_ws()
    if not cap_dir(ws).is_dir():
        raise SystemExit("`.cap/` 가 없다. 작업 폴더에서 먼저 `cap init`.")
    return ws


# ---------------------------------------------------------------- init

def cmd_init(a) -> int:
    ws = Path(a.path or ".").resolve()
    d = cap_dir(ws)
    (d / "runs").mkdir(parents=True, exist_ok=True)
    (d / "snippets").mkdir(exist_ok=True)
    (d / ".gitignore").write_text("*\n", encoding="utf-8")
    ok = Shadow(ws).ensure()
    _out(f"초기화: {d}  (그림자 git: {'준비됨' if ok else 'git 없음 — 스냅샷 끔'})")
    _out("다음: cap spec \"<과제>\"")
    return 0


# ---------------------------------------------------------------- spec

def _show_guards(ws: Path) -> None:
    from .guards import compile_decisions
    sp = specmod.parse(specmod.spec_path(ws).read_text(encoding="utf-8"))
    guards, manual = compile_decisions(sp.decisions)
    if sp.decisions:
        _out(f"결정 감시자: 기계 검사 {len(guards)}/{len(sp.decisions)}줄")
        for g in guards:
            _out(f"  {g.decision_id}: {g.describe()}")
        if manual:
            _out(f"  사람 확인용: {', '.join(manual)}")


def cmd_snippet(a, ws: Path) -> int:
    """cap spec snippets  /  cap spec add <조각> [--set k=v ...]"""
    snips = specmod.list_snippets(ws)
    if a.task == "snippets" or not a.name:
        _out("조각 (프로젝트 .cap/snippets > ~/.cap/snippets > 기본):")
        for k, p in snips.items():
            first = next((l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")), "")
            _out(f"  {k:16s} {first}")
        return 0
    if a.name not in snips:
        raise SystemExit(f"조각 '{a.name}' 이 없다. cap spec snippets 로 목록 보기.")
    text = snips[a.name].read_text(encoding="utf-8")
    given = dict(kv.split("=", 1) for kv in (a.set or []) if "=" in kv)
    for f in specmod.snippet_fields(text):
        val = given.get(f) or _ask(f"  빈칸 {f} = ", "" if a.yes else None).strip()
        text = text.replace("{" + f + "}", val or f"<{f}>")
    p = specmod.spec_path(ws)
    if not p.is_file():
        raise SystemExit("스펙이 없다. 먼저 cap spec \"<과제>\".")
    p.write_text(specmod.insert_snippet(p.read_text(encoding="utf-8"), text), encoding="utf-8")
    _out(f"조각 '{a.name}' 을 끼워 넣음:\n" + "\n".join(l for l in text.splitlines() if l.startswith("- ")))
    if specmod.lock_path(ws).is_file():
        _out("스펙이 잠근 뒤 바뀌었다 → 확인 후 `cap spec --lock` (그다음 cap graph 가 계획을 새로 그림)")
    return 0


def cmd_spec(a) -> int:
    ws = _need_ws()
    auto = "y" if a.yes else None
    if a.task in ("add", "snippets"):
        return cmd_snippet(a, ws)
    if a.lock:
        h = specmod.lock(ws)
        _out(f"스펙 잠금: sha256 {h[:12]}…")
        _show_guards(ws)
        return 0
    if a.show or not a.task:
        p = specmod.spec_path(ws)
        _out(p.read_text(encoding="utf-8") if p.is_file() else "스펙이 없다. cap spec \"<과제>\"")
        return 0
    ledger = Ledger(ws, "spec-" + now_id())
    _out("AI 가 스펙 초안을 쓰는 중…")
    t0 = time.perf_counter()
    sp, questions = specmod.draft(a.task, ws, ledger=ledger)
    _out(f"  ({time.perf_counter() - t0:.1f}s)")
    for n in sp.notes:
        _out(f"  코드 보정: {n}")
    for q in questions:
        _out(f"\n질문 [{q['id']}] {q['question']}")
        for i, o in enumerate(q["options"], 1):
            _out(f"  {i}) {o}")
        ans = _ask("  번호 또는 직접 입력 > ", "1" if a.yes else None).strip()
        choice = q["options"][int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(q["options"]) else (ans or q["options"][0])
        specmod.apply_answer(sp, q["id"], choice, q.get("kind", "text"))
    while True:
        specmod.save(ws, sp, a.task)
        _out("\n" + specmod.spec_path(ws).read_text(encoding="utf-8"))
        ans = _ask("[y] 확정·잠금 / [e] 편집기로 고치기 / [n] 저장만 > ", auto).strip().lower()
        if ans in ("e", "edit"):
            editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
            subprocess.call([editor, str(specmod.spec_path(ws))])
            sp = specmod.parse(specmod.spec_path(ws).read_text(encoding="utf-8"))
            continue
        if ans in ("y", "yes", ""):
            h = specmod.lock(ws)
            _show_guards(ws)
            _out(f"스펙 잠금: {specmod.spec_path(ws)}  sha256 {h[:12]}…   다음: cap graph")
        else:
            _out("저장만 함(잠그지 않음). 고친 뒤 `cap spec --lock`.")
        return 0


# ---------------------------------------------------------------- graph

def cmd_graph(a) -> int:
    ws = _need_ws()
    sp, h = specmod.load_locked(ws)
    ledger = Ledger(ws, "graph-" + now_id())
    spec_text = specmod.spec_path(ws).read_text(encoding="utf-8")
    p = planmod.plan_path(ws)
    plan = planmod.load(ws) if p.is_file() and (a.modify or a.show) and not a.regen else None
    if plan is not None and plan.get("spec_sha256") not in (None, h) and not a.show:
        _out("스펙이 계획을 만든 뒤 다시 잠겼다 → 계획을 새로 그린다.")
        plan = None
    if plan is None:
        _out("AI 가 계획 그래프를 그리는 중…")
        plan = None
        for attempt in range(3):
            raw = planmod.generate(sp, spec_text, ledger=ledger)
            raw["spec_sha256"] = h
            raw["test_cmd"] = sp.test_cmd()
            plan, notes = planmod.attach(raw, sp, has_tests=bool(collect_test_files(ws)))
            errs = planmod.validate(plan)
            if not errs:
                break
            _out("  계획 검증 실패, 다시: " + "; ".join(errs))
        else:
            raise SystemExit("계획 생성 실패")
        for n in notes:
            _out("  자동 부착 " + n)
    if a.show:
        _out(planmod.render_text(plan))
        return 0
    instructions = list(a.modify or [])
    while True:
        if instructions:
            ins = instructions.pop(0)
        else:
            planmod.save(ws, plan)
            _out("\n" + planmod.render_text(plan))
            _out(f"\n저장: {planmod.plan_path(ws)}  (Mermaid: {cap_dir(ws) / 'plan.mmd'})")
            if a.yes or a.modify:
                break
            ins = _ask("수정 지시(예: '테스트 뒤에 린트 넣어'), 엔터 = 확정 > ").strip()
            if not ins:
                break
        _out(f"수정 중: {ins}")
        new = planmod.modify(plan, ins, sp, ledger=ledger)
        new, notes = planmod.attach(new, sp, has_tests=bool(collect_test_files(ws)))
        errs = planmod.validate(new)
        if errs:
            _out("  수정안이 검증에 실패해 버림: " + "; ".join(errs))
            continue
        for n in notes:
            _out("  자동 부착 " + n)
        plan = new
    # 노드별 사전 승인
    risky = [n for n in plan["nodes"] if n["kind"] in ("code", "git")]
    if risky:
        _out("\n노드별 사전 승인 (pre = 실행 중 삭제·이동 같은 '승인 필요' 행동을 묻지 않음; 금지는 그대로 금지)")
        for n in risky:
            default = "n" if n["kind"] == "git" else ("y" if a.yes else None)
            if a.preapprove is not None:
                default = "y" if n["id"] in a.preapprove or "all" in a.preapprove else "n"
            ans = _ask(f"  {n['id']} ({n['kind']}) 사전 승인? [y/N] > ", default).strip().lower()
            n["approve"] = "pre" if ans in ("y", "yes") else "ask"
    planmod.save(ws, plan)
    _out(f"\n계획 확정: {planmod.plan_path(ws)}   다음: cap run")
    return 0


# ---------------------------------------------------------------- run / resume

def _auto(a) -> str | None:
    return "y" if a.yes else ("n" if a.no else None)


def cmd_run(a) -> int:
    from .runner import drive
    ws = _need_ws()
    sp, h = specmod.load_locked(ws)
    plan = planmod.load(ws)
    if plan.get("spec_sha256") and plan["spec_sha256"] != h:
        raise SystemExit("계획이 지금 스펙과 다른 스펙으로 만들어졌다. `cap graph --regen`.")
    run_id = now_id()
    _out(f"재생 시작: run {run_id}  (재개: cap resume {run_id})\n")
    _out(planmod.render_text(plan) + "\n")
    res = drive(ws=ws, plan=plan, sp=sp, run_id=run_id, resume=False, auto=_auto(a), printer=_out, pause=a.pause)
    return 0 if res["status"] == "done" else 2


def cmd_resume(a) -> int:
    from .runner import drive
    ws = _need_ws()
    sp, _ = specmod.load_locked(ws)
    plan = planmod.load(ws)
    run_id = a.run_id or _last_run(ws)
    if not run_id:
        raise SystemExit("이어 갈 실행이 없다.")
    meta = next((m for m in _runs(ws) if m.get("run_id") == run_id), {})
    if meta.get("status") in ("done", "blocked", "stopped") and meta.get("finished"):
        rp = cap_dir(ws) / "runs" / f"{run_id}.report.md"
        _out(f"run {run_id} 은 이미 끝났다({meta['status']}). 새로 돌리려면 cap run.\n")
        if rp.is_file():
            _out(rp.read_text(encoding="utf-8"))
        return 0 if meta["status"] == "done" else 2
    _out(f"재개: run {run_id}\n")
    res = drive(ws=ws, plan=plan, sp=sp, run_id=run_id, resume=True, auto=_auto(a), printer=_out, pause=a.pause)
    return 0 if res["status"] == "done" else 2


def _runs(ws: Path) -> list[dict]:
    out = []
    for p in sorted((cap_dir(ws) / "runs").glob("*.meta.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return out


def _last_run(ws: Path) -> str | None:
    runs = _runs(ws)
    return runs[-1]["run_id"] if runs else None


def cmd_status(a) -> int:
    ws = _need_ws()
    sp_p = specmod.spec_path(ws)
    lock = specmod.lock_path(ws)
    _out(f"작업 폴더: {ws}")
    _out(f"스펙: {'있음' if sp_p.is_file() else '없음'}{', 잠김' if lock.is_file() else ''}")
    _out(f"계획: {'있음' if planmod.plan_path(ws).is_file() else '없음'}")
    runs = _runs(ws)
    if not runs:
        _out("실행 기록 없음")
        return 0
    _out("\n실행 기록:")
    for r in runs[-10:]:
        _out(f"  {r['run_id']}  {r.get('status', '?'):8s}  {r.get('seconds', '-')}s  호출 {r.get('llm_calls', '-')}")
    last = runs[-1]["run_id"]
    rp = cap_dir(ws) / "runs" / f"{last}.report.md"
    if rp.is_file():
        _out("\n최근 보고:\n" + rp.read_text(encoding="utf-8"))
    elif runs[-1].get("status") in ("running", "waiting"):
        _out(f"\n최근 실행이 끝나지 않았다 → cap resume {last}")
    return 0


def cmd_view(a) -> int:
    from .view import write_view
    ws = _need_ws()
    p = write_view(ws, a.run_id or _last_run(ws))
    _out(f"뷰어: {p}")
    if a.open:
        import webbrowser
        webbrowser.open(p.as_uri())
    return 0


def cmd_rollback(a) -> int:
    ws = _need_ws()
    led = Ledger(ws, a.run_id).read()
    cps = {r["no"]: r["sha"] for r in led if r["type"] == "checkpoint"}
    start = next((r.get("sha") for r in led if r["type"] == "run_start"), None)
    if a.cp == 0:
        sha = start
    else:
        sha = cps.get(a.cp)
    if not sha:
        _out("체크포인트: 0(run start) " + " ".join(str(k) for k in sorted(cps)))
        raise SystemExit("그 번호의 체크포인트가 없다.")
    ok = Shadow(ws).restore(sha)
    _out(f"작업 폴더를 체크포인트 {a.cp} ({sha[:10]}) 로 되돌림: {'성공' if ok else '실패'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- 공용 도우미 (/spec·/plan·/run 에서도 씀)

def _ns(**kw):
    return argparse.Namespace(**kw)


def _run_ns(**kw):
    base = dict(yes=False, no=False, pause=False)
    base.update(kw)
    return _ns(**base)


# ---------------------------------------------------------------- main

def use_tui() -> bool:
    """전체 화면 대화창을 쓸까: 입력·출력이 둘 다 실제 터미널이고, CAP_PLAIN 이 없고, textual 이 깔려 있을 때."""
    if os.environ.get("CAP_PLAIN") or os.environ.get("TERM") == "dumb":
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        import textual  # noqa: F401
    except (ImportError, ValueError, AttributeError):
        return False
    return True


def _setup_cmd(args: list[str]) -> int:
    """cap setup [--provider 이름] [--model 모델] [--base-url 주소] [--no-test]  (인자 없으면 대화식)"""
    from .setup import run_setup
    opts: dict = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--provider", "--model", "--base-url") and i + 1 < len(args):
            opts[a[2:].replace("-", "_")] = args[i + 1]
            i += 2
        elif a == "--no-test":
            opts["test"] = False
            i += 1
        else:
            _out("usage: cap setup [--provider NAME] [--model MODEL] [--base-url URL] [--no-test]")
            return 2
    return run_setup(**opts, out=_out)


def _ready_or_setup(interactive: bool = True) -> bool:
    """키가 없으면: 터미널이면 처음 설정을 바로 열고, 아니면 안내만."""
    if os.environ.get("CAP_SKIP_SETUP_CHECK"):
        return True
    from .setup import has_credentials, run_setup
    if has_credentials():
        return True
    if interactive and sys.stdin.isatty():
        _out("No model is set up yet — let's do that first (you can change it later with `cap setup`).")
        return run_setup(out=_out) == 0
    _out("No model is set up: run `cap setup` (or set the API key environment variable of your provider).")
    return False


_VALUE_OPTS = ("--mode", "--approval", "-a", "--provider", "--model", "--profile", "--sandbox")
_FLAG_OPTS = ("--full-auto", "--plain")


def _hoist_exec_options(args: list[str]) -> list[str]:
    """`cap exec --mode auto "할 일"` 처럼 exec 뒤에 쓴 공용 옵션을 앞으로 옮긴다(Codex `exec --full-auto` 습관).
    할 일 문장(따옴표로 묶인 한 덩어리) 앞에 있는 옵션만 옮긴다."""
    if "exec" not in args:
        return args
    k = args.index("exec")
    if any(not a.startswith("-") for a in args[:k]):       # exec 가 할 일 문장 속 단어일 때
        return args
    head, tail, j = args[:k], [], k + 1
    while j < len(args):
        a = args[j]
        if a in _VALUE_OPTS and j + 1 < len(args):
            head += [a, args[j + 1]]
            j += 2
        elif a in _FLAG_OPTS or a.startswith(("--mode=", "--approval=")):
            head.append(a)
            j += 1
        elif a == "resume" and "resume" not in tail:                  # exec resume [--last|세션] … "후속 지시"
            tail.append(a)
            if j + 1 < len(args) and (args[j + 1] in ("--last", "last") or re.match(r"^\d{8}-\d{6}$|^\d{1,2}$", args[j + 1])):
                tail.append(args[j + 1])
                j += 1
            j += 1
        elif a.startswith("-"):                             # exec 전용 옵션(--json·--max-turns N …)은 그대로
            tail.append(a)
            if a in ("--max-turns", "-o", "--output-last-message", "--schema") and j + 1 < len(args):
                tail.append(args[j + 1])
                j += 1
            j += 1
        else:
            break
    return head + ["exec"] + tail + args[j:]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")   # SystemExit 안내문이 Git Bash 에서 깨지던 문제(도그푸딩)
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = _hoist_exec_options(list(sys.argv[1:] if argv is None else argv))
    if args and args[0] in ("--version", "-V"):
        from . import __version__
        _out(f"cap {__version__}")
        return 0
    if args and args[0] in ("setup", "login"):            # 처음 설정: 모델 공급자·키·모델(Codex `codex login`)
        return _setup_cmd(args[1:])
    from .setup import apply_user_default
    apply_user_default()                                  # cap setup 으로 고른 공급자·모델(아래 --provider 가 이김)
    # 앞쪽 공용 옵션: --mode/-a/--approval <모드>, --resume [세션], --plain, -h
    approval, resume, plain, i = "ask", None, False, 0
    while i < len(args) and args[i].startswith("-"):
        a0 = args[i]
        if a0 in ("--mode", "--approval", "-a") and i + 1 < len(args):
            approval, i = args[i + 1], i + 2
        elif a0.startswith(("--mode=", "--approval=")):
            approval, i = a0.split("=", 1)[1], i + 1
        elif a0 == "--full-auto":
            approval, i = "auto", i + 1
        elif a0 == "--plain":
            plain, i = True, i + 1
        elif a0 == "--provider" and i + 1 < len(args):       # Codex: -c model_provider=… / --oss
            from . import llm
            try:
                _out(llm.use_provider(args[i + 1]))
            except ValueError as exc:
                _out(str(exc))
                return 2
            i += 2
        elif a0 == "--model" and i + 1 < len(args):
            from . import llm
            llm.MODEL, i = args[i + 1], i + 2
        elif a0 == "--profile" and i + 1 < len(args):
            from .compat import apply_profile
            pmode, note = apply_profile(args[i + 1])
            if pmode is None and note.startswith("no profile"):
                _out(note)
                return 2
            approval = pmode or approval
            i += 2
        elif a0 == "--sandbox" and i + 1 < len(args):
            from . import sandbox as sbx
            sbx.STATE["mode"], i = args[i + 1].lower(), i + 2
        elif a0 == "--resume":
            if i + 1 < len(args) and re.match(r"^\d{8}-\d{6}$|^\d{1,2}$|^last$", args[i + 1]):
                resume, i = args[i + 1], i + 2
            else:
                resume, i = "last", i + 1
        elif a0 in ("-h", "--help"):
            _out(TOP_HELP)
            return 0
        else:
            break
    try:
        approval = normalize_mode(approval)
    except ValueError as exc:
        _out(str(exc))
        return 2
    rest = args[i:]
    if not rest or (rest[0] not in SUBCOMMANDS and rest[0] not in ("exec", "doctor", "mcp-server", "plugin", "skill", "loop-graph", "sandbox")):
        from .chat import one_shot, repl
        if not _ready_or_setup():
            return 2
        ws = find_ws()
        if not rest:
            if not plain and use_tui():
                from .tui import run_tui
                return run_tui(ws, approval=approval, resume=resume)
            return repl(ws, approval=approval, resume=resume)
        return one_shot(ws, " ".join(rest), approval=approval)
    if rest and rest[0] == "doctor":
        from .chat import doctor_lines
        for ln in doctor_lines(find_ws()):
            _out(ln)
        return 0
    if rest[0] == "plugin":
        from . import compat
        act = rest[1] if len(rest) > 1 else "list"
        if act == "list":
            for ln in compat.describe_plugins(find_ws()):
                _out(ln)
            return 0
        force = "--force" in rest
        args2 = [a for a in rest[2:] if a != "--force"]
        if act == "search" and args2:
            from . import pluginhub
            ok, msg = pluginhub.search(" ".join(args2), limit=15)
            _out(msg)
            return 0 if ok else 1
        if act in ("add", "install") and args2 and "@" in args2[0] and not Path(args2[0]).exists() \
                and not args2[0].startswith(("http", "git@")):
            from . import pluginhub                                  # 이름@마켓 → 마켓플레이스에서
            ok, msg = pluginhub.install(args2[0], force=force)
        elif act in ("add", "install") and args2:
            ok, msg = compat.plugin_add(args2[0])
        elif act in ("remove", "uninstall") and args2:
            ok, msg = compat.plugin_remove(args2[0])
        else:
            _out("사용법: cap plugin list | search <낱말> | add <이름@마켓 | 폴더 | owner/repo | git URL> [--force] | remove <이름>")
            return 2
        _out(msg)
        return 0 if ok else 1
    if rest[0] == "skill":
        from . import skillhub
        act = rest[1] if len(rest) > 1 else ""
        force = "--force" in rest
        args2 = [a for a in rest[2:] if a not in ("--force", "--user")]
        if act == "search" and args2:
            ok, msg = skillhub.search(" ".join(args2), limit=15)
        elif act in ("install", "add") and args2:
            ok, msg = skillhub.install(find_ws(), args2[0], "user" if "--user" in rest else "project", force=force)
        else:
            _out("사용법: cap skill search <낱말> | install <id> [--user] [--force]")
            return 2
        _out(msg)
        return 0 if ok else 1
    if rest[0] == "sandbox":                          # cap sandbox [status|setup|setup wsl|undo]
        from . import sandbox as sbx
        act = " ".join(rest[1:]) or "status"
        if act == "setup":
            ok, msg = sbx.appc_setup() if os.name == "nt" else (True, "Linux uses Landlock automatically.")
            _out(("Done: " if ok else "Failed: ") + msg)
        elif act == "setup wsl":
            ok, msg = sbx.wsl_setup()
            _out(("Done: " if ok else "Failed: ") + (msg.splitlines()[-1] if msg else ""))
        elif act == "undo":
            _out(sbx.appc_undo(find_ws()))
            ok = True
        elif act == "status":
            ok = True
        else:
            _out("usage: cap sandbox [status|setup|setup wsl|undo]")
            return 2
        _out(sbx.describe())
        return 0 if ok else 1
    if rest[0] == "loop-graph":                       # 대화형 코딩 루프의 실제 LangGraph 그래프(Mermaid)
        from .chat import loop_mermaid
        _out(loop_mermaid())
        return 0
    if rest[0] == "mcp-server":
        from .mcp_server import serve
        return serve(find_ws())
    if rest[0] == "exec":
        from .chat import one_shot
        if not _ready_or_setup(interactive=False):
            return 2
        as_json, events = "--json" in rest, "--events" in rest
        schema, resume, max_turns, out_file = None, None, None, None
        words = [w for w in rest[1:] if w not in ("--json", "--events")]
        for flag in ("--max-turns", "-o", "--output-last-message"):
            if flag in words:
                k = words.index(flag)
                if k + 1 >= len(words):
                    _out(f"{flag} needs a value")
                    return 2
                if flag == "--max-turns":
                    if not words[k + 1].isdigit():
                        _out("--max-turns needs a number")
                        return 2
                    max_turns = int(words[k + 1])
                else:
                    out_file = words[k + 1]
                words = words[:k] + words[k + 2:]
        if words and words[0] == "resume":               # Codex: exec resume [--last|<세션>] "후속 지시"
            words = words[1:]
            resume = "last"
            if words and (words[0] in ("--last", "last") or re.match(r"^\d{8}-\d{6}$|^\d{1,2}$", words[0])):
                resume, words = words[0], words[1:]
        if "--schema" in words:                         # Codex exec --output-schema
            k = words.index("--schema")
            try:
                schema = json.loads(Path(words[k + 1]).read_text(encoding="utf-8"))
            except (IndexError, OSError, json.JSONDecodeError) as exc:
                _out(f"--schema needs a JSON schema file: {exc}")
                return 2
            words = words[:k] + words[k + 2:]
            as_json = True
        if not words:
            _out('사용법: cap exec [--json|--events] [--schema 스키마.json] [--max-turns N] [-o 파일] '
                 '[resume [last|세션]] "할 일"')
            return 2
        return one_shot(find_ws(), " ".join(words), approval=approval, as_json=as_json, schema=schema,
                        events=events, resume=resume, max_turns=max_turns, out_file=out_file)
    argv = rest
    ap = argparse.ArgumentParser(prog="cap", description="CAP 선택 기능: 스펙 → 그래프 → 재생 (기본은 `cap` 대화형 코딩 에이전트)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="작업 폴더에 .cap/ 만들기")
    p.add_argument("path", nargs="?")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("spec", help="AI 스펙 초안 → 질문 → 확인 → 잠금")
    p.add_argument("task", nargs="?", help="과제 문장, 또는 'add' / 'snippets'")
    p.add_argument("name", nargs="?", help="cap spec add <조각이름>")
    p.add_argument("--set", nargs="*", help="조각 빈칸 채우기 k=v")
    p.add_argument("--yes", action="store_true", help="질문은 1번, 확인은 y 로 자동")
    p.add_argument("--lock", action="store_true", help="지금 spec.md 를 잠금")
    p.add_argument("--show", action="store_true")
    p.set_defaults(fn=cmd_spec)
    p = sub.add_parser("graph", help="AI 계획 그래프 생성·말로 수정·사전 승인")
    p.add_argument("-m", "--modify", action="append", help="말로 수정 지시(여러 번 가능)")
    p.add_argument("--yes", action="store_true", help="수정 없이 확정, code 노드 사전 승인")
    p.add_argument("--regen", action="store_true", help="기존 plan.yaml 무시하고 새로 생성")
    p.add_argument("--show", action="store_true")
    p.add_argument("--preapprove", nargs="*", help="사전 승인할 노드 id (all 가능)")
    p.set_defaults(fn=cmd_graph)
    for name, fn, h in (("run", cmd_run, "그래프 재생"), ("resume", cmd_resume, "멈춘 실행 이어서")):
        p = sub.add_parser(name, help=h)
        if name == "resume":
            p.add_argument("run_id", nargs="?")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--yes", action="store_true", help="승인 질문에 자동 y (사람 넘김은 중단)")
        g.add_argument("--no", action="store_true", help="승인 질문에 자동 n")
        g.add_argument("--pause", action="store_true", help="승인·질문에서 멈추고 종료(나중에 cap resume)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("status", help="스펙·계획·실행 상태")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("view", help="정적 HTML 뷰어(Mermaid) 생성")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_view)
    p = sub.add_parser("rollback", help="그림자 git 체크포인트로 작업 폴더 되돌리기")
    p.add_argument("run_id")
    p.add_argument("cp", type=int)
    p.set_defaults(fn=cmd_rollback)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        _out("\n중단됨. 이어서: cap resume")
        return 130
    except RuntimeError as exc:   # 모델 서버 오류 등: 상태는 체크포인터에 남아 있다
        _out(f"\n오류: {llm.scrub(str(exc))[:300]}")
        if a.cmd in ("run", "resume"):
            _out("실행 상태는 .cap/checkpoints.sqlite 에 저장돼 있다. 원인을 고친 뒤: cap resume")
        return 1
    finally:
        if llm.STATS["calls"]:
            _out(f"\n[모델 호출 {llm.STATS['calls']}회, {llm.STATS['seconds']:.0f}s, "
                 f"토큰 in {llm.STATS['prompt_tokens']} / out {llm.STATS['completion_tokens']}]")


if __name__ == "__main__":
    sys.exit(main())
