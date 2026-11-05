"""프로젝트 설정 믿기(훅·MCP·허용 규칙). 기록은 사용자 홈(~/.cap/trust.json)에 둔다.

프로젝트 폴더 안(.cap/)에 두면 남의 저장소가 믿음 기록까지 같이 커밋해 둘 수 있다(B14).
키 = 종류 + 작업 폴더 절대 경로, 값 = 그 설정 파일의 sha256. 파일이 바뀌면 다시 믿어 줘야 한다.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _store() -> Path:
    from . import memory
    return memory.user_home() / "trust.json"


def _key(kind: str, ws: Path) -> str:
    return f"{kind}:{ws.resolve().as_posix().lower()}"


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def is_trusted_hash(kind: str, ws: Path, h: str) -> bool:
    try:
        return json.loads(_store().read_text(encoding="utf-8")).get(_key(kind, ws)) == h
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def set_trusted_hash(kind: str, ws: Path, h: str) -> None:
    st = _store()
    try:
        data = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[_key(kind, ws)] = h
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps(data, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 프로젝트별 '다시 묻지 않기' 승인
# Claude Code 의 "Yes, and don't ask again for … in this project" 와 같다. 기록은 사용자 홈에(저장소가 끼워 넣지 못하게).
PROJECT_OK_PREFIXES = ("exec:", "mcp:")       # 명령 앞부분 하나, MCP 서버 하나 단위만. 사람 전용 범주는 저장하지 않는다


def _approvals_store() -> Path:
    from . import memory
    return memory.user_home() / "project_approvals.json"


def project_approvals(ws: Path) -> list[str]:
    try:
        v = json.loads(_approvals_store().read_text(encoding="utf-8")).get(_key("approve", ws), [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    return [c for c in v if isinstance(c, str) and c.startswith(PROJECT_OK_PREFIXES)]


def add_project_approval(ws: Path, category: str) -> bool:
    if not category.startswith(PROJECT_OK_PREFIXES):
        return False
    st = _approvals_store()
    try:
        data = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    cur = list(data.get(_key("approve", ws), []))
    if category not in cur:
        cur.append(category)
    data[_key("approve", ws)] = cur
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return True


def _proj_store() -> Path:
    from . import memory
    return memory.user_home() / "project_settings.json"


def project_setting(ws: Path, key: str) -> str:
    """사용자가 이 프로젝트에 정한 값(예: 테스트 명령). 사용자 홈에 있어 저장소가 끼워 넣지 못한다."""
    try:
        v = json.loads(_proj_store().read_text(encoding="utf-8")).get(_key("project", ws), {}).get(key, "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    return v if isinstance(v, str) else ""


def set_project_setting(ws: Path, key: str, value: str) -> None:
    st = _proj_store()
    try:
        data = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    cur = dict(data.get(_key("project", ws), {}))
    if value:
        cur[key] = value
    else:
        cur.pop(key, None)
    data[_key("project", ws)] = cur
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def clear_project_approvals(ws: Path) -> int:
    st = _approvals_store()
    try:
        data = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    n = len(data.pop(_key("approve", ws), []) or [])
    st.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return n


def is_trusted(kind: str, ws: Path, p: Path) -> bool:
    h = file_hash(p)
    if not h:
        return True
    try:
        return json.loads(_store().read_text(encoding="utf-8")).get(_key(kind, ws)) == h
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def set_trusted(kind: str, ws: Path, p: Path) -> str:
    h = file_hash(p)
    if not h:
        return ""
    st = _store()
    try:
        data = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[_key(kind, ws)] = h
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return h
