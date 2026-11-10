"""Codex 의 apply_patch 형식(docs/codex/04_apply_patch.md): 여러 파일 추가·수정·삭제·이동을 한 번에.

*** Begin Patch
*** Add File: 경로          (+ 로 시작하는 줄이 새 파일 내용)
*** Delete File: 경로
*** Update File: 경로
*** Move to: 새 경로         (선택, Update 바로 뒤)
@@ [찾을 줄]                 (청크 시작, 뒤 글은 위치를 좁히는 앵커)
 문맥 / -지울 줄 / +넣을 줄
*** End of File             (선택)
*** End Patch

GPT 계열 모델이 익숙한 편집 형식이라 다른 공급자(/provider)를 쓸 때 켠다. 수업 서버의 9B 는 edit_file 이 기본.
청크는 edit_file 과 같은 공백 관용 매칭(apply_edit)으로 찾는다. 하나라도 실패하면 아무 파일도 바꾸지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA = {"type": "function", "function": {
    "name": "apply_patch",
    "description": ("Edit files with a patch (Codex format). Use for several files at once, new files, deletes and "
                    "moves. Format:\n*** Begin Patch\n*** Update File: path\n@@ optional anchor line\n context\n-old line\n"
                    "+new line\n*** Add File: path\n+line\n*** Delete File: path\n*** End Patch\n"
                    "Keep 1-3 context lines around each change. All or nothing: if one hunk fails, nothing changes."),
    "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"]}}}


@dataclass
class Hunk:
    anchor: str = ""
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class Op:
    kind: str                      # add · delete · update
    path: str
    move_to: str = ""
    content: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)


class PatchError(ValueError):
    pass


def parse(text: str) -> list[Op]:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise PatchError("the patch must start with '*** Begin Patch'")
    if lines[-1].strip() != "*** End Patch":
        raise PatchError("the patch must end with '*** End Patch'")
    ops: list[Op] = []
    cur: Op | None = None
    hunk: Hunk | None = None
    for raw in lines[1:-1]:
        ln = raw
        if ln.startswith("*** Add File: "):
            cur, hunk = Op("add", ln[14:].strip()), None
            ops.append(cur)
        elif ln.startswith("*** Delete File: "):
            cur, hunk = Op("delete", ln[17:].strip()), None
            ops.append(cur)
        elif ln.startswith("*** Update File: "):
            cur, hunk = Op("update", ln[17:].strip()), None
            ops.append(cur)
        elif ln.startswith("*** Move to: "):
            if cur is None or cur.kind != "update":
                raise PatchError("'*** Move to:' must follow '*** Update File:'")
            cur.move_to = ln[13:].strip()
        elif ln.strip() == "*** End of File":
            continue
        elif cur is None:
            raise PatchError(f"line outside a file section: {ln[:60]!r}")
        elif cur.kind == "add":
            if not ln.startswith("+"):
                raise PatchError(f"lines of an added file must start with '+': {ln[:60]!r}")
            cur.content.append(ln[1:])
        elif cur.kind == "delete":
            if ln.strip():
                raise PatchError(f"unexpected line after '*** Delete File': {ln[:60]!r}")
        else:
            if ln.startswith("@@"):
                hunk = Hunk(anchor=ln[2:].strip())
                cur.hunks.append(hunk)
                continue
            if hunk is None:
                hunk = Hunk()
                cur.hunks.append(hunk)
            if ln.startswith("+"):
                hunk.new.append(ln[1:])
            elif ln.startswith("-"):
                hunk.old.append(ln[1:])
            elif ln.startswith(" ") or ln == "":
                hunk.old.append(ln[1:] if ln else "")
                hunk.new.append(ln[1:] if ln else "")
            else:
                raise PatchError(f"hunk lines must start with ' ', '-' or '+': {ln[:60]!r}")
    if not ops:
        raise PatchError("the patch has no file sections")
    for op in ops:
        if not op.path:
            raise PatchError("a file section has no path")
        if op.kind == "update" and not op.hunks and not op.move_to:
            raise PatchError(f"'*** Update File: {op.path}' has no changes")
    return ops


def paths(text: str) -> list[tuple[str, str]]:
    """권한 판정용: [(종류, 경로)] — 이동은 원래 경로(update)와 새 경로(add) 둘 다."""
    out = []
    for op in parse(text):
        out.append((op.kind, op.path))
        if op.move_to:
            out.append(("add", op.move_to))
    return out


def apply_hunks(text: str, hunks: list[Hunk]) -> str:
    """청크를 차례로. 지울 줄이 없는 청크(순수 추가)는 Codex 처럼 파일 끝에 붙인다."""
    from .tools import apply_edit
    cur = text
    for k, h in enumerate(hunks, 1):
        if not any(x.strip() for x in h.old):
            add = "\n".join(h.new)
            cur = cur + ("" if cur.endswith("\n") or not cur else "\n") + add + ("\n" if add else "")
            continue
        old, new = "\n".join(h.old), "\n".join(h.new)
        r = apply_edit(cur, old, new)
        if not r.ok and h.anchor and "곳과 일치" in r.message:          # 같은 줄이 여러 곳: 앵커 뒤에서만 찾는다
            pos = cur.find(h.anchor)
            if pos >= 0:
                head, tail = cur[:pos], cur[pos:]
                r2 = apply_edit(tail, old, new)
                if r2.ok:
                    cur = head + (r2.new_text if r2.new_text is not None else tail)
                    continue
        if not r.ok:
            raise PatchError(f"hunk {k} did not match: {r.message}")
        if r.new_text is not None:
            cur = r.new_text
    return cur
