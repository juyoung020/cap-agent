"""모델 호출 래퍼: 16k 창 예산 검사, 생각 모드 끔, 호출 수 집계.

- 키는 환경변수 KAU_API_KEY 또는 .env(현재 폴더부터 위로, 그리고 패키지 위쪽)에서 읽기만 한다.
- 출력·저장 금지: 예외 문자열에 키가 섞이면 지운다.
"""

from __future__ import annotations

import json
import re
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

BASE_URL = os.getenv("CAP_BASE_URL", "https://agent.kau.ac.kr/v1")
MODEL = os.getenv("CAP_MODEL", "qwen3.5-9b")
CONTEXT_WINDOW = int(os.getenv("CAP_CONTEXT", "16384"))   # 실험용으로 줄여 예산 경로를 시험할 수 있음
SAFETY_MARGIN = 600          # 추정 오차 대비 여유
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def set_thinking(on: bool) -> None:
    """/think: 모델의 생각(추론) 모드. 켜면 느리지만 어려운 문제에 낫다. 생각 내용은 답에서 뺀다."""
    NO_THINK["chat_template_kwargs"]["enable_thinking"] = bool(on)


def thinking() -> bool:
    return bool(NO_THINK["chat_template_kwargs"]["enable_thinking"])

STATS = {"calls": 0, "seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
# 추정 보정 배율: 실제 prompt_tokens / 추정 의 최댓값(실측: 코드가 많은 대화는 문자수/3 추정보다 ~17% 많음)
CALIB = {"factor": 1.2, "recent": []}   # 추정 → 실제 토큰 보정. 첫 측정 전에는 넉넉히 1.2
_client = None
_key = ""

# ---------------------------------------------------------------- 모델 공급자(Codex 의 model_providers)
# 기본은 수업 서버. ~/.cap/settings.json 의 "model_providers" 로 더하거나 바꾼다:
#   {"model_providers": {"이름": {"base_url": ..., "env_key": "키가 든 환경변수"(없으면 키 없이), "model": ...,
#                                "context_window": 32768, "thinking_param": false}}}
# 키는 공급자마다 그 환경변수에서만 읽는다(수업 서버 키가 다른 서버로 가지 않게).
BUILTIN_PROVIDERS = {
    "kau": {"base_url": "https://agent.kau.ac.kr/v1", "env_key": "KAU_API_KEY", "model": "qwen3.5-9b",
            "context_window": 16384, "thinking_param": True},
    "ollama": {"base_url": "http://localhost:11434/v1", "env_key": "", "model": "qwen3:8b", "context_window": 32768},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "env_key": "", "model": "", "context_window": 32768},
    "openai": {"base_url": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY", "model": "gpt-5-mini",
               "context_window": 128000},
    "anthropic": {"base_url": "https://api.anthropic.com/v1/", "env_key": "ANTHROPIC_API_KEY",
                  "model": "claude-sonnet-5-5", "context_window": 200000},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "env_key": "GEMINI_API_KEY",
               "model": "", "context_window": 128000},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY", "model": "",
                   "context_window": 128000},
}
PROVIDER = {"name": "kau", "env_key": "KAU_API_KEY", "thinking_param": True}


def providers() -> dict[str, dict]:
    out = {k: dict(v) for k, v in BUILTIN_PROVIDERS.items()}
    try:
        from .compat import settings
        for k, v in (settings().get("model_providers") or {}).items():
            if isinstance(v, dict):
                out[str(k)] = {**out.get(str(k), {}), **v}
    except Exception:  # noqa: BLE001
        pass
    return out


