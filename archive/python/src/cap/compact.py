"""대화형(일반 코딩 에이전트) 모드의 기록 정리·압축과 작은 도우미.

- normalize: 결과 없는 tool_call 에 'aborted' 결과를 채우고 짝 없는 결과를 지운다(Codex 07 §3 normalize.rs).
- shrink_old: 지난 턴의 큰 도구 출력·인자를 줄인다(Hermes 04 §3 "도구 출력부터 줄인다", LLM 호출 없음).
- summarize: 그래도 크면 같은 모델로 4항목 요약(Codex 07 §6.1 prompt.md), 머리말은 Hermes 04 §5 SUMMARY_PREFIX 요지,
  최근 사용자 원문을 코드가 그대로 붙인다(Hermes 04 §6 'User Messages (verbatim)', Codex 07 §6.3).
"""

from __future__ import annotations

import difflib
import io
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import llm

# 16k 창에서 한 턴 안에 도구 출력이 5~8k 토큰 붙으므로 턴 시작 시점 기준은 창의 절반으로 둔다.
# 창 크기는 공급자를 고른 뒤에야 정해지므로(import 시점엔 기본 16k) 읽을 때마다 compact_at() 으로 계산한다.
COMPACT_AT: int | None = None      # 정수를 넣으면 그 값으로 고정(시험·실험용)
OLD_TOOL_KEEP = 300
PRUNE_OVER = 400                   # prune_turn: 이보다 긴 도구 출력만 한 줄로 줄인다
PRUNE_FAIL_KEEP = 300              # 실패한 도구 출력은 앞부분을 이만큼 남긴다


def compact_at() -> int:
    """턴 시작 압축 기준(토큰): COMPACT_AT 을 넣었으면 그 값, 아니면 지금 창의 절반."""
    return COMPACT_AT if COMPACT_AT is not None else int(llm.CONTEXT_WINDOW * 0.5)
ABORTED = "aborted: 사용자가 턴을 중단해서 이 호출은 실행되지 않았다"

SUMMARY_PROMPT = """[CONTEXT CHECKPOINT] 지금까지의 대화를 다른 모델이 이어받을 수 있게 요약해라. 도구를 부르지 말고 글로만.
아래 4개 소제목을 그대로 쓰고 각 1~4줄:
1. 진행 상황과 결정: 무엇을 바꿨나(파일·함수), 테스트 결과
2. 사용자의 제약·선호
3. 남은 일
4. 정확한 값: 경로, 함수명, 명령, 오류문(글자 그대로)"""

SUMMARY_PREFIX = ("[이전 대화 요약 — 참고용] 앞선 대화가 길어서 요약으로 바꿨다. 이미 한 일은 반복하지 말고 이어서 해라. "
                  "여기 적힌 요청에 다시 답하지 말고 맨 아래의 새 요청만 처리해라. 도구는 계속 쓸 수 있다. "
                  "파일의 현재 내용은 요약이 아니라 파일을 다시 읽어서 확인해라.")
SUMMARY_END = "--- 요약 끝: 아래 새 요청에 답해라 ---"


def normalize(msgs: list[dict]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        i += 1
        if m.get("role") == "tool":
            continue                       # 앞의 assistant 묶음에 붙지 않은 결과(고아)
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            seen = set()
            while i < len(msgs) and msgs[i].get("role") == "tool":
                if msgs[i].get("tool_call_id") in ids and msgs[i]["tool_call_id"] not in seen:
                    out.append(msgs[i])
                    seen.add(msgs[i]["tool_call_id"])
                i += 1
            for tid in ids:
                if tid not in seen:
                    out.append({"role": "tool", "tool_call_id": tid, "content": ABORTED})
    return out


def _short_args(raw: str, limit: int = 400) -> str:
    if len(raw) <= limit:
        return raw
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict):
        return raw
    for k, v in obj.items():
        if isinstance(v, str) and len(v) > 200:
            obj[k] = v[:150] + f"…[이전 턴 인자 {len(v)}자 생략]"
    return json.dumps(obj, ensure_ascii=False)


def shrink_old(msgs: list[dict]) -> list[dict]:
    """지금 있는 메시지는 전부 지난 턴 것이다(새 사용자 메시지를 붙이기 전에 부른다)."""
    out = []
    for m in msgs:
        m = dict(m)
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str) and len(c) > OLD_TOOL_KEEP + 200:
            m["content"] = c[:OLD_TOOL_KEEP] + f"\n…[이전 턴 출력 {len(c)}자 생략 — 필요하면 다시 읽어라]"
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m["tool_calls"] = [{**tc, "function": {**tc["function"],
                                                   "arguments": _short_args(tc["function"].get("arguments") or "{}")}}
                               for tc in m["tool_calls"]]
        if m.get("role") == "user" and isinstance(c, str) and c.startswith("[harness]") and len(c) > 800:
            m["content"] = c[:500] + "\n…[생략]"
        out.append(m)
    return out


