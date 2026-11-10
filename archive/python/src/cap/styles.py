"""출력 스타일 (Claude Code 의 output styles 와 같은 형식).

파일: .cap/output-styles/<이름>.md, ~/.cap/output-styles, .claude/output-styles, ~/.claude/output-styles
      (맨 위 --- 안에 name·description, 본문 = 답하는 방식에 대한 지시)
기본 제공: concise · explanatory · learning.  /output-style <이름> 으로 고르고, ~/.cap/settings.json 의 "outputStyle" 이 시작 값.
고른 스타일의 지시는 매 요청에 덧붙는다(화면에는 원래 요청만 보인다).
"""
from __future__ import annotations

from pathlib import Path

BUILTIN = {
    "concise": ("short answers", "Answer as briefly as possible: results first, no restating the question, no lists "
                                 "unless needed. Code changes speak for themselves — summarize them in one or two lines."),
    "explanatory": ("explain choices while working", "While you work, add short 'Insight' notes (2-3 lines) explaining "
                    "why you chose an approach and what in this codebase made it the right one."),
    "learning": ("teach while coding", "The user is learning. Explain each step in plain words before doing it. For "
                 "small, self-contained pieces (one function, under 10 lines), describe what to write and leave a "
                 "TODO(human) for the user instead of writing it, then continue with the rest."),
}


def _dirs(ws: Path) -> list[Path]:
    from .compat import cap_home, claude_home, import_on
    out = [claude_home() / "output-styles"] if import_on("claude") else []
    out += [cap_home() / "output-styles", ws / ".claude" / "output-styles", ws / ".cap" / "output-styles"]
    return [d for d in out if d.is_dir()]


def available(ws: Path) -> dict[str, tuple[str, str]]:
    """이름 → (설명, 지시). 나중 것이 이긴다(프로젝트 > 사용자 > 기본)."""
    from .compat import front
    out = dict(BUILTIN)
    for d in _dirs(ws):
        for f in sorted(d.glob("*.md")):
            meta, body = front(f.read_text(encoding="utf-8", errors="replace"))
            if body.strip():
                out[(meta.get("name") or f.stem).lower()] = (meta.get("description", "")[:120], body.strip()[:2000])
    return out


def default_name() -> str:
    from .compat import settings
    v = settings().get("outputStyle")
    return str(v).lower() if isinstance(v, str) and v.lower() not in ("default", "") else ""