def use_provider(name: str, model: str | None = None) -> str:
    """공급자를 바꾼다: 주소·모델·창 크기·키 환경변수. 클라이언트와 읽어 둔 키는 버리고 다음 호출에서 새로 만든다."""
    global BASE_URL, MODEL, CONTEXT_WINDOW, _client, _key
    p = providers().get(name)
    if p is None:
        raise ValueError(f"no model provider named {name} (known: {', '.join(providers())})")
    if not p.get("base_url"):
        raise ValueError(f"provider {name} has no base_url")
    BASE_URL = str(p["base_url"])
    MODEL = str(model or p.get("model") or MODEL)
    CONTEXT_WINDOW = int(p.get("context_window") or CONTEXT_WINDOW)
    PROVIDER.update(name=name, env_key=str(p.get("env_key") or ""), thinking_param=bool(p.get("thinking_param")))
    _client, _key = None, ""
    CALIB.update(factor=1.2, recent=[])         # 모델이 바뀌면 토큰 추정도 처음부터
    return f"provider {name}: {BASE_URL} · model {MODEL} · context {CONTEXT_WINDOW:,}"


def _stored_key(provider: str) -> str:
    """cap setup 이 ~/.cap/auth.json 에 저장한 키."""
    try:
        from .setup import load_auth
        v = load_auth().get(provider) or {}
        return str(v.get("api_key") or "") if isinstance(v, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def _load_key() -> str:
    global _key
    if _key:
        return _key
    stored = _stored_key(PROVIDER["name"])
    if PROVIDER["name"] != "kau":                 # 다른 공급자: 그 공급자의 환경변수 → cap setup 으로 저장한 키(없으면 로컬 서버라 키 없이)
        env = PROVIDER.get("env_key") or ""
        key = (os.getenv(env, "") if env else "") or stored
        if not key:
            if not env:
                return "not-needed"
            raise RuntimeError(f"No API key for provider {PROVIDER['name']}: run `cap setup` or set {env}.")
        _key = key
        return key
    key = os.getenv("KAU_API_KEY") or os.getenv("CAP_API_KEY") or stored
    if not key:
        try:
            from dotenv import dotenv_values
        except ImportError:  # pragma: no cover
            dotenv_values = None
        cands = []
        here = Path.cwd().resolve()
        cands += [p / ".env" for p in [here, *here.parents]]
        pkg = Path(__file__).resolve()
        cands += [p / ".env" for p in pkg.parents]
        for c in cands:
            if c.is_file() and dotenv_values:
                vals = dotenv_values(c)
                key = vals.get("KAU_API_KEY") or vals.get("CAP_API_KEY")
                if key:
                    break
    if not key:
        raise RuntimeError("No API key: run `cap setup` to choose a model provider "
                           "(or set KAU_API_KEY for the class server).")
    _key = key
    return key


RATE_LIMIT_RETRIES = int(os.getenv("CAP_RATE_LIMIT_RETRIES", "10"))


def is_rate_limited(msg: str) -> bool:
    return bool(re.search(r"\b429\b|too many requests|rate.?limit|\b503\b|service unavailable", msg or "", re.I))


def _short_error(msg: str) -> str:
    """nginx 오류 HTML 같은 긴 본문을 한 줄로 줄인다(장부·화면용)."""
    if "<html" in (msg or "").lower():
        title = re.search(r"<title>(.*?)</title>", msg, re.I | re.S)
        return "HTTP 오류: " + (title.group(1).strip() if title else re.sub(r"<[^>]+>", " ", msg)[:120])
    return msg


def scrub(text: str) -> str:
    if _key and isinstance(text, str):
        return text.replace(_key, "<KEY>")
    return text


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=_load_key(), base_url=BASE_URL, timeout=300.0, max_retries=2)
    return _client


# ---------------------------------------------------------------- 토큰 추정

def est_text(s: str) -> int:
    """대략 추정: ASCII 3자 ≈ 1토큰, 비ASCII(한글 등) 1자 ≈ 1.1토큰. 보수적으로 잡는다."""
    if not s:
        return 0
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    extra = sum(image_tokens(m.group(2)) for m in IMAGE_RX.finditer(s)) if "[[image:" in s else 0
    return int((len(s) - non_ascii) / 3.0 + non_ascii * 1.1) + 1 + extra


