"""처음 실행 설정(`cap setup`): 모델 공급자를 고르고, 키를 저장하고, 실제로 한 번 불러 본다.

키는 ~/.cap/auth.json 에 공급자별로 둔다(이 계정만 읽게 권한을 좁힌다). 공급자의 환경변수가 있으면 그것이 먼저다.
고른 공급자·모델은 ~/.cap/settings.json 의 "provider"·"model" 로 남아 다음 실행부터 기본이 된다.
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from . import llm

# 처음 설정에서 보여 줄 순서와 한 줄 설명. 주소·키 환경변수는 llm.BUILTIN_PROVIDERS 에 있다.
MENU = [
    ("openai", "OpenAI (API key)"),
    ("anthropic", "Anthropic Claude (API key, OpenAI-compatible endpoint)"),
    ("gemini", "Google Gemini (API key, OpenAI-compatible endpoint)"),
    ("openrouter", "OpenRouter (API key, many models)"),
    ("ollama", "Ollama on this computer (no key)"),
    ("lmstudio", "LM Studio on this computer (no key)"),
    ("kau", "KAU class server (class API key)"),
    ("custom", "Another OpenAI-compatible server (enter its URL)"),
]


def auth_path() -> Path:
    from .memory import user_home
    return user_home() / "auth.json"


def load_auth() -> dict:
    try:
        d = json.loads(auth_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _restrict(p: Path) -> None:
    """키 파일은 이 계정만 읽게: Windows 는 상속을 끊고 현재 사용자만, 그 밖은 chmod 600."""
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F", "/Q"], capture_output=True)
    else:
        os.chmod(p, 0o600)


def save_key(provider: str, key: str) -> Path:
    p = auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_auth()
    data[provider] = {"api_key": key}
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    _restrict(p)
    return p


def save_settings(**kv) -> None:
    from .compat import cap_home
    p = cap_home() / "settings.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
        cur = cur if isinstance(cur, dict) else {}
    except (OSError, json.JSONDecodeError):
        cur = {}
    for k, v in kv.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k] = {**cur[k], **v}
        else:
            cur[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")


def list_models(timeout: float = 10.0) -> list[str]:
    try:
        from openai import OpenAI
        c = OpenAI(api_key=llm._load_key(), base_url=llm.BASE_URL, timeout=timeout, max_retries=0)
        return sorted(m.id for m in c.models.list().data)[:200]
    except Exception:  # noqa: BLE001 — 목록을 주지 않는 서버도 있다
        return []


def test_call() -> tuple[bool, str]:
    try:
        r = llm.chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=20)
        return True, (r.get("content") or "").strip()[:60] or "(empty reply)"
    except Exception as exc:  # noqa: BLE001
        return False, llm.scrub(str(exc))[:300]


def run_setup(provider: str | None = None, model: str | None = None, key: str | None = None,
              base_url: str | None = None, test: bool = True, ask: Callable[[str], str] = input,
              secret: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print) -> int:
    """대화식(인자 없이) 또는 인자로 설정한다. 반환: 0 = 저장됨, 1 = 실패·취소."""
    interactive = provider is None
    if interactive:
        out("CAP setup — choose the model provider CAP should use:")
        for k, (name, desc) in enumerate(MENU, 1):
            out(f"  {k}) {desc}")
        pick = ask("Number [1]: ").strip() or "1"
        if not pick.isdigit() or not 1 <= int(pick) <= len(MENU):
            out("Cancelled.")
            return 1
        provider = MENU[int(pick) - 1][0]
    provs = llm.providers()
    if provider == "custom":
        base_url = base_url or (ask("Server URL (e.g. http://localhost:8000/v1): ").strip() if interactive else "")
        if not base_url:
            out("A custom provider needs a URL.")
            return 1
        save_settings(model_providers={"custom": {"base_url": base_url, "env_key": "CAP_CUSTOM_API_KEY",
                                                  "model": model or "", "context_window": 32768}})
        provs = llm.providers()
    if provider not in provs:
        out(f"Unknown provider {provider}. Known: {', '.join(provs)}")
        return 1
    p = provs[provider]
    env = p.get("env_key") or ""
    needs_key = bool(env) or provider == "custom"
    if needs_key and key is None:
        if env and os.environ.get(env):
            out(f"Using the key in the environment variable {env}.")
        elif interactive:
            key = secret(f"API key for {provider} (input hidden; Enter to skip if the server needs none): ").strip()
    try:
        out(llm.use_provider(provider, model))
    except ValueError as exc:
        out(str(exc))
        return 1
    if key:
        llm._key = key                      # 시험 호출에만 쓰고, 성공하면 저장한다
    if interactive and not model:
        models = list_models()
        if models:
            shown = models[:15]
            out("Models on this server: " + ", ".join(shown) + (" …" if len(models) > 15 else ""))
        default = llm.MODEL or (models[0] if models else "")
        chosen = ask(f"Model [{default}]: ").strip() or default
        if not chosen:
            out("No model chosen.")
            return 1
        llm.MODEL = chosen
    if test:
        ok, msg = test_call()
        out(("✓ The model answered: " if ok else "✗ Test call failed: ") + msg)
        if not ok:
            out("Nothing was changed in your settings. Check the key, the URL or the model name, then run `cap setup` again.")
            return 1
    if key:
        where = save_key(provider, key)
        out(f"Key saved to {where} (readable only by your account).")
    save_settings(provider=provider, model=llm.MODEL)
    out(f"Saved: provider {provider}, model {llm.MODEL}. Change it any time with `cap setup` or /provider.")
    return 0


def apply_user_default() -> None:
    """~/.cap/settings.json 의 "provider"·"model" 을 기본으로(없으면 수업 서버 그대로)."""
    from .compat import settings
    st = settings()
    name = st.get("provider")
    if isinstance(name, str) and name and name != llm.PROVIDER.get("name"):
        try:
            llm.use_provider(name, st.get("model") if isinstance(st.get("model"), str) else None)
        except ValueError:
            pass
    elif isinstance(st.get("model"), str) and st.get("model"):
        llm.MODEL = st["model"]


def has_credentials() -> bool:
    try:
        llm._load_key()
        return True
    except RuntimeError:
        return False