_DATA_WRAP = re.compile(r"\A(<data source=\"[^\"]*\">\n)(.*)(\n</data>)\Z", re.S)


_FAIL_LINE = re.compile(r"^(FAILED |ERROR |E\s+\S|Traceback|\S+\.\w+:\d+(:|\s|$))|\b\w*(Error|Exception)\b")


def failure_digest(body: str, max_lines: int = 15, max_chars: int = 900) -> str:
    """테스트·명령 출력에서 무엇이 왜 실패했는지 보여 주는 줄만(FAILED·E 줄·파일:줄·오류 이름).
    실사용에서 이 줄을 버리자 모델이 무엇이 실패했는지 잊고 같은 파일만 다시 읽다 턴 한도에 닿았다."""
    keep, used = [], 0
    for ln in body.splitlines():
        s = ln.rstrip()
        if not s or not _FAIL_LINE.search(s.lstrip()):
            continue
        s = s[:200]
        if used + len(s) > max_chars or len(keep) >= max_lines:
            break
        keep.append(s)
        used += len(s) + 1
    return "\n".join(keep)


_DEF_LINE = re.compile(r"^\s*(\d+)\|\s*(?:async\s+def|def|class|function|fn|func|pub\s+fn|struct|interface)\s+([A-Za-z_]\w*)")


def read_outline(body: str, limit: int = 300) -> str:
    """줄인 read_file 결과에 남길 지도: 그 범위에 정의된 함수·클래스와 줄 번호(look@115, Engagement@85).
    실사용에서 압축으로 읽은 내용이 지워지자 모델이 무엇이 어디 있었는지 잊고 엉뚱한 파일에서 찾다 추측으로 답했다."""
    out, used = [], 0
    for ln in body.splitlines():
        m = _DEF_LINE.match(ln)
        if m:
            item = f"{m.group(2)}@{m.group(1)}"
            if used + len(item) > limit:
                out.append("…")
                break
            out.append(item)
            used += len(item) + 2
    return ", ".join(out)


def _prune_one(name: str, args: dict, body: str) -> str:
    """이번 턴의 오래된 도구 출력 하나를 한 줄로(실패 출력은 앞부분만)."""
    first = body.split("\n", 1)[0]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    last = lines[-1][:200] if lines else ""
    if name == "read_file":
        m = re.match(r"\[(.+?) 줄 (\d+)-(\d+) / 전체 (\d+)\]", first)
        where = (f"{m.group(1)} lines {m.group(2)}-{m.group(3)} of {m.group(4)}" if m
                 else str(args.get("path", "")))
        defs = read_outline(body)
        return (f"[pruned] read_file {where} ({len(body)} chars) — content removed to save context"
                + (f"; it defines: {defs}" if defs else "") + ". Read only the part you need again if needed.")
    if name in ("run", "run_tests"):
        m = re.match(r"\[(?:(.*) → )?exit (-?\d+)\]", first)
        cmd = (m.group(1) if m and m.group(1) else "") or str(args.get("cmd") or args.get("target") or name)
        code = m.group(2) if m else "?"
        head = f"[pruned] {name} `{cmd[:200]}` → exit {code} ({len(body)} chars); last line: {last}"
        digest = failure_digest(body)
        return head + ("\nkept failure lines:\n" + digest if digest else "")
    if name == "search":
        return (f"[pruned] search `{str(args.get('pattern', ''))[:120]}` in {args.get('path') or '.'} → "
                f"{len(lines)} matching lines (output removed; search again if needed)")
    if name in ("edit_file", "write_file", "apply_patch") and (body.startswith("편집 적용") or re.match(r"\S+ 저장\(\d+자\)", body)):
        return f"[pruned] {name} {args.get('path', '')}: applied — {first[:120]}"
    return body[:PRUNE_FAIL_KEEP] + f"\n…[pruned {len(body) - PRUNE_FAIL_KEEP} more chars to save context]"


def _is_test_call(tc: dict | None) -> bool:
    if not tc:
        return False
    name = tc.get("function", {}).get("name", "")
    if name == "run_tests":
        return True
    if name != "run":
        return False
    try:
        cmd = str(json.loads(tc["function"].get("arguments") or "{}").get("cmd", ""))
    except (json.JSONDecodeError, AttributeError):
        return False
    return bool(re.search(r"\b(pytest|unittest|npm (run )?test|cargo test|go test|make test)\b", cmd))