# ---------------------------------------------------------------- 이미지 (@그림.png)
# 대화 상태에는 글자 표식만 둔다. 모델을 부르기 직전에 최근 이미지만 실제 이미지로 바꿔 보낸다.
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp"}
IMAGE_MAX_BYTES = 5_000_000
IMAGES_KEPT = 2                 # 이미지가 붙은 최근 사용자 메시지 2개만 이미지로, 그 전은 글자 안내로
IMAGE_RX = re.compile(r"(--- @[^\n]+ \(image\) ---)\n\[\[image:([^\]\n]+)\]\]")


def image_size(path: str) -> tuple[int, int]:
    """PNG·JPEG·GIF 머리에서 가로·세로(모르면 (0, 0)). 토큰 추정용."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return 0, 0
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", head[6:10])
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker, seg = head[i + 1], struct.unpack(">H", head[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            i += 2 + seg
    return 0, 0


def image_tokens(path: str) -> int:
    """비전 모델은 대략 28×28 칸 하나가 1토큰. 크기를 모르면 넉넉히 1,500."""
    w, h = image_size(path)
    if not w or not h:
        return 1500
    return min(6000, max(64, -(-w // 28) * -(-h // 28))) + 10


def with_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, kept = [], 0
    for m in reversed(messages):
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, str) and IMAGE_RX.search(c):
            if kept < IMAGES_KEPT:
                kept += 1
                parts: list[dict[str, Any]] = []
                for k, chunk in enumerate(IMAGE_RX.split(c)):
                    # split 결과: [글, 머리, 경로, 글, 머리, 경로, …]
                    if k % 3 == 0:
                        if chunk.strip():
                            parts.append({"type": "text", "text": chunk})
                    elif k % 3 == 1:
                        parts.append({"type": "text", "text": chunk})
                    else:
                        p = Path(chunk)
                        try:
                            data = p.read_bytes() if p.stat().st_size <= IMAGE_MAX_BYTES else b""
                        except OSError:
                            data = b""
                        if data:
                            import base64
                            mime = IMAGE_EXT.get(p.suffix.lower(), "image/png")
                            parts.append({"type": "image_url", "image_url": {
                                "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
                        else:
                            parts.append({"type": "text", "text": "(image could not be read)"})
                m = {**m, "content": parts}
            else:
                m = {**m, "content": IMAGE_RX.sub(lambda x: x.group(1) + "\n(image shown earlier, not re-sent)", c)}
        out.append(m)
    return list(reversed(out))


def calibrate(actual: int, raw_est: float) -> None:
    """서버가 센 실제 입력 토큰으로 추정을 맞춘다. 최근 8번 중 가장 큰 비율 + 5% — 넘치지 않을 만큼만 넉넉하게.
    (예전에는 올리기만 해서, 추정이 실제보다 1.5배 크면 턴 중간 압축이 너무 일찍 돌았다.)"""
    if actual <= 0 or raw_est <= 0:
        return
    rec = CALIB["recent"]
    rec.append(actual / raw_est)
    del rec[:-8]
    CALIB["factor"] = min(1.8, max(0.6, max(rec) * 1.05))


OVERFLOW_RX = re.compile(r"(\d[\d,]*)\s+(?:tokens\s+)?in the messages|messages? (?:resulted in|has|have)\s+(\d[\d,]*)\s+tokens"
                         r"|prompt (?:contains|has|is)\s+(\d[\d,]*)\s+tokens", re.I)


def learn_from_overflow(msg: str, messages: list[dict[str, Any]], tools: list[dict] | None) -> None:
    """서버가 '맥락 초과'로 거절했다 = 추정이 작았다. 서버가 센 입력 토큰이 오류문에 있으면 그대로 보정하고,
    없으면 보정 계수를 15% 올린다(다음 추정부터 일찍 줄이게)."""
    raw = est_messages(messages, tools) / CALIB["factor"]
    m = OVERFLOW_RX.search(msg or "")
    n = next((g for g in (m.groups() if m else ()) if g), "")
    if n and raw > 0:
        calibrate(int(n.replace(",", "")), raw)
    else:
        CALIB["recent"].append(CALIB["factor"] * 1.15)
        del CALIB["recent"][:-8]
        CALIB["factor"] = min(1.8, CALIB["factor"] * 1.15)


def keep_tail(messages: list[dict[str, Any]], n: int = 6) -> list[dict[str, Any]]:
    """시스템 + 첫 사용자 요청 + 최근 n 개. 도구 결과가 자기 호출 없이 맨 앞에 오지 않게 자른다."""
    head = messages[:2] if len(messages) > 2 and messages[1].get("role") == "user" else messages[:1]
    cut = max(len(head), len(messages) - n)
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return head + messages[cut:]


def est_messages(messages: list[dict[str, Any]], tools: list[dict] | None = None) -> int:
    total = 0
    for m in messages:
        total += 6
        c = m.get("content")
        if isinstance(c, str):
            total += est_text(c)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += est_text(fn.get("name", "")) + est_text(fn.get("arguments", "")) + 8
    if tools:
        total += est_text(json.dumps(tools, ensure_ascii=False))
    return int(total * CALIB["factor"])


def fit_budget(messages: list[dict[str, Any]], tools: list[dict] | None, max_tokens: int,
               min_out: int = 512) -> tuple[list[dict[str, Any]], int, list[str]]:
    """입력+max_tokens 가 창을 넘지 않게 맞춘다.

    순서: (1) 오래된 도구 출력부터 생략 표시로 줄임 (2) max_tokens 줄임(최소 min_out)
    (3) 그래도 넘으면 시스템·첫 사용자 메시지 뒤의 오래된 턴을 통째로 버림.
    반환: (새 메시지, max_tokens, 한 일 목록)
    """
    msgs = [dict(m) for m in messages]
    actions: list[str] = []
    limit = CONTEXT_WINDOW - SAFETY_MARGIN

    def over() -> int:
        return est_messages(msgs, tools) + max_tokens - limit

    if over() <= 0:
        return msgs, max_tokens, actions
    # (1) 오래된 도구 출력 줄이기 (마지막 4개 메시지는 보존)
    for i, m in enumerate(msgs[:-4]):
        if over() <= 0:
            break
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 300:
            n = len(m["content"])
            m["content"] = m["content"][:200] + f"\n…[예산 때문에 오래된 출력 {n}자 생략]"
            actions.append(f"trim_tool_output#{i}")
    # (2) max_tokens 줄이기
    if over() > 0 and max_tokens > min_out:
        max_tokens = max(min_out, max_tokens - over())
        actions.append(f"max_tokens->{max_tokens}")
    # (3) 오래된 턴 버리기: assistant(tool_calls)+tool 묶음 단위
    head = 2 if len(msgs) > 2 and msgs[1].get("role") == "user" else 1
    while over() > 0 and len(msgs) > head + 2:
        # head 다음의 첫 assistant 와 그 뒤 tool 들을 버린다
        j = head
        drop = [j]
        k = j + 1
        while k < len(msgs) and msgs[k].get("role") == "tool":
            drop.append(k)
            k += 1
        msgs = [m for idx, m in enumerate(msgs) if idx not in drop]
        actions.append("drop_old_turn")
    if msgs and msgs[head - 1:head] and any(a == "drop_old_turn" for a in actions):
        msgs.insert(head, {"role": "user", "content": "(오래된 대화 일부를 창 예산 때문에 생략함. 필요하면 파일을 다시 읽어라.)"})
    # 첫 메시지가 tool 이 되지 않게 정리
    while len(msgs) > head and msgs[head].get("role") == "tool":
        msgs.pop(head)
    return msgs, max_tokens, actions


# ---------------------------------------------------------------- 호출

def _stream_call(kwargs: dict, on_text: Callable[[str], None]) -> Any:
    """스트리밍 호출: 글자가 오는 대로 on_text(지금까지의 본문)를 부르고, 끝나면 비스트리밍 응답과 같은 모양으로 돌려준다.
    도구 호출 조각은 index 별로 이어 붙인다(수업 서버 vLLM 은 스트리밍에서도 tool_calls 를 준다 — 실측)."""
    from types import SimpleNamespace as NS
    stream = client().chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    content, calls, finish, usage, last = "", {}, None, {}, 0.0
    for ch in stream:
        if getattr(threading.current_thread(), "cap_abandoned", False):   # 대화창에서 중단: 더 읽지 않는다
            stream.close()
            raise RuntimeError("중단됨")
        if getattr(ch, "usage", None):
            usage = ch.usage.model_dump()
        if not ch.choices:
            continue
        d = ch.choices[0].delta
        if d.content:
            content += d.content
            now = time.perf_counter()
            if now - last > 0.08:
                last = now
                on_text(content)
        for tc in d.tool_calls or []:
            c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            c["id"] = tc.id or c["id"]
            if tc.function:
                c["name"] += tc.function.name or ""
                c["args"] += tc.function.arguments or ""
        finish = ch.choices[0].finish_reason or finish
    if content:
        on_text(content)
    tcs = [NS(id=c["id"] or f"call_{k}", function=NS(name=c["name"], arguments=c["args"] or "{}"))
           for k, c in sorted(calls.items()) if c["name"]]
    return NS(choices=[NS(message=NS(content=content, tool_calls=tcs), finish_reason=finish)],
              usage=NS(model_dump=lambda: usage) if usage else None)


def chat(messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
         max_tokens: int = 1024, temperature: float = 0.1,
         response_format: dict | None = None, ledger=None, tag: str = "",
         on_text: Callable[[str], None] | None = None,
         on_overflow: Callable[[list], list] | None = None) -> dict[str, Any]:
    """모델 호출. 반환: {content, tool_calls, finish_reason, usage, budget_actions}. on_text 를 주면 스트리밍.
    on_overflow: 서버가 맥락 초과로 거절하면 원래 메시지를 받아 줄인 메시지를 돌려주는 함수(예: 요약 압축)."""
    msgs, max_tokens, actions = fit_budget(messages, tools, max_tokens)
    msgs = with_images(msgs)
    kwargs: dict[str, Any] = dict(model=MODEL, messages=msgs, max_tokens=max_tokens,
                                  temperature=temperature,
                                  **({"extra_body": NO_THINK} if PROVIDER.get("thinking_param") else {}))
    if tools:
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = True
    if response_format:
        kwargs["response_format"] = response_format
    last_err = None
    attempts, attempt = 3, 0
    while True:
        t0 = time.perf_counter()
        try:
            resp = _stream_call(kwargs, on_text) if on_text else client().chat.completions.create(**kwargs)
            dt = time.perf_counter() - t0
            break
        except Exception as exc:  # noqa: BLE001
            dt = time.perf_counter() - t0
            msg = _short_error(scrub(str(exc)))
            last_err = msg
            STATS["calls"] += 1
            if getattr(threading.current_thread(), "cap_abandoned", False):   # 대화창에서 중단한 호출: 재시도 없이 끝
                raise RuntimeError("중단됨")
            STATS["seconds"] += dt
            busy = is_rate_limited(msg)
            if ledger:
                ledger.log("llm_error", tag=tag, error=msg[:500], seconds=round(dt, 2), rate_limited=busy)
            attempt += 1
            if busy:
                # 공유 서버의 동시 요청 제한(nginx 429): 짧게 2·4초 쉬고 포기하면 실행 전체가 죽는다(도그푸딩 실측).
                # 길게 물러나며 더 여러 번 기다린다.
                attempts = RATE_LIMIT_RETRIES
                if attempt >= attempts:
                    raise RuntimeError(f"모델 호출 실패(서버 혼잡, {attempt}회 대기 후 포기): {last_err}")
                time.sleep(min(60.0, 3.0 * 2 ** min(attempt, 5)))
                continue
            if attempt >= attempts:
                raise RuntimeError(f"모델 호출 실패: {last_err}")
            if "maximum context length" in msg or "context length" in msg:
                # 추정이 빗나간 경우: 추정을 바로잡고, 요약 압축(on_overflow)이 있으면 그것으로, 없으면 최근 것만 남겨 다시
                learn_from_overflow(msg, kwargs["messages"], tools)
                if ledger:
                    ledger.log("context_overflow", factor=round(CALIB["factor"], 3))
                shrunk = None
                if on_overflow is not None:
                    try:
                        shrunk = on_overflow(list(messages))
                    except Exception:  # noqa: BLE001
                        shrunk = None
                if not shrunk or len(shrunk) >= len(messages):
                    shrunk = keep_tail(list(messages))
                messages = shrunk
                kwargs["messages"], kwargs["max_tokens"], _ = fit_budget(messages, tools, kwargs["max_tokens"])
                kwargs["messages"] = with_images(kwargs["messages"])
                continue
            time.sleep(2 * attempt)
    STATS["calls"] += 1
    STATS["seconds"] += dt
    choice = resp.choices[0]
    msg = choice.message
    usage = resp.usage.model_dump() if resp.usage else {}
    STATS["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    STATS["last_prompt"] = usage.get("prompt_tokens", 0) or 0   # 마지막 호출의 실제 입력 토큰(대화창 맥락 표시)
    est_now = est_messages(kwargs["messages"], tools)
    if usage.get("prompt_tokens") and est_now:
        calibrate(usage["prompt_tokens"], est_now / CALIB["factor"])
    STATS["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    tool_calls = []
    for tc in msg.tool_calls or []:
        tool_calls.append({"id": tc.id, "type": "function",
                           "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
    content = msg.content or ""
    if "<think>" in content or "</think>" in content:
        content = re.sub(r"(?s)^.*?</think>", "", content) if "</think>" in content else content
        content = re.sub(r"(?s)<think>.*?(</think>|$)", "", content).strip()
    if tools and not tool_calls and "<tool_call>" in content:
        tool_calls = parse_leaked_tool_calls(content)
        if tool_calls:
            content = re.sub(r"<tool_call>.*?(</tool_call>|$)", "", content, flags=re.S).strip()
    if ledger:
        # 모델이 무엇을 생각·결정했는지 나중에 읽을 수 있게 본문 머리와 도구 호출 이름을 남긴다(도그푸딩: 장부만으로는 판단 근거가 안 보였음)
        ledger.log("llm_call", tag=tag, seconds=round(dt, 2), finish=choice.finish_reason,
                   usage=usage, tool_calls=len(tool_calls), budget_actions=actions,
                   est_prompt=est_messages(kwargs["messages"], tools),
                   text=content[:400], calls=[tc["function"]["name"] for tc in tool_calls])
    return {"content": content, "tool_calls": tool_calls, "finish_reason": choice.finish_reason,
            "usage": usage, "budget_actions": actions, "seconds": dt}


def chat_json(messages: list[dict[str, Any]], schema: dict, *, name: str = "out",
              max_tokens: int = 1500, ledger=None, tag: str = "") -> dict:
    """json_schema 강제 출력. 형식은 서버가, 판단은 코드가."""
    rf = {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
    for _ in range(2):
        r = chat(messages, max_tokens=max_tokens, temperature=0.1, response_format=rf, ledger=ledger, tag=tag)
        txt = r["content"].strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            # 코드 펜스 등 정리 후 재시도
            s, e = txt.find("{"), txt.rfind("}")
            if s >= 0 and e > s:
                try:
                    return json.loads(txt[s:e + 1])
                except json.JSONDecodeError:
                    pass
    raise RuntimeError("구조화 출력 파싱 실패")


def parse_leaked_tool_calls(content: str) -> list[dict]:
    """vLLM 파서가 놓쳐 본문에 `<tool_call>{json}</tool_call>` 로 샌 호출을 되살린다."""
    out = []
    for i, m in enumerate(re.finditer(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", content, flags=re.S)):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", {}))
        if not name:
            continue
        out.append({"id": f"leak_{i}_{abs(hash(m.group(1))) % 10**8}", "type": "function",
                    "function": {"name": name, "arguments": args if isinstance(args, str)
                                 else json.dumps(args, ensure_ascii=False)}})
    return out
