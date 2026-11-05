"""결정 감시자: 스펙 `## 결정` 줄 중 정해진 틀만 기계 검사한다. LLM 0회.

틀 (정규식으로 문장에서 뽑음):
- forbid_import(pkg)      "numpy import 금지", "requests 사용 금지", "do not import numpy", "no numpy"
- stdlib_only()           "표준 라이브러리만", "외부 의존성 금지/없이", "stdlib only", "no external dependencies"
- allow_deps([pkgs])      "의존성은 numpy, pandas 만", "allowed dependencies: numpy"
틀에 안 맞는 결정은 '사람 확인용'으로 남긴다(검사 가능 비율을 보고).

검사: 바뀐 .py 파일(테스트 제외)의 import 를 AST 로 읽어 비교. 편집 직후 도구 결과에 위반을 붙인다.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}

RX_FORBID = [
    re.compile(r"([A-Za-z_][\w.]*)\s*(?:모듈|패키지|라이브러리)?\s*(?:을|를)?\s*(?:import|임포트|사용|의존)\s*(?:을|를)?\s*(?:금지|하지\s*말|하지\s*않|불가|안\s*함)", re.I),
    re.compile(r"(?:do\s+not|don't|never)\s+(?:import|use)\s+([A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bno\s+([A-Za-z_][\w.]*)\s+(?:import|dependency)", re.I),
    re.compile(r"([A-Za-z_][\w.]*)\s*(?:은|는)?\s*(?:쓰지\s*말|쓰지\s*않|금지)", re.I),
]
RX_STDLIB = re.compile(r"표준\s*라이브러리만|외부\s*(?:의존성|패키지|라이브러리)\s*(?:금지|없이|사용\s*안|를?\s*쓰지)|"
                       r"stdlib\s*only|no\s+external\s+(?:dependenc|packages)", re.I)
RX_ALLOW = re.compile(r"(?:의존성|라이브러리|패키지|dependencies)\s*(?:은|는|:)?\s*([A-Za-z_][\w.\-]*(?:\s*[,/]\s*[A-Za-z_][\w.\-]*)*)\s*(?:만|only)", re.I)
RX_IMPORTY = re.compile(r"import|임포트|사용|의존|쓰지|use", re.I)
RX_NEG = re.compile(r"금지|안\s*된다|안\s*됨|않|말\s*것|말라|말아|불가|never|not|no|don't", re.I)
RX_IDENT = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
STOP_WIDE = {"import", "imports", "use", "using", "used", "do", "does", "not", "no", "never", "don", "t", "module",
             "modules", "package", "library", "the", "a", "an", "in", "any", "code", "only", "from", "and", "or",
             "python", "py", "it", "is", "be", "must", "should", "dependency", "dependencies"}
STOP = {"import", "use", "외부", "the", "a", "기존", "it", "this", "코드", "테스트", "test", "tests"}


@dataclass
class Guard:
    decision_id: str
    text: str
    kind: str                     # forbid_import | stdlib_only | allow_deps
    pkgs: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == "forbid_import":
            return f"import 금지: {', '.join(self.pkgs)}"
        if self.kind == "stdlib_only":
            return "표준 라이브러리만 허용"
        return f"허용 의존성: 표준 라이브러리 + {', '.join(self.pkgs)}"


def compile_decisions(decisions) -> tuple[list[Guard], list[str]]:
    """decisions: spec.Item 목록. 반환: (검사 틀, 틀에 안 맞는 결정 id)."""
    guards: list[Guard] = []
    manual: list[str] = []
    for d in decisions:
        t = d.text
        got = False
        if RX_STDLIB.search(t):
            guards.append(Guard(d.id, t, "stdlib_only"))
            got = True
        m = RX_ALLOW.search(t)
        if m and not got:
            pkgs = [p.strip() for p in re.split(r"[,/]", m.group(1)) if p.strip() and p.strip().lower() not in STOP]
            if pkgs:
                guards.append(Guard(d.id, t, "allow_deps", pkgs))
                got = True
        if not got and RX_IMPORTY.search(t) and RX_NEG.search(t):
            pk = [p for p in RX_IDENT.findall(t) if p.lower() not in STOP_WIDE]
            if pk:
                guards.append(Guard(d.id, t, "forbid_import", sorted(set(pk))))
                got = True
        if not got:
            for rx in RX_FORBID:
                pk = [p for p in rx.findall(t) if p.lower() not in STOP and not p.isdigit()]
                if pk:
                    guards.append(Guard(d.id, t, "forbid_import", sorted(set(pk))))
                    got = True
                    break
        if not got:
            manual.append(d.id)
    return guards, manual


def imports_of(src: str) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [(a.name, n.lineno) for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.append((n.module, n.lineno))
    return out


def _local_modules(ws: Path) -> set[str]:
    names = set()
    for p in ws.iterdir():
        if p.suffix == ".py":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
    src = ws / "src"
    if src.is_dir():
        for p in src.iterdir():
            names.add(p.stem if p.suffix == ".py" else p.name)
    return names


def check(guards: list[Guard], ws: Path, files: list[str]) -> list[dict]:
    """files(작업 폴더 상대 경로) 중 .py 의 import 를 결정과 비교. 위반 목록."""
    if not guards:
        return []
    local = _local_modules(ws)
    out = []
    for f in files:
        p = ws / f
        if not f.endswith(".py") or not p.is_file():
            continue
        name = Path(f).name
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            continue
        for mod, line in imports_of(p.read_text(encoding="utf-8", errors="replace")):
            top = mod.split(".")[0]
            for g in guards:
                bad = False
                if g.kind == "forbid_import":
                    bad = any(mod == x or mod.startswith(x + ".") or top == x for x in g.pkgs)
                elif g.kind == "stdlib_only":
                    bad = top not in STDLIB and top not in local
                elif g.kind == "allow_deps":
                    bad = top not in STDLIB and top not in local and top not in g.pkgs
                if bad:
                    out.append({"decision": g.decision_id, "rule": g.describe(), "file": f, "line": line,
                                "import": mod})
    return out


def format_violations(v: list[dict]) -> str:
    return "\n".join(f"결정 위반 {x['decision']} ({x['rule']}): {x['file']}:{x['line']} import {x['import']} "
                     f"— 결정을 지키도록 고쳐라" for x in v)
