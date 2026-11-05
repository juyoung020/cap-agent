"""코딩 루프 도구 7개(+report_conflict). 문자열 치환 편집은 느슨한 매칭 3단계.

ToolBox 는 실행기다. 권한 판정은 policy.classify 가 먼저 한다(여기서는 안 함).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import is_secret_path
from .store import IGNORE_DIRS, file_hash, iter_files, rel

MAX_OUT = 6000  # 도구 출력 최대 글자 수(머리+꼬리)


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMAS = [
    _fn("read_file", "Read a text file. Each output line starts with its line number and '| ' "
        "(not part of the file — never copy it into edit_file). Optional 1-based line window.",
        {"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based start line"},
         "limit": {"type": "integer", "description": "number of lines"}}, ["path"]),
    _fn("search", "Regex search in workspace files. Returns path:line: text.",
        {"pattern": {"type": "string"}, "path": {"type": "string", "description": "dir or file, default ."}},
        ["pattern"]),
    _fn("edit_file", "Replace text in a file. `old` must be copied from the file (with indentation) "
        "and be unique; include a nearby line if needed. To change several places in one file at once, pass "
        "`edits` (a list of {old, new}, applied top to bottom; all or nothing) instead of old/new. "
        "On success it returns a short diff of what changed, so you do not need to read the file again.",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
         "edits": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
             "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["old", "new"]}}},
        ["path"]),
    _fn("write_file", "Create a new file, or overwrite a file you have fully read.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("run", "Run a shell command in the workspace (bash on this machine). Set background=true for servers or "
        "long jobs; then check them with the process tool. A command still running after 30 s (or `timeout` seconds) "
        "keeps running in the background as bgN. Set escalate=true only if the command failed because of "
        "the sandbox (no network, no writes outside the workspace) and really needs it — the user will be asked.",
        {"cmd": {"type": "string"}, "background": {"type": "boolean"}, "escalate": {"type": "boolean"},
         "timeout": {"type": "number", "description": "seconds to wait before moving it to the background (max 600)"}},
        ["cmd"]),
    _fn("run_tests", "Run the project's tests. target: optional test file or node id.",
        {"target": {"type": "string"}}, []),
    _fn("todo", "Track your plan. items: list of {text, done}.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "done": {"type": "boolean"}}, "required": ["text"]}}}, ["items"]),
    _fn("report_conflict", "Use only if the tests contradict the task/spec and cannot be passed honestly.",
        {"reason": {"type": "string"}}, ["reason"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
REQUIRED = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in TOOL_SCHEMAS}
WRITE_TOOL_NAMES = {"edit_file", "write_file", "run"}


def render_notebook(raw: str, out_max: int = 1500) -> str:
    """.ipynb(JSON) → 사람이 읽는 셀 목록. 그림 출력은 표시만, 긴 출력은 자른다."""
    import json as _json
    try:
        nb = _json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return raw
    out = []
    for k, c in enumerate(nb["cells"], 1):
        src = c.get("source", "")
        src = "".join(src) if isinstance(src, list) else str(src)
        kind = c.get("cell_type", "?")
        head = f"# ── cell {k} [{kind}]" + (f" (run {c.get('execution_count')})" if c.get("execution_count") else "")
        out += [head, src.rstrip("\n")]
        texts = []
        for o in c.get("outputs") or []:
            if o.get("output_type") == "stream":
                t = o.get("text", "")
                texts.append("".join(t) if isinstance(t, list) else str(t))
            elif o.get("output_type") in ("execute_result", "display_data"):
                data = o.get("data") or {}
                if "text/plain" in data:
                    t = data["text/plain"]
                    texts.append("".join(t) if isinstance(t, list) else str(t))
                imgs = [m for m in data if m.startswith("image/")]
                if imgs:
                    texts.append(f"[{', '.join(imgs)} output not shown]")
            elif o.get("output_type") == "error":
                texts.append(f"{o.get('ename', 'Error')}: {o.get('evalue', '')}")
        if texts:
            body = "\n".join(t.rstrip("\n") for t in texts)
            if len(body) > out_max:
                body = body[:out_max] + f"\n…[{len(body) - out_max} chars of output omitted]"
            out += ["# ── output", body]
    return "\n".join(out)


OUTPUT_KEEP = 30   # .cap/outputs 에 남겨 두는 긴 출력 파일 수


def with_failures(out: str, code: int | None) -> str:
    """실패한 테스트 출력 맨 앞에 실패 요약(FAILED·E 줄·파일:줄). 긴 출력은 앞뒤만 남기므로 가운데의
    '무엇이 왜 틀렸나'가 잘려 모델이 엉뚱한 파일을 헤맸다(실사용)."""
    if not code:
        return out
    from .compact import failure_digest
    digest = failure_digest(out)
    return f"Failures (summary):\n{digest}\n--- full output ---\n{out}" if digest else out


def spill(ws: Path, text: str, limit: int = MAX_OUT) -> str:
    """긴 출력은 머리·꼬리만 모델에 주고, 원문 전체는 .cap/outputs/ 에 남겨 이어 읽게 한다(Claude Code 방식).
    파일에도 비밀 값은 가린 채 쓴다."""
    if len(text) <= limit:
        return text
    import time
    d = ws / ".cap" / "outputs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = f"out-{time.strftime('%Y%m%d-%H%M%S')}-{time.perf_counter_ns() % 100000:05d}.txt"
        (d / name).write_text(redact(text.replace("\r\n", "\n"), secret_values(ws)), encoding="utf-8", newline="\n")
        for old in sorted(d.glob("out-*.txt"))[:-OUTPUT_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        return clip(text, limit)
    n = text.count("\n") + 1
    return (clip(text, limit) + f"\n[full output ({n} lines) saved to .cap/outputs/{name} — "
            f"read_file it with offset/limit to see the omitted part]")


def clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    return f"{head}\n…[{len(text) - limit}자 생략]…\n{tail}"


# ---------------------------------------------------------------- 편집 매칭

@dataclass
class EditResult:
    ok: bool
    message: str
    stage: str = ""          # exact | rstrip | indent | unicode | spaces | already_applied
    new_text: str | None = None


def _find_line_block(lines: list[str], old_lines: list[str], norm) -> list[int]:
    n, m = len(lines), len(old_lines)
    if m == 0:
        return []
    target = [norm(x) for x in old_lines]
    hits = []
    for i in range(n - m + 1):
        if all(norm(lines[i + k]) == target[k] for k in range(m)):
            hits.append(i)
    return hits


def _strip_blank_edges(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def _indent_of(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s)


# 모델이 ASCII 로 옮겨 적는 유니코드 문장부호: 대시·빼기, 둥근 따옴표, 말줄임표, 특수 공백
_UNI = str.maketrans({**{chr(c): "-" for c in range(0x2010, 0x2016)}, "−": "-",
                      "‘": "'", "’": "'", "‚": "'", "‛": "'",
                      "“": '"', "”": '"', "„": '"', "‟": '"', "…": "...",
                      " ": " ", **{chr(c): " " for c in range(0x2002, 0x200b)}, " ": " ", "　": " "})


def _unicode(s: str) -> str:
    return s.translate(_UNI).strip()


def _match_places(text: str, old: str, limit: int = 5) -> str:
    """정확 일치가 여러 곳일 때 줄 번호를 보여 준다(Codex 에 없는 보강, D4: 'L줄: 내용' 최대 5개)."""
    out, start = [], 0
    while len(out) < limit:
        k = text.find(old, start)
        if k < 0:
            break
        ln = text.count("\n", 0, k) + 1
        line = text.splitlines()[ln - 1] if text.splitlines() else ""
        out.append(f"L{ln}: {line.strip()[:60]}")
        start = k + 1
    return (" 일치 위치: " + " | ".join(out)) if out else ""


def _data_error(path: str, text: str) -> str:
    """JSON·TOML·YAML 문법 오류(없으면 ""). Hermes 07 §7-1 의 쓰기 전 검사."""
    low = path.lower()
    try:
        if low.endswith(".json"):
            import json as _json
            _json.loads(text)
        elif low.endswith(".toml"):
            import tomllib
            tomllib.loads(text)
        elif low.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError:
                return ""
            for _ in yaml.compose_all(text, Loader=yaml.SafeLoader):   # 문법만(!Sub 같은 태그는 받아 준다)
                pass
        else:
            return ""
    except Exception as exc:  # noqa: BLE001 — 형식별 예외가 제각각
        return str(exc).splitlines()[0][:160]
    return ""


def data_break(path: str, before: str | None, after: str) -> str:
    """after 가 JSON·TOML·YAML 문법을 깨면 그 오류(원래 멀쩡했거나 새 파일일 때만). 쓰기 전에 거부하는 데 쓴다."""
    err = _data_error(path, after)
    if not err or (before is not None and _data_error(path, before)):
        return ""
    return err


def edit_diff(before: str, after: str, max_lines: int = 25, max_chars: int = 1200) -> str:
    """편집 결과를 짧은 unified diff 로(@@ 줄 포함, 앞뒤 1줄). 길면 자른다."""
    import difflib
    dl = list(difflib.unified_diff(before.replace("\r\n", "\n").splitlines(),
                                   after.replace("\r\n", "\n").splitlines(), n=1, lineterm=""))[2:]
    out, size = [], 0
    for k, ln in enumerate(dl):
        ln = ln if len(ln) <= 200 else ln[:200] + "…"
        if len(out) >= max_lines or size + len(ln) + 1 > max_chars:
            out.append(f"…[diff truncated: {len(dl) - k} more lines]")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def syntax_note(path: str, before: str | None, after: str) -> str:
    """편집이 '새로' 만든 문법 오류만 알린다(Hermes 07 §7-3 린트 비교의 축소판). 파이썬·JSON·TOML·YAML."""
    if not path.endswith(".py"):
        err = _data_error(path, after)
        if not err or (before is not None and _data_error(path, before)):
            return ""
        return f"\n경고: 이번 편집 뒤 {path} 의 문법이 깨졌다 — {err}. 바로 고쳐라."
    import ast as _ast
    try:
        _ast.parse(after)
        return ""
    except SyntaxError as exc:
        if before is not None:
            try:
                _ast.parse(before)
            except SyntaxError:
                return ""          # 원래도 깨져 있던 파일: 이번 편집 탓이 아님
        return (f"\n경고: 이번 편집 뒤 {path} 에 문법 오류가 생겼다 — {exc.lineno}행: {exc.msg}. "
                f"read_file 로 그 부분을 확인하고 바로 고쳐라.")


LINE_NO = re.compile(r"^\s*\d+\| ?")


def strip_line_numbers(s: str) -> str:
    """read_file 의 'N| ' 접두사를 모델이 그대로 복사해 왔으면 벗긴다(모든 줄이 그 모양일 때만)."""
    lines = s.splitlines()
    body = [ln for ln in lines if ln.strip()]
    if body and all(LINE_NO.match(ln) for ln in body):
        return "\n".join(LINE_NO.sub("", ln, count=1) for ln in lines) + ("\n" if s.endswith("\n") else "")
    return s


def strip_numbered_content(s: str) -> tuple[str, str]:
    """write_file 내용의 'N| ' 접두사: 모든 줄이 연속 번호면 벗긴 내용, 60% 이상이면 (원문, 거부 사유)."""
    body = [ln for ln in s.splitlines() if ln.strip()]
    nums = [int(m.group(0).strip().rstrip("|").strip()) for m in (LINE_NO.match(ln) for ln in body) if m]
    if not body or len(nums) < 0.6 * len(body):
        return s, ""
    if len(nums) == len(body) and all(b == a + 1 for a, b in zip(nums, nums[1:])):
        return strip_line_numbers(s), ""
    return s, (f"{len(nums)} of {len(body)} non-empty lines start with a line-number prefix like '12| ' "
               "copied from read_file output. The file was not changed. Send the content without those prefixes.")


def closest_lines_hint(text: str, old: str, top: int = 3) -> str:
    """'혹시 이 부분인가요?' — old 의 첫 의미 있는 줄과 비슷한 줄 3개를 앞뒤 1줄과 함께(Hermes 07 §4-7)."""
    import difflib
    anchor = next((ln for ln in old.splitlines() if ln.strip()), "")
    if not anchor:
        return ""
    lines = text.splitlines()
    scored = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        r = difflib.SequenceMatcher(None, anchor.strip(), ln.strip()).ratio()
        if r > 0.3:
            scored.append((r, i))
    if not scored:
        return ""
    out = ["\nClosest lines in the file (line| text):"]
    for _, i in sorted(scored, reverse=True)[:top]:
        for k in range(max(0, i - 1), min(len(lines), i + 2)):
            out.append(f"{k + 1:>5}| {lines[k]}")
        out.append("  ---")
    best = lines[sorted(scored, reverse=True)[0][1]]
    if best.strip() == anchor.strip() and best != anchor:      # 공백만 다르면 눈에 보이게
        vis = lambda x: x.replace("\t", "→").replace(" ", "·")  # noqa: E731
        out.append(f"Only whitespace differs. file has: {vis(best)}\n                 you sent: {vis(anchor)}")
    return "\n".join(out)


def similar_paths(ws: Path, path: str, limit: int = 5) -> list[str]:
    import difflib
    files = []
    for i, f in enumerate(iter_files(ws)):
        if i > 3000:
            break
        files.append(rel(ws, f))
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    by_name = [f for f in files if f.lower().rsplit("/", 1)[-1] == name]
    return (by_name + [f for f in difflib.get_close_matches(path.replace("\\", "/"), files, n=limit, cutoff=0.5)
                       if f not in by_name])[:limit]


def _restore_spacing(line: str, text: str) -> str:
    """new 줄의 따옴표 문자열이 파일에 이미 있는 문자열과 공백만 다르면 파일 쪽 표기로 되돌린다."""
    if "\"" not in line and "'" not in line:
        return line

    def segments(s: str) -> list[str]:
        # 따옴표 문자 사이의 조각 전부(삼중 따옴표·중첩 따옴표 안의 문자열도 잡힌다)
        out = []
        for ln in s.split("\n"):
            for qc in ("\"", "'"):
                parts = ln.split(qc)
                out += [p for p in parts[1:-1] if p.strip()]
        return out
    known = {}
    for q in segments(text):
        known.setdefault(_nospace(q), q)
    for q in segments(line):
        k = _nospace(q)
        if k in known and known[k] != q and len(k) >= 3:
            line = line.replace(q, known[k])
    return line


MAX_EDITS = 10


def apply_edit_args(text: str, args: dict) -> EditResult:
    """edit_file 인자 → 결과. `edits`(여러 곳, 위에서부터 차례로 적용, 하나라도 실패하면 아무것도 안 바뀜) 또는 old/new."""
    edits = args.get("edits")
    if not edits:
        return apply_edit(text, strip_line_numbers(args.get("old") or ""), strip_line_numbers(args.get("new") or ""))
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        return EditResult(False, f"edits must be a list of at most {MAX_EDITS} {{old, new}} items")
    cur, stages, changed = text, [], False
    for k, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return EditResult(False, f"edit #{k}: must be {{old, new}}")
        r = apply_edit(cur, strip_line_numbers(e.get("old") or ""), strip_line_numbers(e.get("new") or ""))
        if not r.ok:
            return EditResult(False, f"edit #{k} of {len(edits)} failed (nothing was changed): {r.message}")
        stages.append(r.stage)
        if r.new_text is not None:
            cur, changed = r.new_text, True
    if not changed:
        return EditResult(True, "이미 적용됨, 변경 없음", "already_applied")
    return EditResult(True, "", f"{len(edits)} edits: " + ",".join(stages), cur)


def apply_edit(text: str, old: str, new: str) -> EditResult:
    """정확 → 줄끝 공백 무시 → 들여쓰기 무시 → 줄 안 공백 무시. 못 찾았는데 new 가 이미 있으면 성공(already_applied)."""
    if old == "":
        return EditResult(False, "old 가 비었다. 새 파일이면 write_file 을 써라.")
    if old == new:
        return EditResult(False, "old 와 new 가 같다.")
    new = "\n".join(_restore_spacing(ln, text) for ln in new.split("\n"))
    # 1단계: 정확
    cnt = text.count(old)
    if cnt == 1:
        return EditResult(True, "ok", "exact", text.replace(old, new, 1))
    if cnt > 1:
        return EditResult(False, f"old 가 {cnt}곳과 일치한다. 앞뒤 줄을 더 넣어 고유하게 만들어라."
                          + _match_places(text, old))
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    old_lines = _strip_blank_edges(old.replace("\r\n", "\n").split("\n"))
    new_lines = new.replace("\r\n", "\n").split("\n")
    if old.replace("\r\n", "\n").strip("\n") and not new.strip():
        new_lines = []
    else:
        new_lines = _strip_blank_edges(new_lines) if new.strip() else new_lines
    new_lines = [_restore_spacing(ln, text) for ln in new_lines]
    for stage, norm in (("rstrip", lambda s: s.rstrip()), ("indent", lambda s: s.strip()),
                        ("unicode", _unicode), ("spaces", _nospace)):
        hits = _find_line_block(lines, old_lines, norm)
        if stage == "unicode" and hits and _find_line_block(lines, old_lines, _nospace):
            continue        # 공백만 다른 경우는 예전처럼 spaces 단계가 맡는다(이전에 맞던 편집의 결과·단계를 그대로)
        if len(hits) > 1:
            where = " 일치 위치: " + " | ".join(f"L{h + 1}: {lines[h].strip()[:60]}" for h in hits[:5])
            return EditResult(False, f"old 가 {len(hits)}곳과 일치한다({stage}). 고유한 줄을 더 넣어라." + where)
        if len(hits) == 1:
            i = hits[0]
            repl = list(new_lines)
            if stage == "spaces":
                # 9B 가 줄 안 띄어쓰기를 바꿔 복사한 경우: old 와 공백만 다른 new 줄은 파일 원래 줄을 그대로 쓴다
                orig = {_nospace(old_lines[k]): lines[i + k] for k in range(len(old_lines))}
                repl = [orig.get(_nospace(ln), ln) if ln.strip() else ln for ln in repl]
            if stage in ("indent", "unicode", "spaces") and old_lines:
                # 들여쓰기 재정렬: 파일 쪽 첫 줄 들여쓰기 - old 첫 줄 들여쓰기 만큼 new 를 옮긴다
                file_ind = _indent_of(lines[i])
                old_ind = _indent_of(old_lines[0])
                fixed = []
                for ln in repl:
                    if not ln.strip():
                        fixed.append(ln)
                    elif ln.startswith(old_ind):
                        fixed.append(file_ind + ln[len(old_ind):])
                    else:
                        fixed.append(file_ind + ln.lstrip())
                repl = fixed
            if stage == "unicode":
                # 둥근 따옴표·대시를 ASCII 로 옮겨 적은 경우: old 와 같아지는 new 줄은 파일 원래 줄을 그대로 쓴다
                orig = {_unicode(old_lines[k]): lines[i + k] for k in range(len(old_lines))}
                repl = [orig.get(_unicode(ln), ln) if ln.strip() else ln for ln in repl]
            out = lines[:i] + repl + lines[i + len(old_lines):]
            return EditResult(True, "ok", stage, nl.join(out))
    # 인자가 두 번 이스케이프됐나? 진짜 줄바꿈 없이 글자 '\n' 만 있고, 그걸 줄바꿈으로 바꾸면 파일에 있다(고치지는 않는다)
    if "\n" not in old and "\\n" in old and old.replace("\\n", "\n") in text.replace("\r\n", "\n"):
        return EditResult(False, "old has no real line breaks but contains the two characters \\n, and with real "
                          "line breaks it is in the file: the arguments were escaped twice. Send real line breaks "
                          "in old and new (not \\\\n). Nothing was changed.")
    # 이미 적용됐나? 짧은 new 는 파일 어딘가에 우연히 있을 수 있어 8자 이상일 때만(Hermes 07 §4-7)
    if len(new.strip()) >= 8:
        if new in text or _find_line_block(lines, _strip_blank_edges(new.replace("\r\n", "\n").split("\n")),
                                           lambda s: s.strip()):
            return EditResult(True, "이미 적용된 편집(변경 없음)", "already_applied", None)
    # 힌트: old 첫 줄과 비슷한 줄
    hint = ""
    first = old_lines[0].strip() if old_lines else ""
    if first:
        cands = [f"{k + 1}: {ln}" for k, ln in enumerate(lines) if first[:20] and first[:20] in ln][:3]
        if cands:
            hint = " 비슷한 줄: " + " | ".join(cands)
    return EditResult(False, "old 를 파일에서 찾지 못했다. 파일을 다시 읽고 정확히 복사해라." + hint)


# ---------------------------------------------------------------- 실행기

def find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(p).exists():
            return p
    return None


def _python_dir(d: Path) -> Path | None:
    """d 또는 d/Scripts(Windows)·d/bin 중 파이썬 실행 파일이 있는 폴더."""
    names = ("python.exe",) if os.name == "nt" else ("python", "python3")
    for x in (d, d / ("Scripts" if os.name == "nt" else "bin")):
        if any((x / n).is_file() for n in names):
            return x
    return None


def project_python_dir(ws: Path | None) -> Path | None:
    """명령이 쓸 프로젝트 파이썬 폴더: .cap/settings.json "python"(실행 파일이나 그 폴더) > ws/.venv·ws/venv > VIRTUAL_ENV.
    없으면 None — 그때는 CAP 자신의 파이썬이 잡힌다(uv tool 설치면 프로젝트 의존성이 없다)."""
    if ws is None:
        return None
    try:
        v = json.loads((ws / ".cap" / "settings.json").read_text(encoding="utf-8")).get("python")
    except (OSError, ValueError, AttributeError):
        v = None
    cands = []
    if isinstance(v, str) and v.strip():
        p = Path(v.strip()).expanduser()
        cands.append(p if p.is_absolute() else ws / p)
    cands += [ws / ".venv", ws / "venv"] + ([Path(os.environ["VIRTUAL_ENV"])] if os.environ.get("VIRTUAL_ENV") else [])
    for p in cands:
        d = p.parent if p.is_file() else _python_dir(p) if p.is_dir() else None
        if d:
            return d.absolute()
    return None


def _path_dirs(ws: Path | None) -> tuple[str, str]:
    """PATH 맨 앞·맨 뒤에 붙일 폴더. 프로젝트 파이썬이 있으면 그것이 앞, CAP 자신의 폴더는 뒤(pytest 등이 없을 때 대비)."""
    own = str(Path(sys.executable).parent)
    proj = project_python_dir(ws)
    if proj and os.path.normcase(str(proj)) != os.path.normcase(own):
        return str(proj), own
    return own, ""


def python_note(ws: Path) -> str:
    """/doctor 한 줄: 명령이 쓰는 파이썬."""
    proj = project_python_dir(ws)
    if proj:
        return f"commands use the project's python in {proj}"
    return (f"commands use CAP's own python in {Path(sys.executable).parent} (no .venv/venv, VIRTUAL_ENV "
            'or "python" in .cap/settings.json)')


QUIET_ENV = {"NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat", "COLORTERM": ""}


def shell_env(ws: Path | None = None) -> dict:
    env = dict(os.environ)
    first, last = _path_dirs(ws)
    env["PATH"] = first + os.pathsep + env.get("PATH", "") + (os.pathsep + last if last else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(QUIET_ENV)       # 색·페이저·대화형 출력 끄기(Codex 처럼): 출력이 모델에게 그대로 가므로
    for k in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        env.pop(k, None)
    for k in list(env):     # 자식 프로세스에 키·토큰을 넘기지 않는다(이름으로 거름, Hermes 06 §9)
        if k in ("KAU_API_KEY", "CAP_API_KEY") or SECRET_ENV.search(k):
            env.pop(k, None)
    return env


SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|SESSION|AUTH", re.I)


def secret_values(ws: Path) -> list[str]:
    """가릴 값: 환경변수 중 비밀 이름의 값 + 작업 폴더 .env 류 파일의 값(8자 이상만)."""
    vals = [v for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v) >= 8]
    try:
        for f in list(ws.glob(".env*"))[:10]:
            if f.is_file() and is_secret_path(f.name):
                for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    k, sep, v = ln.partition("=")
                    v = v.strip().strip("'\"")
                    if sep and len(v) >= 8:
                        vals.append(v)
    except OSError:
        pass
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str, values: list[str]) -> str:
    for v in values:
        if v in text:
            text = text.replace(v, "[REDACTED]")
    return text


def _decode(b: bytes | None) -> str:
    """명령 출력: 줄마다 UTF-8, 아니면 Windows 코드 페이지(격리 안 cmd.exe 의 메시지는 콘솔이 없어 cp949 로 나온다)."""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out = []
    for ln in b.split(b"\n"):
        try:
            out.append(ln.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(ln.decode("mbcs" if os.name == "nt" else "latin-1", "replace"))
    return "\n".join(out)


def _bash_path(d: str) -> str:
    d = Path(d).as_posix()
    return "/" + d[0].lower() + d[2:] if re.match(r"^[A-Za-z]:/", d) else d     # Git Bash: C:/x → /c/x


def shell_args(cmd: str, cwd: Path, sandbox: str = "", timeout: int | None = None,
               pyws: Path | None = None) -> tuple[Any, bool, str]:
    """run_shell·bg_start 가 같이 쓰는 실행 인자 (args, shell, 오류). 오류가 있으면 격리를 띄우지 못한 것.
    프로젝트 파이썬 폴더(pyws, 기본 cwd)가 PATH 맨 앞, CAP 자신의 폴더는 맨 뒤(프로젝트 파이썬이 없으면 맨 앞)."""
    bash = find_bash()
    first, last = _path_dirs(pyws or cwd)
    if sandbox:
        from . import sandbox as sb
        inner = cmd
        if sandbox == "appcontainer":          # Windows 격리: cmd.exe, venv 파이썬이 먼저 잡히게
            from .winutils import translate    # mkdir -p·dir·export·/dev/null 같은 bash 습관을 cmd 로(유닉스 명령은 .cap/sbx/bin)
            # chcp 결과는 NUL 대신 격리 임시 폴더의 파일로(NUL 을 못 여는 컴퓨터가 있다), 실패해도 명령은 돈다
            inner = f'chcp 65001 >"%TEMP%\\cap-devnull" 2>&1 & set "PATH={first};%PATH%{";" + last if last else ""}" && {translate(cmd)}'
        elif sandbox != "wsl" and bash:          # 리눅스: venv 파이썬이 먼저 잡히게(WSL 은 리눅스 쪽 도구를 쓴다)
            inner = f'export PATH="{_bash_path(first)}:$PATH{":" + _bash_path(last) if last else ""}"; {cmd}'
        args, err = sb.wrap(inner, cwd, sandbox, timeout)
        if args is None:
            return None, False, err
        return args, False, ""
    if bash:                                     # Git Bash 에서 venv 파이썬이 먼저 잡히도록
        full = f'export PATH="{_bash_path(first)}:$PATH{":" + _bash_path(last) if last else ""}"; {cmd}'
        return [bash, "-c", full], False, ""
    return cmd, True, ""


def sandbox_note(code: int, out: str, sandbox: str) -> tuple[int, str]:
    """격리 안에서 돈 명령의 출력에 붙이는 설명(격리가 막았거나 못 띄웠을 때)."""
    if sandbox:
        from . import sandbox as sb
        if code == 97 and "landlock_exec:" in out:
            return 1, out + "\n[sandbox] the sandbox could not start, so the command was not run."
        out += sb.note_if_blocked(code, out)
    return code, out


def run_shell(cmd: str, cwd: Path, timeout: int = 180, sandbox: str = "",
              pyws: Path | None = None) -> tuple[int, str]:
    """sandbox: "" 이면 그대로, 아니면 그 격리 방식(sandbox.py)으로 감싸서 돌린다.
    pyws: 어느 작업 폴더의 파이썬을 쓸지(기본 cwd — 임시 폴더에서 돌릴 때 원래 작업 폴더를 넘긴다)."""
    args, shell, err = shell_args(cmd, cwd, sandbox, timeout, pyws)
    if err:
        return 1, f"[sandbox] could not start the sandbox: {err}. The command was not run."
    env = shell_env(pyws or cwd)
    # 새 프로세스 그룹으로 띄우고, 시간이 넘으면 자식·손자까지 통째로 죽인다
    # (bash 만 죽이면 그 아래 python 이 파이프를 쥐고 있어 communicate 가 끝나지 않는다 — 무한 루프 코드에서 멈춤)
    kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(args, cwd=cwd, env=env, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, **kw)
    try:
        so, se = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            so, se = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            so, se = b"", b""
        out = (so or b"").decode("utf-8", "replace") + (se or b"").decode("utf-8", "replace")
        return 124, clip(out, 3000) + f"\n[timed out after {timeout}s — process tree killed]"
    return sandbox_note(p.returncode, _decode(so) + _decode(se), sandbox)


YIELD_S = 30      # run 이 이만큼 기다려도 안 끝나면 백그라운드로 넘긴다(서버를 background 없이 띄워도 멈추지 않게)


def run_yield(cmd: str, cwd: Path, sandbox: str = "", limit: float = YIELD_S) -> tuple[int | None, str, str]:
    """백그라운드 명령처럼 띄워 limit 초까지 기다린다(Codex 의 yield). 끝나면 (exit, 출력, "") 이고 백그라운드 표에서 지운다.
    아직 돌면 (None, 지금까지의 출력, "bgK") — 그 뒤로는 process 도구로 본다. 격리를 못 띄우면 RuntimeError."""
    from . import extras as ext
    pid = ext.bg_start(cwd, cmd, sandbox=sandbox, stdin=False)
    v = ext._find(cwd, pid)
    try:
        v["proc"].wait(timeout=limit)
    except subprocess.TimeoutExpired:
        data = v["log"].read_bytes()
        cut = ext.utf8_cut(data)
        v["model_pos"] = cut                    # 여기서 보여 준 데까지는 process(output) 가 다시 보내지 않는다
        return None, _decode(data[:cut]), pid
    code = v["proc"].returncode
    return (*sandbox_note(code, _decode(ext.bg_take(cwd, pid)), sandbox), "")


def _kill_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()


@dataclass
class ToolBox:
    ws: Path
    test_cmd: str = "python -m pytest -q"
    full_reads: set[str] = field(default_factory=set)
    todos: list[dict] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)   # CAP 이 마지막으로 읽거나 쓴 내용의 해시
    edit_fails: dict[str, int] = field(default_factory=dict)    # 파일별 연속 편집 실패 수
    # 나눠 읽은 줄 범위 {path: {"hash", "ranges": [[a, b], ...]}} — 같은 내용에서 1~끝을 다 덮으면 통째로 읽은 셈
    read_ranges: dict[str, dict] = field(default_factory=dict)
    sandbox: str = ""            # 명령 격리 방식(sandbox.py). "" = 격리 없음
    unsandboxed_once: bool = False   # 이번 호출만 격리 밖(사람이 허락한 설치·네트워크 명령, escalate)

    def p(self, path: str) -> Path:
        q = Path(path)
        return q if q.is_absolute() else (self.ws / q)

    def execute(self, name: str, args: dict) -> dict[str, Any]:
        """반환: {ok, output, changed:[paths], extra...}"""
        try:
            fn = getattr(self, "t_" + name)
        except AttributeError:
            return {"ok": False, "output": f"모르는 도구: {name}", "changed": []}
        try:
            res = fn(**args)
            if isinstance(res.get("output"), str):      # 어떤 경로로 읽었든 비밀 값은 모델·장부에 넘기지 않는다
                res["output"] = redact(res["output"], secret_values(self.ws))
            return res
        except TypeError as exc:
            return {"ok": False, "output": f"인자 오류: {exc}", "changed": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "output": f"{type(exc).__name__}: {exc}", "changed": []}

    # -- 도구들
    def t_read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> dict:
        fp = self.p(path)
        if not fp.is_file():
            if fp.is_dir():
                items = sorted(x.name + ("/" if x.is_dir() else "") for x in fp.iterdir()
                               if x.name not in IGNORE_DIRS)
                return {"ok": True, "output": "(폴더) " + "  ".join(items[:200]), "changed": []}
            near = similar_paths(self.ws, path)
            hint = (" Did you mean: " + ", ".join(near) + "?") if near else " Use search or read_file on a folder to find it."
            return {"ok": False, "output": f"파일 없음: {path}.{hint}", "changed": []}
        from .llm import IMAGE_EXT, IMAGE_MAX_BYTES
        if fp.suffix.lower() in IMAGE_EXT:     # 그림: 글자로 읽지 않고 모델에게 그림으로 보여 준다(Claude Code Read 와 같이)
            size = fp.stat().st_size
            if size > IMAGE_MAX_BYTES:
                return {"ok": False, "output": f"image {rel(self.ws, fp)} is {size:,} bytes — larger than "
                                               f"{IMAGE_MAX_BYTES // 1_000_000} MB, not shown", "changed": []}
            return {"ok": True, "output": f"[image {rel(self.ws, fp)}, {size:,} bytes] It is attached right after "
                                          "these tool results so you can look at it.", "changed": [],
                    "image": str(fp.resolve())}
        text = fp.read_text(encoding="utf-8", errors="replace")
        notebook = fp.suffix.lower() == ".ipynb"
        if notebook:                       # 노트북: JSON 대신 셀(코드·마크다운·출력)로 보여 준다(Claude Code 와 같이)
            text = render_notebook(text)
        lines = text.splitlines()
        n = len(lines)
        start = max(1, int(offset or 1))
        cnt = int(limit) if limit else n
        end = min(n, start + cnt - 1)
        body = "\n".join(f"{k}| {ln}" for k, ln in enumerate(lines[start - 1:end], start))
        if start == 1 and end >= n and len(body) <= MAX_OUT and not notebook:   # 노트북은 원문(JSON)을 본 게 아니다
            self.full_reads.add(rel(self.ws, fp))
            self.file_hashes[rel(self.ws, fp)] = file_hash(fp)
        if len(body) > MAX_OUT:
            # 줄 단위로 자르고 이어 읽기 안내
            acc, k = [], start
            for ln in lines[start - 1:end]:
                ln = f"{k}| {ln}"
                if sum(len(a) + 1 for a in acc) + len(ln) > MAX_OUT:
                    break
                acc.append(ln)
                k += 1
            body = "\n".join(acc) + f"\n…[잘림: {k}번째 줄부터 offset={k} 로 이어 읽어라]"
            end = k - 1
        if not notebook and start <= end:
            self._note_range(rel(self.ws, fp), file_hash(fp), start, end, n)
        return {"ok": True, "output": f"[{rel(self.ws, fp)} 줄 {start}-{end} / 전체 {n}]\n{body}", "changed": []}

    def _note_range(self, r: str, h: str, a: int, b: int, n: int) -> None:
        """실제로 보여 준 줄 범위를 합친다(내용이 바뀌었으면 새로 시작). 1~n 을 다 덮으면 통째로 읽은 것으로."""
        cur = self.read_ranges.get(r) or {}
        old = (cur.get("ranges") or []) if cur.get("hash") == h else []
        merged: list[list[int]] = []
        for x, y in sorted([list(p) for p in old] + [[a, b]]):
            if merged and x <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], y)
            else:
                merged.append([x, y])
        self.read_ranges[r] = {"hash": h, "ranges": merged}
        if merged[0][0] <= 1 and merged[0][1] >= n:
            self.full_reads.add(r)
            self.file_hashes[r] = h

    def t_search(self, pattern: str, path: str = ".") -> dict:
        base = self.p(path or ".")
        rg = shutil.which("rg")
        out_lines: list[str] = []
        if rg:
            r = subprocess.run([rg, "-n", "--no-heading", "-S", "-g", "!.cap", "-g", "!.venv", "-g", "!.env",
                                "-g", "!.env.*", "-g", "!*.pem", pattern, str(base)],
                               cwd=self.ws, capture_output=True, text=True, encoding="utf-8", errors="replace")
            out_lines = [ln.replace(str(self.ws) + os.sep, "") for ln in r.stdout.splitlines()]
            out_lines = [ln for ln in out_lines if not is_secret_path(ln.split(":", 1)[0])]
        else:
            try:
                rx = re.compile(pattern)
            except re.error:
                rx = re.compile(re.escape(pattern))
            files = [base] if base.is_file() else list(iter_files(base))
            for f in files:
                if f.suffix in (".pyc", ".png", ".jpg", ".sqlite", ".zip", ".exe", ".dll") or is_secret_path(f.name):
                    continue
                try:
                    for k, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(ln):
                            out_lines.append(f"{rel(self.ws, f)}:{k}: {ln.strip()[:200]}")
                except OSError:
                    continue
                if len(out_lines) > 200:
                    break
        if not out_lines:
            narrow = path not in ("", ".", "./") and self.p(path).is_file()
            return {"ok": True, "output": "(일치 없음) — try a shorter or simpler pattern (regex; lowercase = case-insensitive). "
                                          "Hidden folders, .venv and secret files are skipped."
                                          + (f" You searched only {path}; it may be defined elsewhere — search the whole "
                                             "project (path \".\")." if narrow else ""), "changed": []}
        return {"ok": True, "output": clip("\n".join(out_lines[:200])), "changed": []}

    def t_edit_file(self, path: str, old: str = "", new: str = "", edits: list | None = None) -> dict:
        fp = self.p(path)
        if not fp.is_file():
            return {"ok": False, "output": f"파일 없음: {path}. 새 파일은 write_file.", "changed": []}
        raw = fp.read_bytes().decode("utf-8", errors="replace")
        r = rel(self.ws, fp)
        old, new = strip_line_numbers(old), strip_line_numbers(new)
        res = apply_edit_args(raw, {"old": old, "new": new, "edits": edits})
        if edits:
            old = strip_line_numbers(next((e.get("old", "") for e in edits if isinstance(e, dict)), ""))
        if not res.ok:
            n = self.edit_fails.get(r, 0) + 1
            self.edit_fails[r] = n
            msg = res.message
            if "일치한다" not in msg:            # 여러 곳 일치 오류에는 힌트를 붙이지 않는다(오해를 부름)
                msg += closest_lines_hint(raw, old)
            if n >= 3:                          # Hermes 07 §4-8: 같은 파일 3번째 실패부터 강하게
                msg += (f"\nThis is edit failure #{n} on {r}. Stop retrying variations of the same old text: "
                        "(1) read_file it again, (2) copy a longer unique old text exactly, or "
                        "(3) read the whole file and replace it with write_file.")
            return {"ok": False, "output": msg, "changed": [], "stage": "fail"}
        self.edit_fails.pop(r, None)
        if res.new_text is None:
            return {"ok": True, "output": res.message, "changed": [], "stage": res.stage}
        bad = data_break(r, raw, res.new_text)
        if bad:                                 # 멀쩡하던 JSON·TOML·YAML 을 깨는 편집은 쓰기 전에 거부
            return {"ok": False, "output": f"Not applied: after this edit {r} would no longer parse ({bad}). "
                                           "The file was not changed. Fix the edit and try again.",
                    "changed": [], "stage": "refused"}
        warn = ""
        if r in self.file_hashes and self.file_hashes[r] != file_hash(fp):
            warn = f"\nNote: {r} changed on disk since you last read it; your edit matched the current text."
        fp.write_bytes(res.new_text.encode("utf-8"))
        self.file_hashes[r] = file_hash(fp)
        diff = "" if "no_diff" in os.getenv("CAP_ABLATION", "") else edit_diff(raw, res.new_text)
        if diff:
            diff = f"\n{diff}\nThe file now contains this change; no need to re-read it."
        note = syntax_note(r, raw, res.new_text)
        if not note:
            from .extras import lint_delta
            note = lint_delta(r, raw, res.new_text)
        return {"ok": True, "output": f"편집 적용({res.stage})" + warn + diff + note, "changed": [r],
                "stage": res.stage, "syntax_error": bool(note)}

    def check_edit(self, path: str, old: str, new: str, edits: list | None = None) -> str:
        """승인을 묻기 전에 해 보는 편집(적용하지 않음). 적용될 수 없으면 모델에게 돌려줄 문장."""
        fp = self.p(path)
        if not fp.is_file():
            return ""
        raw = fp.read_bytes().decode("utf-8", errors="replace")
        res = apply_edit_args(raw, {"old": old, "new": new, "edits": edits})
        if res.ok:
            bad = data_break(rel(self.ws, fp), raw, res.new_text) if res.new_text is not None else ""
            return f"after this edit {rel(self.ws, fp)} would no longer parse ({bad})." if bad else ""
        if edits:
            return res.message
        return res.message + ("" if "일치한다" in res.message else closest_lines_hint(raw, strip_line_numbers(old)))

    def check_write(self, path: str, content: str | None = None) -> str:
        """write_file 이 거부될 이유(승인 전에 확인). 없으면 "". content 를 주면 내용도 본다(줄 번호 접두사·데이터 문법)."""
        stale = self._stale_write(path)
        return stale or (self._bad_content(path, content) if content is not None else "")

    def _stale_write(self, path: str) -> str:
        fp = self.p(path)
        r = rel(self.ws, fp)
        if not fp.exists():
            return ""
        if r not in self.full_reads:
            return (f"{r} already exists. Overwrite only a file you have read in full with read_file; "
                    "for small changes use edit_file.")
        if r in self.file_hashes and self.file_hashes[r] != file_hash(fp):
            return (f"{r} changed on disk since you read it (maybe the user edited it). The file was not changed. "
                    "read_file it again, merge, then write_file again — or use edit_file.")
        return ""

    def _bad_content(self, path: str, content: str) -> str:
        content, why = strip_numbered_content(content)
        if why:
            return why
        fp = self.p(path)
        before = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else None
        bad = data_break(rel(self.ws, fp), before, content)
        if bad:
            return (f"the new content of {rel(self.ws, fp)} does not parse ({bad}). The file was not changed. "
                    "Fix the syntax and write it again.")
        return ""

    def t_write_file(self, path: str, content: str) -> dict:
        fp = self.p(path)
        r = rel(self.ws, fp)
        refuse = self._stale_write(path)       # Hermes 07 §5-7: 읽지 않았거나 읽은 뒤 바뀐 파일은 덮어쓰지 않는다
        if refuse:
            return {"ok": False, "output": refuse, "changed": [], "stale_write_blocked": True}
        stripped, _ = strip_numbered_content(content)
        refuse = self._bad_content(path, stripped)   # 줄 번호가 섞였거나 멀쩡하던 데이터 파일을 깨면 쓰지 않는다
        if refuse:
            return {"ok": False, "output": refuse, "changed": [], "write_refused": True}
        tag = " (removed the line-number prefixes copied from read_file)" if stripped != content else ""
        content = stripped
        before = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else None
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content.encode("utf-8"))
        self.full_reads.add(r)
        self.file_hashes[r] = file_hash(fp)
        self.read_ranges.pop(r, None)
        note = syntax_note(r, before, content)
        syn = bool(note)
        if not note:
            from .extras import lint_delta
            note = lint_delta(r, before, content)
        return {"ok": True, "output": f"{r} 저장({len(content)}자)" + tag + note, "changed": [r], "syntax_error": syn}

    def t_apply_patch(self, patch: str) -> dict:
        """Codex apply_patch: 여러 파일을 한 번에. 모든 청크가 맞아야 쓴다(하나라도 실패하면 아무것도 안 바뀜)."""
        from . import patch as P
        try:
            ops = P.parse(patch)
        except P.PatchError as exc:
            return {"ok": False, "output": f"patch not applied: {exc}", "changed": []}
        plan: dict[str, str | None] = {}            # 작업 폴더 기준 경로 → 새 내용(None = 지움)
        for op in ops:
            fp = self.p(op.path)
            r = rel(self.ws, fp)
            exists = (plan.get(r) is not None) if r in plan else fp.is_file()
            if op.kind == "add":
                if exists:
                    return {"ok": False, "output": f"patch not applied: {r} already exists (use Update File)",
                            "changed": []}
                plan[r] = "\n".join(op.content) + ("\n" if op.content else "")
                continue
            if not exists:
                return {"ok": False, "output": f"patch not applied: {r} does not exist", "changed": []}
            if op.kind == "delete":
                plan[r] = None
                continue
            text = plan[r] if r in plan else fp.read_text(encoding="utf-8", errors="replace")
            try:
                new = P.apply_hunks(text or "", op.hunks)
            except P.PatchError as exc:
                return {"ok": False, "output": f"patch not applied ({r}): {exc}. Read the file again and retry.",
                        "changed": []}
            if op.move_to:
                plan[r] = None
                plan[rel(self.ws, self.p(op.move_to))] = new
            else:
                plan[r] = new
        changed, notes = [], []
        for r, content in plan.items():
            fp = self.ws / r
            if content is None:
                fp.unlink(missing_ok=True)
                self.file_hashes.pop(r, None)
                self.full_reads.discard(r)
                self.read_ranges.pop(r, None)
                notes.append(f"deleted {r}")
            else:
                before = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else None
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(content.encode("utf-8"))
                self.file_hashes[r] = file_hash(fp)
                self.full_reads.add(r)
                self.read_ranges.pop(r, None)
                notes.append(("updated " if before is not None else "added ") + r + syntax_note(r, before, content))
            changed.append(r)
        return {"ok": True, "output": "patch applied: " + "; ".join(notes), "changed": changed}

    def t_glob(self, pattern: str, path: str = "") -> dict:
        """파일 이름 패턴으로 찾기(Claude Code Glob). 최근에 바뀐 파일 먼저, 200개까지."""
        if ".." in Path(pattern).parts or Path(pattern).is_absolute() or ".." in Path(path or ".").parts:
            return {"ok": False, "output": "pattern must stay inside the workspace (no '..' or absolute paths)", "changed": []}
        root = self.p(path) if path else self.ws
        skip = {".git", ".cap", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
        if not root.is_dir():
            return {"ok": False, "output": f"no such folder: {path}", "changed": []}

        def show(q: Path) -> str:      # 작업 폴더 안은 상대 경로, 밖(사람이 허락한 경우)은 절대 경로
            try:
                return q.relative_to(self.ws).as_posix()
            except ValueError:
                return q.as_posix()
        hits = []
        for q in root.glob(pattern):
            if q.is_file() and not (skip & set(q.relative_to(root).parts)) and not is_secret_path(show(q)):
                hits.append(q)
            if len(hits) >= 2000:
                break
        hits.sort(key=lambda q: q.stat().st_mtime, reverse=True)
        lines = [show(q) for q in hits[:200]]
        more = f"\n…(+{len(hits) - 200} more — narrow the pattern)" if len(hits) > 200 else ""
        return {"ok": True, "output": "\n".join(lines) + more if lines else "(no files match)", "changed": []}

    def t_symbols(self, action: str = "map", name: str = "", path: str = "") -> dict:
        from .codemap import code_map, find_symbol
        if action == "find":
            return {"ok": True, "output": find_symbol(self.ws, name), "changed": []}
        return {"ok": True, "output": code_map(self.ws, path), "changed": []}

    def _sandbox_for_this_call(self, escalate: bool = False) -> str:
        sb, self.unsandboxed_once = ("" if self.unsandboxed_once else self.sandbox), False
        return "" if escalate else sb

    def t_run(self, cmd: str, background: bool = False, escalate: bool = False, timeout: float | None = None) -> dict:
        sb = self._sandbox_for_this_call(escalate)
        if background:
            from .extras import bg_start
            pid = bg_start(self.ws, cmd, sandbox=sb)
            return {"ok": True, "output": f"[started in background as {pid}] {cmd}\nCheck it with "
                                          f"process(action=\"output\", id=\"{pid}\"); stop it with action=\"kill\".",
                    "changed": [], "exit": 0}
        try:
            limit = max(1.0, min(float(timeout), 600.0)) if timeout else float(YIELD_S)
        except (TypeError, ValueError):
            limit = float(YIELD_S)
        before = self._mtimes()
        try:
            code, out, pid = run_yield(cmd, self.ws, sandbox=sb, limit=limit)
        except RuntimeError as exc:
            code, out, pid = 1, f"[sandbox] {exc}. The command was not run.", ""
        after = self._mtimes()
        changed = sorted(k for k in after if before.get(k) != after[k]) + sorted(k for k in before if k not in after)
        for k in changed:                      # 명령이 바꾼 파일은 다시 읽어야 통째로 덮어쓸 수 있다
            self.full_reads.discard(k)
            self.file_hashes.pop(k, None)
            self.read_ranges.pop(k, None)
        if code is None:                       # 아직 도는 중: 백그라운드로 넘어갔다(서버·긴 작업)
            head = (f"[still running after {limit:.0f}s as {pid} — partial output below — check it with "
                    f"process(action=\"output\", id=\"{pid}\", wait=30) or stop it with action=\"kill\"]")
            return {"ok": True, "output": spill(self.ws, f"{head}\n{out or '(no output yet)'}"), "changed": changed,
                    "exit": None, "background": pid}
        if re.search(r"\b(pytest|unittest|npm (run )?test|cargo test|go test|make test)\b", cmd):
            out = with_failures(out, code)      # 모델이 run 으로 돌린 테스트도 실패 요약을 앞에
        return {"ok": code == 0, "output": spill(self.ws, f"[exit {code}]\n{out}"), "changed": changed, "exit": code}

    def t_run_tests(self, target: str = "") -> dict:
        from .policy import _bad_test_target    # 권한 판정과 같은 검사를 실행기에서도 한 번 더(셸에 그대로 넘어가므로)
        bad = _bad_test_target(target or "", self.ws)
        if bad:
            return {"ok": False, "output": bad, "changed": [], "exit": 2, "cmd": self.test_cmd, "test": True}
        cmd = self.test_cmd + (f" {target}" if target else "")
        code, out = run_shell(cmd, self.ws, sandbox=self._sandbox_for_this_call())
        return {"ok": code == 0, "output": spill(self.ws, f"[{cmd} → exit {code}]\n{with_failures(out, code)}"),
                "changed": [], "exit": code, "cmd": cmd, "test": True}

    def t_todo(self, items: list) -> dict:
        self.todos = [{"text": str(i.get("text", "")), "done": bool(i.get("done", False))}
                      for i in items if isinstance(i, dict)]
        txt = "\n".join(f"[{'x' if t['done'] else ' '}] {t['text']}" for t in self.todos)
        return {"ok": True, "output": txt or "(비어 있음)", "changed": [], "todos": self.todos}

    def t_report_conflict(self, reason: str) -> dict:
        return {"ok": True, "output": "충돌 신고 접수. 사람에게 넘긴다.", "changed": [], "conflict": reason}

    def _mtimes(self) -> dict[str, float]:
        out = {}
        for f in iter_files(self.ws):
            try:
                out[rel(self.ws, f)] = f.stat().st_mtime_ns
            except OSError:
                pass
        return out
