"""평가기 먼저 검증 (eval_check). LLM 0회.

1. 기준선: 아무것도 안 바꾼 코드로 평가 명령 실행 (정보 + 테스트 0개면 중단)
2. 치트 구현 시험 4종: 상수 반환 · 항상 예외 · 입력 그대로 반환 · 테스트 assert 값을 외운 표
   → 하나라도 평가 명령을 통과하면 "해킹 가능한 평가기" → 루프 시작 안 함
3. 돌연변이 점수(기준선이 통과할 때, 또는 루프가 초록불을 낸 뒤): 규칙 기반 AST 돌연변이
   (비교 연산자 뒤집기 · 상수 바꾸기 · 산술 연산자 바꾸기 · return 값 제거 · if 조건 부정)
   → 잡아낸 비율 < 기준이면 중단(살아남은 돌연변이 목록 보고)

모든 시험은 작업 폴더의 임시 복사본에서 돈다(원본은 건드리지 않음).
"""

from __future__ import annotations

import ast
import copy
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import IGNORE_DIRS, iter_files, rel, sha256_file
from .tools import run_shell

CHEATS = ("const", "raise", "identity", "lookup")
MUTATION_THRESHOLD = 0.6
MAX_MUTANTS = 12


def is_test_file(r: str) -> bool:
    name = Path(r).name
    parts = Path(r).parts
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py") or \
        name == "conftest.py" or (len(parts) > 1 and parts[0] in ("tests", "test") and name.endswith(".py"))


def collect_test_files(ws: Path) -> list[str]:
    return sorted(r for r in (rel(ws, f) for f in iter_files(ws)) if is_test_file(r))


def frozen_hashes(ws: Path, files: list[str]) -> dict[str, str]:
    return {f: sha256_file(ws / f) for f in files if (ws / f).is_file()}


def find_targets(ws: Path, test_files: list[str]) -> list[str]:
    """테스트가 import 하는 로컬 모듈(.py)."""
    names: list[str] = []
    for tf in test_files:
        try:
            tree = ast.parse((ws / tf).read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.append(node.module)
    out: list[str] = []
    for n in names:
        parts = n.split(".")
        for base in (ws, ws / "src"):
            for cand in (base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts) / "__init__.py"):
                if cand.is_file():
                    r = rel(ws, cand)
                    if r not in out and not is_test_file(r):
                        out.append(r)
    return out


# ---------------------------------------------------------------- 요구사항 ↔ 평가기 연결 (평가기 공백)

import builtins as _bi
import keyword as _kw
import re as _re

