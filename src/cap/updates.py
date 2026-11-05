"""새 버전 알림: 대화형 시작(TUI·줄 모드) 때 하루에 한 번까지 GitHub 의 최신 릴리스를 확인해 한 줄로 알린다.

백그라운드 스레드에서 돌아 시작을 기다리게 하지 않고, 모든 오류는 삼킨다. 보내는 것은 평범한 User-Agent 뿐.
끄기: ~/.cap/settings.json "update_check": false 또는 CAP_NO_UPDATE_CHECK=1. 결과와 시각은 ~/.cap/update.json.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable

REPO = "juyoung020/cap-agent"
API = f"https://api.github.com/repos/{REPO}"
TIMEOUT = 3.0
DAY = 24 * 3600
NOTICE = "CAP {v} is available — uv tool upgrade cap-agent (or pipx upgrade cap-agent)"

_pending: list[str] = []
_lock = threading.Lock()


def cache_file():
    from .compat import cap_home
    return cap_home() / "update.json"


def enabled() -> bool:
    if os.environ.get("CAP_NO_UPDATE_CHECK", "").strip().lower() not in ("", "0", "false", "no", "off"):
        return False
    from .compat import settings
    return settings().get("update_check", True) is not False


def parse(v: str | None) -> tuple[int, ...] | None:
    """'v0.10.0' · '0.9.1' · 'cap-agent-1.2' → (0, 10, 0). 숫자 버전이 아니면 None. 꼬리(-rc1 등)는 무시."""
    m = re.search(r"(\d+(?:\.\d+)*)", str(v or ""))
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str | None, current: str) -> bool:
    a, b = parse(latest), parse(current)
    return a is not None and b is not None and a > b


def _get_json(url: str):
    from .extras import UA
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None:
        r = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None


def fetch_latest() -> str | None:
    """최신 릴리스의 태그(없으면 태그 목록에서 가장 높은 버전). 실패하면 None."""
    rel = _get_json(f"{API}/releases/latest")
    if isinstance(rel, dict) and rel.get("tag_name") and not rel.get("draft") and not rel.get("prerelease"):
        return str(rel["tag_name"])
    tags = _get_json(f"{API}/tags")
    if isinstance(tags, list):
        names = [str(t.get("name")) for t in tags if isinstance(t, dict) and parse(t.get("name"))]
        if names:
            return max(names, key=lambda n: parse(n))
    return None


def _read_cache() -> dict:
    try:
        d = json.loads(cache_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(d: dict) -> None:
    try:
        p = cache_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _fresh(cache: dict, now: float) -> bool:
    t = cache.get("checked")
    return isinstance(t, (int, float)) and 0 <= now - t < DAY


def notice_for(latest: str | None, current: str) -> str:
    if not is_newer(latest, current):
        return ""
    m = re.search(r"\d+(?:\.\d+)*", str(latest))
    return NOTICE.format(v=m.group(0) if m else latest)


def check(current: str | None = None, now: float | None = None) -> str:
    """하루 안에 확인했으면 저장한 결과로, 아니면 GitHub 에 물어서. 알릴 한 줄 또는 ""."""
    from . import __version__
    current = current or __version__
    now = time.time() if now is None else now
    cache = _read_cache()
    if _fresh(cache, now):
        return notice_for(cache.get("latest"), current)
    try:
        latest = fetch_latest()
    except Exception:  # noqa: BLE001 — 네트워크·형식 오류는 조용히
        latest = None
    _write_cache({"checked": now, "latest": latest or cache.get("latest")})
    return notice_for(latest, current)


def start(show: Callable[[str], None] | None = None) -> threading.Thread | None:
    """대화형 시작에서 부른다. 저장한 결과가 오늘 것이면 바로, 아니면 백그라운드에서 확인한다.
    알릴 줄은 show(line) 로 넘기거나(없으면) take() 로 꺼낸다."""
    def deliver(line: str) -> None:
        if not line:
            return
        if show is not None:
            try:
                show(line)
            except Exception:  # noqa: BLE001
                pass
        else:
            with _lock:
                _pending.append(line)

    try:
        if not enabled():
            return None
        if _fresh(_read_cache(), time.time()):
            deliver(check())
            return None
    except Exception:  # noqa: BLE001
        return None

    def work() -> None:
        try:
            deliver(check())
        except Exception:  # noqa: BLE001
            pass
    th = threading.Thread(target=work, daemon=True, name="cap-update-check")
    th.start()
    return th


def take() -> str:
    """기다리던 알림 한 줄(한 번만). 없으면 ""."""
    with _lock:
        return _pending.pop(0) if _pending else ""
