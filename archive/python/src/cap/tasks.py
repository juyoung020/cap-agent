"""백그라운드 작업 (Claude Code 의 백그라운드 에이전트, Codex 클라우드 작업과 같은 생각을 이 컴퓨터에서).

/task <할 일>  → 이 저장소의 git worktree(새 브랜치 cap/task-N, 지금 HEAD 에서)를 .cap/worktrees/task-N 에 만들고,
                 그 안에서 `cap -a auto exec --json "<할 일>"` 을 백그라운드로 돌린다. 지금 대화는 그대로 이어 간다.
/tasks         → 작업마다 상태(돌고 있음·끝남), 끝났으면 결과 JSON(상태·답·바뀐 파일·검증)과 브랜치의 변경 요약.
/tasks diff N  → 그 작업 브랜치의 diff.   /tasks remove N → worktree 와 브랜치 정리.
합치기는 사람이 `git merge cap/task-N` 으로 한다(다른 작업과 충돌하지 않게 브랜치를 나눈 것).
비대화식이라 승인이 필요한 행동은 거절된다(auto 모드: 위험한 명령만 해당). 커밋하지 않은 변경은 worktree 에 들어가지 않는다.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _file(ws: Path) -> Path:
    return ws / ".cap" / "tasks.json"


def _load(ws: Path) -> list[dict]:
    try:
        return json.loads(_file(ws).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save(ws: Path, items: list[dict]) -> None:
    _file(ws).parent.mkdir(parents=True, exist_ok=True)
    _file(ws).write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def _git(ws: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)


def start(ws: Path, prompt: str, mode: str = "auto", command: str | None = None) -> tuple[bool, str]:
    """command: 시험에서 실제 CAP 대신 돌릴 명령."""
    from .extras import bg_start
    if not prompt.strip():
        return False, "usage: /task <what to do>"
    if _git(ws, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return False, "background tasks need a git repository (they run on their own branch). Run `git init` first."
    if _git(ws, "rev-parse", "--verify", "HEAD").returncode != 0:
        return False, "the repository has no commit yet — commit once so the task has something to branch from"
    items = _load(ws)
    n = max([t["n"] for t in items] + [0]) + 1
    branch, wt = f"cap/task-{n}", ws / ".cap" / "worktrees" / f"task-{n}"
    r = _git(ws, "worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        return False, "git worktree failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200]
    dirty = bool(_git(ws, "status", "--porcelain", "--untracked-files=no").stdout.strip())
    cmd = command or (f"{shlex.quote(Path(sys.executable).as_posix())} -m cap.cli -a {shlex.quote(mode)} exec --json "
                      f"{shlex.quote(prompt)}")
    from . import llm
    try:
        key_env = {"KAU_API_KEY": llm._load_key()}        # 자식도 CAP 자신이라 모델 키만 넘긴다(다른 비밀 값은 지운 채)
    except RuntimeError:
        key_env = {}
    pid = bg_start(wt, cmd, extra_env=key_env)
    items.append({"n": n, "prompt": prompt[:300], "branch": branch, "worktree": str(wt), "bg": pid,
                  "started": time.time(), "base": _git(ws, "rev-parse", "--short", "HEAD").stdout.strip()})
    _save(ws, items)
    return True, (f"task {n} started on branch {branch} (worktree {wt.relative_to(ws).as_posix()}). "
                  "Keep working here; check it with /tasks."
                  + (" Note: your uncommitted changes are not in the task's copy." if dirty else ""))


def _result(wt: Path, ws: Path, bg: str) -> tuple[str, dict]:
    from .extras import bg_output
    ok, out = bg_output(wt, bg, tail=20000)
    state = "running" if "running]" in out.splitlines()[0] else "finished" if ok else "unknown"
    if not ok:                                   # 이 CAP 이 띄운 것이 아니면(다시 연 세션) 로그 파일을 직접 읽는다
        log = wt / ".cap" / "bg" / f"{bg}.log"
        out = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        state = "finished" if out.strip().endswith("}") else "unknown"
    res = {}
    for ln in reversed(out.splitlines()):
        if ln.startswith("{") and '"status"' in ln:
            try:
                res = json.loads(ln)
                state = "finished"
            except json.JSONDecodeError:
                pass
            break
    return state, res


def finished(ws: Path) -> list[tuple[int, dict]]:
    """끝난 작업 [(번호, 결과)] — 대화창 알림용(git 은 부르지 않는다)."""
    out = []
    for t in _load(ws):
        wt = Path(t["worktree"])
        if wt.is_dir():
            state, res = _result(wt, ws, t["bg"])
            if state == "finished":
                out.append((t["n"], res))
    return out


def describe(ws: Path) -> list[str]:
    items = _load(ws)
    if not items:
        return ["No background tasks. Start one with /task <what to do>."]
    out = []
    for t in items:
        wt = Path(t["worktree"])
        state, res = _result(wt, ws, t["bg"]) if wt.is_dir() else ("removed", {})
        mins = (time.time() - t["started"]) / 60
        line = f"{t['n']}) [{state}] {t['prompt'][:70]}  · {t['branch']} · {mins:.0f} min"
        out.append(line)
        if res:
            stat = _git(ws, "diff", "--shortstat", f"{t['base']}...{t['branch']}").stdout.strip()
            uncommitted = _git(wt, "status", "--porcelain").stdout.strip()
            out.append(f"   status {res.get('status')} · {res.get('verify_note') or 'not verified'} · "
                       f"changed {', '.join(res.get('changed') or []) or 'nothing'}")
            if res.get("answer"):
                out.append("   " + " ".join(str(res["answer"]).split())[:200])
            if uncommitted:
                out.append(f"   the task's changes are uncommitted in {Path(t['worktree']).relative_to(ws).as_posix()} — "
                           f"commit there, then `git merge {t['branch']}`" + (f" ({stat})" if stat else ""))
            elif stat:
                out.append(f"   merge with `git merge {t['branch']}` ({stat})")
    return out


def diff(ws: Path, n: int) -> str:
    t = next((x for x in _load(ws) if x["n"] == n), None)
    if not t:
        return f"no task {n}"
    wt = Path(t["worktree"])
    if not wt.is_dir():
        return f"task {n} was removed"
    _git(wt, "add", "-A")
    d = _git(wt, "diff", "--cached", "--no-color", t["base"]).stdout
    _git(wt, "reset", "-q")
    return d or "(no changes)"


def commit(ws: Path, n: int) -> str:
    """/tasks commit n: 작업 worktree 의 변경을 그 브랜치에 커밋한다(그다음 `git merge cap/task-n`)."""
    t = next((x for x in _load(ws) if x["n"] == n), None)
    if not t or not Path(t["worktree"]).is_dir():
        return f"no task {n}"
    wt = Path(t["worktree"])
    _git(wt, "add", "-A")
    r = _git(wt, "commit", "-q", "-m", f"cap task {n}: {t['prompt'][:60]}")
    if r.returncode != 0:
        return "nothing to commit" if "nothing to commit" in (r.stdout + r.stderr) else \
            "commit failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:160]
    stat = _git(ws, "diff", "--shortstat", f"{t['base']}...{t['branch']}").stdout.strip()
    return f"committed on {t['branch']} ({stat}). Merge it here with: git merge {t['branch']}"


def remove(ws: Path, n: int) -> str:
    from .extras import bg_kill
    items = _load(ws)
    t = next((x for x in items if x["n"] == n), None)
    if not t:
        return f"no task {n}"
    wt = Path(t["worktree"])
    if wt.is_dir():
        from .extras import _PROCS
        bg_kill(wt, t["bg"])
        ent = _PROCS.pop(f"{t['bg']}@{wt}", None)           # 열린 로그 파일을 닫아야 Windows 에서 폴더가 지워진다
        if ent:
            try:
                ent["file"].close()
            except OSError:
                pass
        _git(ws, "worktree", "remove", "--force", str(wt))
        if wt.exists():
            from .store import rmtree
            rmtree(wt)
    _git(ws, "worktree", "prune")
    _git(ws, "branch", "-D", t["branch"])
    _save(ws, [x for x in items if x["n"] != n])
    return f"removed task {n} (worktree and branch {t['branch']})"