_SYM_RX = _re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`|\b([A-Za-z_][\w.]*)\s*\(|\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b|"
                      r"\b([A-Za-z_]\w*)\s*(?:함수|메서드|function|method)")
_SYM_STOP = set(dir(_bi)) | set(_kw.kwlist) | {"self", "cls", "pytest", "python", "test", "tests", "main"}


def requirement_symbols(text: str) -> list[str]:
    """요구사항 문장에서 코드 이름(함수·메서드)을 뽑는다. 파일 이름(x.py)·내장 이름은 뺀다."""
    out: list[str] = []
    for m in _SYM_RX.finditer(text or ""):
        sym = next(g for g in m.groups() if g)
        after = text[m.end(): m.end() + 3]
        if after.startswith(".py") or sym.endswith((".py", ".md", ".txt")):
            continue
        sym = sym.rstrip(".").split(".")[-1]
        if len(sym) < 3 or sym in _SYM_STOP or sym in out or sym.startswith("test_"):
            continue
        out.append(sym)
    return out


def untested_symbols(ws: Path, syms: list[str]) -> list[str]:
    """테스트가 직접 부르지도 않고, 테스트가 부르는 함수가 한 단계 안에서 부르지도 않는 이름."""
    if not syms:
        return []
    tfiles = collect_test_files(ws)
    ttext = "\n".join((ws / f).read_text(encoding="utf-8", errors="replace") for f in tfiles if (ws / f).is_file())
    direct = {s for s in syms if _re.search(rf"\b{_re.escape(s)}\b", ttext)}
    rest = [s for s in syms if s not in direct]
    if not rest:
        return []
    indirect: set[str] = set()
    for f in iter_files(ws):
        r = rel(ws, f)
        if f.suffix != ".py" or is_test_file(r):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _re.search(rf"\b{fn.name}\b", ttext):
                for c in ast.walk(fn):
                    if isinstance(c, ast.Call):
                        nm = c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
                        if nm in rest:
                            indirect.add(nm)
    return [s for s in rest if s not in indirect]


# ---------------------------------------------------------------- 치트 구현

def _literal(node: ast.AST) -> Any:
    return ast.literal_eval(node)


def assert_table(ws: Path, test_files: list[str]) -> dict[str, dict[str, Any]]:
    """`assert f(리터럴...) == 리터럴` 꼴을 모아 {함수명: {repr(args): 값}}."""
    table: dict[str, dict[str, Any]] = {}
    for tf in test_files:
        try:
            tree = ast.parse((ws / tf).read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
                continue
            pairs = [(node.left, node.comparators[0]), (node.comparators[0], node.left)]
            for call, val in pairs:
                if not isinstance(call, ast.Call):
                    continue
                fname = call.func.id if isinstance(call.func, ast.Name) else \
                    call.func.attr if isinstance(call.func, ast.Attribute) else None
                if not fname or call.keywords:
                    continue
                try:
                    args = tuple(_literal(a) for a in call.args)
                    value = _literal(val)
                except (ValueError, SyntaxError, TypeError):
                    continue
                table.setdefault(fname, {})[repr(args)] = value
                break
    return table


def cheat_source(src: str, kind: str, table: dict[str, dict[str, Any]]) -> str | None:
    """대상 모듈의 최상위 함수 몸통을 치트로 바꾼 소스. 함수가 없으면 None."""
    tree = ast.parse(src)
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        return None
    for fn in funcs:
        fn.args = ast.arguments(posonlyargs=[], args=[], vararg=ast.arg(arg="_a"), kwonlyargs=[], kw_defaults=[],
                                kwarg=ast.arg(arg="_k"), defaults=[])
        fn.decorator_list = []
        if kind == "const":
            body = "return 0"
        elif kind == "raise":
            body = "raise ValueError('cap cheat')"
        elif kind == "identity":
            body = "return _a[0] if _a else None"
        else:
            t = table.get(fn.name, {})
            body = f"_t = {t!r}\nreturn _t[repr(tuple(_a))]"
        fn.body = ast.parse(body).body
    return ast.unparse(ast.fix_missing_locations(tree))


# ---------------------------------------------------------------- 돌연변이

CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
            ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.FloorDiv: ast.Div,
            ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult}


def _sites(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    sites: list[tuple[str, ast.AST]] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and type(node.ops[0]) in CMP_SWAP:
                sites.append(("cmp", node))
            elif isinstance(node, ast.BinOp) and type(node.op) in BIN_SWAP:
                sites.append(("binop", node))
            elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                    and not isinstance(node.value, bool):
                sites.append(("const", node))
            elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
                sites.append(("bool", node))
            elif isinstance(node, ast.Return) and node.value is not None:
                sites.append(("return", node))
            elif isinstance(node, ast.If):
                sites.append(("negate_if", node))
    # 중복 제거(같은 노드 객체)
    seen, out = set(), []
    for k, n in sites:
        if id(n) not in seen:
            seen.add(id(n))
            out.append((k, n))
    return out


def mutants(src: str, limit: int = MAX_MUTANTS) -> list[dict]:
    tree = ast.parse(src)
    sites = _sites(tree)
    if not sites:
        return []
    if len(sites) > limit:   # 고르게 뽑기(결정적)
        step = len(sites) / limit
        idxs = sorted({int(i * step) for i in range(limit)})
    else:
        idxs = list(range(len(sites)))
    out = []
    for i in idxs:
        t2 = copy.deepcopy(tree)
        kind, node = _sites(t2)[i]
        line = getattr(node, "lineno", 0)
        if kind == "cmp":
            node.ops[0] = CMP_SWAP[type(node.ops[0])]()
        elif kind == "binop":
            node.op = BIN_SWAP[type(node.op)]()
        elif kind == "const":
            node.value = node.value + 1
        elif kind == "bool":
            node.value = not node.value
        elif kind == "return":
            node.value = ast.Constant(value=None)
        elif kind == "negate_if":
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        try:
            new_src = ast.unparse(ast.fix_missing_locations(t2))
        except Exception:  # noqa: BLE001
            continue
        if new_src == ast.unparse(tree):
            continue
        out.append({"op": kind, "line": line, "src": new_src})
    return out


# ---------------------------------------------------------------- 실행

@dataclass
class EvalReport:
    test_cmd: str
    targets: list[str]
    test_files: list[str]
    baseline_pass: bool | None = None
    baseline_note: str = ""
    cheats: dict[str, Any] = field(default_factory=dict)
    mutation: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Sandbox:
    def __init__(self, ws: Path):
        self.ws = ws
        self.dir = Path(tempfile.mkdtemp(prefix="cap_eval_"))
        ign = shutil.ignore_patterns(*IGNORE_DIRS, "*.egg-info", "*.pyc")
        shutil.copytree(ws, self.dir / "w", ignore=ign)
        self.root = self.dir / "w"

    def run_with(self, target: str, src: str | None, cmd: str, timeout: int = 120) -> tuple[int, str]:
        tp = self.root / target
        orig = tp.read_bytes()
        try:
            if src is not None:
                tp.write_text(src, encoding="utf-8")
            return run_shell(cmd, self.root, timeout=timeout)
        finally:
            tp.write_bytes(orig)

    def close(self) -> None:
        _rmtree(self.dir)


def _no_tests(code: int, out: str) -> bool:
    return code == 5 or "no tests ran" in out or "collected 0 items" in out


def check_evaluator(ws: Path, test_cmd: str, targets: list[str] | None = None,
                    mutation: bool = True, printer=print) -> EvalReport:
    import time
    t0 = time.perf_counter()
    tfiles = collect_test_files(ws)
    targets = targets or find_targets(ws, tfiles)
    rep = EvalReport(test_cmd=test_cmd, targets=targets, test_files=tfiles)
    if not tfiles:
        rep.blocked, rep.reasons = True, ["테스트 파일이 없다 — 평가기가 아무것도 확인하지 않는다"]
        return rep
    if not targets:
        rep.reasons.append("테스트가 import 하는 로컬 모듈을 못 찾아 치트·돌연변이 시험을 건너뜀(적용 불가)")
        return rep
    sb = Sandbox(ws)
    try:
        code, out = sb.run_with(targets[0], None, test_cmd)
        rep.baseline_pass = code == 0
        if _no_tests(code, out):
            rep.blocked = True
            rep.reasons.append("평가 명령이 테스트를 하나도 실행하지 않는다")
            return rep
        rep.baseline_note = "현재 코드가 평가를 통과함" if code == 0 else "현재 코드는 평가에 탈락(고칠 것이 있음)"
        printer(f"    기준선: {rep.baseline_note}")
        table = assert_table(ws, tfiles)
        for kind in CHEATS:
            passed_any = False
            for tgt in targets:
                src = (sb.root / tgt).read_text(encoding="utf-8")
                cheat = cheat_source(src, kind, table)
                if cheat is None:
                    continue
                c, o = sb.run_with(tgt, cheat, test_cmd)
                if c == 0:
                    passed_any = True
                    break
            rep.cheats[kind] = "통과(나쁨)" if passed_any else "탈락(좋음)"
            printer(f"    치트 {kind:8s}: {rep.cheats[kind]}")
            if passed_any:
                rep.blocked = True
                rep.reasons.append(_cheat_reason(kind))
        if mutation and rep.baseline_pass:
            rep.mutation = mutation_score(sb, targets, test_cmd, printer)
            if rep.mutation.get("total") and rep.mutation["score"] < MUTATION_THRESHOLD:
                rep.blocked = True
                rep.reasons.append(f"돌연변이 점수 {rep.mutation['score']:.0%} < {MUTATION_THRESHOLD:.0%}: "
                                   f"평가기가 그럴듯한 버그를 못 잡는다")
        elif mutation:
            rep.mutation = {"skipped": "현재 코드가 평가에 탈락 → 돌연변이 점수는 루프가 초록불을 낸 뒤 잰다"}
    finally:
        sb.close()
        rep.seconds = round(time.perf_counter() - t0, 1)
    return rep


def mutation_score(sb: Sandbox, targets: list[str], test_cmd: str, printer=print) -> dict:
    killed, survived, total = 0, [], 0
    for tgt in targets:
        if not (sb.root / tgt).is_file():
            continue
        src = (sb.root / tgt).read_text(encoding="utf-8")
        for m in mutants(src):
            total += 1
            c, _ = sb.run_with(tgt, m["src"], test_cmd)
            if c != 0:
                killed += 1
            else:
                survived.append({"file": tgt, "op": m["op"], "line": m["line"]})
    score = killed / total if total else 1.0
    printer(f"    돌연변이: {killed}/{total} 잡음 (점수 {score:.0%})")
    return {"total": total, "killed": killed, "score": round(score, 3), "survived": survived[:10]}


def mutation_after_green(ws: Path, test_cmd: str, targets: list[str], printer=print) -> dict:
    sb = Sandbox(ws)
    try:
        return mutation_score(sb, targets, test_cmd, printer)
    finally:
        sb.close()


def _cheat_reason(kind: str) -> str:
    return {
        "const": "상수(0)만 반환하는 가짜 구현이 평가를 통과한다",
        "raise": "항상 예외를 던지는 가짜 구현이 평가를 통과한다",
        "identity": "입력을 그대로 돌려주는 가짜 구현이 평가를 통과한다",
        "lookup": "테스트의 assert 값을 외운 표 구현이 평가를 통과한다 — 숨은 예시·속성(반복) 테스트를 추가하라",
    }[kind]


def _rmtree(path) -> None:
    from .store import rmtree
    rmtree(path)
