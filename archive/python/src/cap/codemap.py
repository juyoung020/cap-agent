"""코드 지도·정의 찾기 (Aider 의 repo map, Claude Code 의 코드 인텔리전스를 작게).

16k 창에서는 파일을 통째로 읽으며 길을 찾을 수 없다. 그래서
- map: 파일마다 최상위 클래스·함수(파이썬은 메서드까지)와 줄 번호를 짧게
- find: 이름의 정의 위치와, 그 이름이 쓰인 곳(파일:줄)
을 돌려준다. 파이썬은 ast 로 정확히, 다른 언어는 흔한 선언 모양을 정규식으로.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SKIP_DIRS = {".git", ".cap", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache",
             ".pytest_cache", "target", ".tox"}
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp",
            ".cs", ".rb", ".php", ".swift"}
DECL_RX = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:static\s+)?"
    r"(?:function\*?|class|interface|type|enum|struct|trait|impl|fn|func|def|const|let|var)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)")
MAP_CHARS = 6000


def _files(root: Path, ws: Path, limit: int = 2000) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix in CODE_EXT and p.is_file() and not (SKIP_DIRS & set(p.relative_to(ws).parts)):
            out.append(p)
            if len(out) >= limit:
                break
    return out


def _py_symbols(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((n.lineno, f"def {n.name}({', '.join(a.arg for a in n.args.args)})"))
        elif isinstance(n, ast.ClassDef):
            meths = [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            out.append((n.lineno, f"class {n.name}" + (f": {', '.join(meths[:12])}" + (" …" if len(meths) > 12 else "")
                                                      if meths else "")))
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id.isupper():
            out.append((n.lineno, n.targets[0].id))
    return out


def _other_symbols(text: str) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        if ln[:1] in (" ", "\t") and not ln.lstrip().startswith(("export", "pub", "func", "fn")):
            continue                                  # 들여쓴 줄은 대개 지역 선언 — 최상위만
        m = DECL_RX.match(ln)
        if m:
            out.append((i, ln.strip()[:90]))
    return out


def symbols_of(p: Path) -> list[tuple[int, str]]:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _py_symbols(text) if p.suffix == ".py" else _other_symbols(text)


def code_map(ws: Path, path: str = "") -> str:
    root = (ws / path).resolve() if path else ws.resolve()
    if ws.resolve() not in (root, *root.parents) or not root.exists():
        return "path must be a folder or file inside the workspace"
    files = [root] if root.is_file() else _files(root, ws.resolve())
    lines, used = [], 0
    for f in files:
        syms = symbols_of(f)
        rel = f.relative_to(ws.resolve()).as_posix()
        chunk = rel + "\n" + "".join(f"  {ln}: {s}\n" for ln, s in syms[:40]) + \
            (f"  … +{len(syms) - 40} more\n" if len(syms) > 40 else "")
        if used + len(chunk) > MAP_CHARS:
            lines.append(f"…(map cut at {MAP_CHARS} chars — call symbols(action='map', path=<folder>) for a part)")
            break
        lines.append(chunk.rstrip("\n"))
        used += len(chunk)
    return "\n".join(lines) or "(no code files)"


def find_symbol(ws: Path, name: str, limit: int = 40) -> str:
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name or ""):
        return "name must be a single identifier, e.g. normalize_name"
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    defs, uses = [], []
    for f in _files(ws.resolve(), ws.resolve()):
        rel = f.relative_to(ws.resolve()).as_posix()
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name not in text:
            continue
        def_lines = set()
        if f.suffix == ".py":
            try:
                for n in ast.walk(ast.parse(text)):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
                        def_lines.add(n.lineno)
            except (SyntaxError, ValueError):
                pass
        lines = text.splitlines()
        for i, ln in enumerate(lines, 1):
            if not word.search(ln):
                continue
            m = DECL_RX.match(ln) if f.suffix != ".py" else None
            if i in def_lines or (m and m.group(1) == name):
                defs.append(f"{rel}:{i}: {ln.strip()[:120]}")
            else:
                uses.append(f"{rel}:{i}: {ln.strip()[:120]}")
    out = ["definitions:"] + (defs or ["  (none found)"])
    out += [f"used in {len(uses)} places" + (":" if uses else "")] + uses[:limit]
    if len(uses) > limit:
        out.append(f"… +{len(uses) - limit} more (narrow with search)")
    return "\n".join(out)


SCHEMA = {"type": "function", "function": {
    "name": "symbols",
    "description": ("Code navigation without reading whole files. action='map': each code file with its top-level "
                    "classes/functions and line numbers (optionally only under `path`). action='find': where `name` is "
                    "defined and where it is used (file:line). Then read_file only the lines you need."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["map", "find"]},
        "name": {"type": "string", "description": "for find: a function/class/variable name"},
        "path": {"type": "string", "description": "for map: folder or file (default: whole workspace)"}},
        "required": ["action"]}}}
