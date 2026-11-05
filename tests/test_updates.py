"""새 버전 알림: 가짜 fetch 로(네트워크 없음) 비교·하루 캐시·끄기를 본다."""

import json

import pytest

from cap import updates as U
from cap.compat import cap_home


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.delenv("CAP_NO_UPDATE_CHECK", raising=False)
    calls = []
    box = {"latest": "v0.10.0"}

    def fake():
        calls.append(1)
        return box["latest"]
    monkeypatch.setattr(U, "fetch_latest", fake)
    box["calls"] = calls
    return box


def test_version_comparison():
    assert U.is_newer("v0.10.0", "0.9.1")
    assert U.is_newer("0.9.1", "0.9.0") and U.is_newer("v1.0", "0.99.9")
    assert not U.is_newer("v0.9.0", "0.9.0") and not U.is_newer("0.9", "0.9.0")
    assert not U.is_newer("v0.8.5", "0.9.0") and not U.is_newer(None, "0.9.0") and not U.is_newer("latest", "0.9.0")


def test_newer_version_shows_one_line(fetch):
    line = U.check(current="0.9.1", now=1000.0)
    assert line == "CAP 0.10.0 is available — uv tool upgrade cap-agent (or pipx upgrade cap-agent)"
    cache = json.loads((cap_home() / "update.json").read_text(encoding="utf-8"))
    assert cache == {"checked": 1000.0, "latest": "v0.10.0"}


def test_same_or_older_shows_nothing(fetch):
    assert U.check(current="0.10.0", now=1000.0) == ""
    fetch["latest"] = "v0.9.0"
    assert U.check(current="0.10.0", now=1000.0 + U.DAY + 1) == ""
    fetch["latest"] = None                                   # 네트워크 실패
    assert U.check(current="0.10.0", now=1000.0 + 3 * U.DAY) == ""


def test_cache_prevents_second_fetch_within_a_day(fetch):
    assert U.check(current="0.9.0", now=1000.0)
    assert U.check(current="0.9.0", now=1000.0 + U.DAY - 5)              # 저장한 결과로 다시 알린다
    assert len(fetch["calls"]) == 1
    U.check(current="0.9.0", now=1000.0 + U.DAY + 5)
    assert len(fetch["calls"]) == 2


def test_fetch_errors_are_swallowed(monkeypatch):
    monkeypatch.delenv("CAP_NO_UPDATE_CHECK", raising=False)

    def boom():
        raise OSError("offline")
    monkeypatch.setattr(U, "fetch_latest", boom)
    assert U.check(current="0.9.0", now=5.0) == ""
    th = U.start(lambda line: None)
    if th is not None:
        th.join(5)


def test_opt_out_by_env_and_setting(fetch, monkeypatch):
    monkeypatch.setenv("CAP_NO_UPDATE_CHECK", "1")
    assert U.start(lambda line: None) is None and not U.enabled()
    monkeypatch.delenv("CAP_NO_UPDATE_CHECK")
    cap_home().mkdir(parents=True, exist_ok=True)
    (cap_home() / "settings.json").write_text(json.dumps({"update_check": False}), encoding="utf-8")
    assert U.start(lambda line: None) is None and not U.enabled()
    assert fetch["calls"] == []


def test_start_runs_in_background_and_delivers(fetch, monkeypatch):
    import cap
    monkeypatch.setattr(cap, "__version__", "0.9.1")
    got = []
    th = U.start(got.append)
    assert th is not None                                     # 처음은 백그라운드 스레드
    th.join(5)
    assert got == ["CAP 0.10.0 is available — uv tool upgrade cap-agent (or pipx upgrade cap-agent)"]
    assert U.start() is None                                  # 오늘 확인했으면 스레드 없이 저장한 결과로
    assert U.take().startswith("CAP 0.10.0") and U.take() == ""
    assert len(fetch["calls"]) == 1


def test_exec_and_one_shot_do_not_check(monkeypatch):
    """cap exec·cap "할 일" 은 one_shot — 확인은 repl·TUI 시작에서만."""
    import inspect

    from cap import chat as C
    assert "updates" not in inspect.getsource(C.one_shot)
    assert "updates.start" in inspect.getsource(C.repl)