def prune_turn(msgs: list[dict], start: int, keep_rounds: int = 2) -> list[dict]:
    """이번 턴(start 뒤)의 도구 결과 중 마지막 keep_rounds 묶음 전 것의 긴 출력을 한 줄로 바꾼다(LLM 호출 없음).
    도구 호출 인자·<data> 감싸기·호출과 결과의 짝은 그대로 둔다."""
    rounds = [i for i in range(max(start + 1, 0), len(msgs))
              if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")]
    if len(rounds) <= keep_rounds:
        return list(msgs)
    stop = rounds[-keep_rounds] if keep_rounds > 0 else len(msgs)
    out = list(msgs)
    calls: dict[str, dict] = {}
    for i in range(rounds[0], len(msgs)):
        if msgs[i].get("role") == "assistant":
            calls.update({tc["id"]: tc for tc in msgs[i].get("tool_calls") or []})
    # 이번 턴의 가장 최근 테스트 결과는 줄이지 않는다(무엇이 실패했는지가 다음 행동의 근거)
    latest_test = max((i for i in range(rounds[0], len(msgs)) if msgs[i].get("role") == "tool"
                       and _is_test_call(calls.get(msgs[i].get("tool_call_id")))), default=-1)
    for i in range(rounds[0], stop):
        m = msgs[i]
        if m.get("role") == "assistant" or i == latest_test:
            continue
        c = m.get("content")
        if m.get("role") != "tool" or not isinstance(c, str) or len(c) <= PRUNE_OVER:
            continue
        tc = calls.get(m.get("tool_call_id"))
        if tc is None:
            continue
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        args = args if isinstance(args, dict) else {}
        w = _DATA_WRAP.match(c)
        body = w.group(2) if w else c
        if len(body) <= PRUNE_OVER or body.startswith("[pruned]"):
            continue
        short = _prune_one(tc["function"]["name"], args, body)
        if len(short) >= len(body):
            continue
        out[i] = {**m, "content": (w.group(1) + short + w.group(3)) if w else short}
    return out


def user_requests(msgs: list[dict], limit: int = 5, each: int = 300, total: int = 1200) -> list[str]:
    """실제 사용자 요청(하네스 알림·요약 제외)을 최신부터."""
    out, used = [], 0
    for m in reversed(msgs):
        c = m.get("content")
        if m.get("role") != "user" or not isinstance(c, str) or c.startswith("[harness]"):
            continue
        if SUMMARY_END in c:
            c = c.split(SUMMARY_END, 1)[1]
        c = c.strip()
        if c.startswith("(The user interrupted"):
            c = c.split(")", 1)[-1].strip()
        c = c[:each]
        if not c or used + len(c) > total or len(out) >= limit:
            break
        out.append(c)
        used += len(c)
    return out


def summarize(msgs: list[dict], ledger=None, focus: str = "") -> str:
    reqs = user_requests(msgs)
    prompt = SUMMARY_PROMPT + (f"\n\n특히 이것을 자세히 남겨라(사용자 지시): {focus}" if focus else "")
    try:
        r = llm.chat(msgs + [{"role": "user", "content": prompt}], max_tokens=700, temperature=0.1,
                     ledger=ledger, tag="compact", think=False)
        body = (r["content"] or "").strip() or "(요약 없음)"
    except RuntimeError as exc:   # 요약 실패: Hermes 04 §2 처럼 가운데를 '요약 없음'으로 버린다
        body = f"(요약 실패: {llm.scrub(str(exc))[:120]})"
    lines = [SUMMARY_PREFIX, "", body]
    if reqs:
        lines += ["", "사용자의 이전 요청(원문, 최신순):"] + [f"- {q}" for q in reqs]
    lines += ["", SUMMARY_END]
    return "\n".join(lines)


# ---------------------------------------------------------------- 테스트 도우미

def _norm_test_cmd(cmd: str) -> list[str]:
    toks = (cmd or "").split()
    while toks and toks[0] in ("python", "python3", "py", "-m", "uv", "run", "poetry"):
        toks = toks[1:]
    return toks


def is_full_test_run(cmd: str, test_cmd: str) -> bool:
    """`python -m pytest -q` 와 `pytest`, `pytest -x` 는 같은 전체 실행. 파일·노드를 지정하면 부분 실행(Hermes 07 §8-4)."""
    a, b = _norm_test_cmd(cmd), _norm_test_cmd(test_cmd)
    if not a or not b or a[0] != b[0] or re.search(r"[;&|]", cmd or ""):
        return False
    return all(t.startswith("-") for t in a[1:])


def test_summary(out: str, code: int) -> str:
    for ln in reversed((out or "").strip().splitlines()):
        m = re.search(r"(\d+ (passed|failed|errors?|skipped|xfailed)[^=]*?)(\s+in\s+[\d.]+s)?\s*=*\s*$", ln)
        if m:
            return m.group(1).strip()
        if "no tests ran" in ln:
            return "no tests ran"
    return "통과" if code == 0 else f"실패(exit {code})"


def _test_id(raw: str) -> str:
    """Windows 의 `tests\\test_a.py::t` 도 `tests/test_a.py::t` 로(파일 부분만 — 매개변수 id 는 그대로)."""
    path, sep, rest = raw.partition("::")
    return path.replace("\\", "/") + sep + rest


def failing_tests(out: str, code: int) -> list[str]:
    ids = sorted({_test_id(m.group(2)) for m in re.finditer(r"^(FAILED|ERROR)\s+(\S+)", out or "", re.M)})
    if not ids and code not in (0, 5):
        ids = [f"<exit {code}>"]
    return ids


def is_pytest(cmd: str) -> bool:
    toks = _norm_test_cmd(cmd)
    return bool(toks) and toks[0] == "pytest" and not re.search(r"[;&|]", cmd or "")


def baseline_failures(ws: Path, sha: str, test_cmd: str, sandbox: str = "") -> tuple[list[str], bool]:
    """턴 시작 스냅샷(그림자 git)을 작업 폴더의 .cap/base-* 에 풀어 같은 테스트를 돌려, 원래부터 실패하던 테스트를 센다.
    (작업 폴더 안이라야 Windows AppContainer 격리가 그 폴더에서 돌 수 있다.)
    반환: (실패 목록, 완전한가). 수집 오류(그 파일의 테스트는 돌지 못함)·시간 초과·스냅샷 없음이면 완전하지 않다 —
    그때 목록에 없는 테스트가 전에 통과했다고 말할 수 없다."""
    from .store import CAP_DIR, Shadow
    from .tools import run_shell
    data = Shadow(ws).archive(sha) if sha else None
    if not data:
        return [], False
    cmd = test_cmd
    if is_pytest(cmd) and "--continue-on-collection-errors" not in cmd:
        cmd += " --continue-on-collection-errors"     # 한 파일의 import 오류로 나머지 테스트까지 멈추지 않게
    root = ws / CAP_DIR
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="base-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(tmp)
        code, out = run_shell(cmd, tmp, sandbox=sandbox, pyws=ws)     # 원래 작업 폴더의 파이썬(.venv)으로
        if code == 0:
            return [], True
        ids = failing_tests(out, code)
        broken = (code == 124 or "Interrupted:" in out or "ERROR collecting" in out
                  or any(t.startswith("<exit") or "::" not in t for t in ids))
        return ids, not broken
    finally:
        _rmtree(tmp)


# ---------------------------------------------------------------- 화면용 미리보기

def read_before(ws: Path, name: str, args: dict) -> str | None:
    if name not in ("edit_file", "write_file"):
        return None
    p = Path(args.get("path") or "")
    p = p if p.is_absolute() else ws / p
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    except OSError:
        return None


def diff_lines(before: str | None, after: str, n: int = 0) -> tuple[int, int, list[str]]:
    a = (before or "").splitlines()
    b = after.splitlines()
    plus = minus = 0
    lines = []
    for ln in difflib.unified_diff(a, b, lineterm="", n=n):
        if ln.startswith(("---", "+++", "@@")):
            continue
        if ln.startswith("+"):
            plus += 1
        elif ln.startswith("-"):
            minus += 1
        lines.append(ln)
    return plus, minus, lines


def preview(ws: Path, name: str, args: dict, limit: int = 12) -> list[str]:
    """승인 질문에 보여 줄 짧은 diff/명령(부작용 없음)."""
    if name == "edit_file":
        before = read_before(ws, name, args)
        if before is None:
            return [f"(파일 없음: {args.get('path')})"]
        from .tools import apply_edit_args
        res = apply_edit_args(before, args)
        if not res.ok or res.new_text is None:
            return [f"(적용 불가: {res.message[:80]})"]
        _, _, lines = diff_lines(before, res.new_text)
    elif name == "write_file":
        before = read_before(ws, name, args)
        _, _, lines = diff_lines(before, args.get("content") or "")
    elif name == "run":
        return [f"$ {args.get('cmd', '')}"]
    else:
        return []
    return lines[:limit] + ([f"… +{len(lines) - limit} lines"] if len(lines) > limit else [])


def _rmtree(path) -> None:
    from .store import rmtree
    rmtree(path)
