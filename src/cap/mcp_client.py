"""MCP(Model Context Protocol) 클라이언트 — 외부 도구 서버를 붙여 CAP 의 도구로 쓴다(수업 7주차 MCP).

설정(Claude Code·Codex 와 같은 모양): 프로젝트 `.cap/mcp.json`, 사용자 `~/.cap/mcp.json`
  {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {...}}}}
- 도구 이름은 `mcp__<서버>__<도구>`(Codex·Claude Code 관례). 설명은 1,000자로 자른다(Codex 03 mcp_tool.rs).
- 무엇을 하는 도구인지 CAP 이 알 수 없으므로 권한표에서 서버 단위 승인 대상이다(bypass 제외, chat·plan 에서는 안 씀).
  하위 에이전트에게는 주지 않는다.
- 프로젝트 설정은 남이 만든 저장소에 들어 있을 수 있는 실행 명령이라 `/mcp trust` 로 믿어 줘야 연결한다(훅과 같음).
- `mcp` 패키지(선택 설치: pip install "cap[mcp]")가 없으면 조용히 꺼진다. 서버는 백그라운드 asyncio 스레드에서 산다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 120
OUT_MAX = 6000


def project_file(ws: Path) -> Path:
    return ws / ".cap" / "mcp.json"


def user_file() -> Path:
    return Path.home() / ".cap" / "mcp.json"


def _servers(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("mcpServers") or d.get("servers") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def project_hash(ws: Path) -> str:
    """.cap/mcp.json 과 Claude Code 의 .mcp.json 을 함께."""
    from .compat import project_mcp_files
    h = hashlib.sha256()
    files = project_mcp_files(ws)
    for p in files:
        h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest() if files else ""


def project_servers(ws: Path) -> dict:
    from .compat import project_mcp_files
    out: dict = {}
    for p in project_mcp_files(ws):
        out.update(_servers(p))
    return out


def trusted(ws: Path) -> bool:
    from .trust import is_trusted_hash
    h = project_hash(ws)
    return not h or is_trusted_hash("mcp", ws, h)


def trust(ws: Path) -> str:
    from .trust import set_trusted_hash
    h = project_hash(ws)
    if not h:
        return "No project MCP config (.cap/mcp.json or .mcp.json)."
    set_trusted_hash("mcp", ws, h)
    return f"Trusted project MCP servers ({h[:12]}). Restart CAP (or /clear) to connect."


VAR_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_vars(v):
    """Claude Code .mcp.json 처럼 ${VAR}, ${VAR:-기본값} 을 환경변수로 바꾼다(값은 화면·장부에 남기지 않는다)."""
    import os
    if isinstance(v, str):
        return VAR_RX.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, list):
        return [expand_vars(x) for x in v]
    if isinstance(v, dict):
        return {k: expand_vars(x) for k, x in v.items()}
    return v


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def available() -> bool:
    try:
        import mcp  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
        return True
    except ImportError:
        return False


class MCPManager:
    """설정된 MCP 서버들에 연결해 두고, 도구 목록과 호출을 동기 함수로 제공한다."""

    def __init__(self, ws: Path, configs: dict[str, dict] | None = None):
        self.ws = ws
        self.untrusted = bool(project_hash(ws)) and not trusted(ws)
        self.foreign: dict[str, tuple[str, dict]] = {}
        if configs is None:
            from .compat import enabled_foreign, foreign_mcp
            configs = dict(_servers(user_file()))
            if not self.untrusted:
                configs.update(project_servers(ws))
            # 사용자의 Claude·Codex MCP 서버와 플러그인 서버: /mcp enable 로 켠 것만(도구 설명이 16k 창을 채운다)
            self.foreign = foreign_mcp(ws)
            on = set(enabled_foreign())
            for n, (_, c) in self.foreign.items():
                if n in on and n not in configs:
                    configs[n] = c
        self.configs = configs
        self.tools: dict[str, dict] = {}            # 노출 이름 → {server, tool, schema}
        self.resources: dict[str, list[dict]] = {}  # 서버 → [{uri, name, description, mime}]
        self.prompts: dict[str, dict] = {}          # mcp__서버__프롬프트 → {server, name, description, args}
        self.last_images: list[tuple[str, str]] = []  # 마지막 도구 호출이 돌려준 그림(작업 폴더 기준 경로, 절대 경로)
        self.errors: dict[str, str] = {}
        self._sessions: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        if configs and available():
            self._start()
        elif configs:
            self.errors["*"] = 'the "mcp" package is not installed (pip install mcp)'

    # -- 백그라운드 이벤트 루프
    def _start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop = asyncio.Event()
            self._loop.run_until_complete(self._main(ready))
        self._thread = threading.Thread(target=run, daemon=True, name="cap-mcp")
        self._thread.start()
        ready.wait(CONNECT_TIMEOUT + 5)

    async def _main(self, ready: threading.Event) -> None:
        pending = {name: asyncio.ensure_future(self._serve(name, cfg)) for name, cfg in self.configs.items()}
        try:
            await asyncio.wait_for(self._all_ready(), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            for name in self.configs:
                if name not in self._sessions and name not in self.errors:
                    self.errors[name] = f"did not start within {CONNECT_TIMEOUT}s"
        ready.set()
        await self._stop.wait()
        for t in pending.values():
            t.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)

    async def _all_ready(self) -> None:
        while len(self._sessions) + len(self.errors) < len(self.configs):
            await asyncio.sleep(0.05)

    def _transport(self, cfg: dict):
        """stdio(command) 또는 원격(url: Claude Code 의 type http / streamable-http) 연결."""
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        from .tools import shell_env
        cfg = expand_vars(cfg)
        if cfg.get("url"):
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
            return streamable_http_client(cfg["url"], http_client=create_mcp_http_client(headers=headers or None))
        env = {**shell_env(), **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
        params = StdioServerParameters(command=cfg["command"], args=[str(a) for a in cfg.get("args") or []],
                                       env=env, cwd=cfg.get("cwd") or str(self.ws))
        return stdio_client(params)

    async def _serve(self, name: str, cfg: dict) -> None:
        from mcp import ClientSession
        try:
            async with self._transport(cfg) as streams:
                r, w = streams[0], streams[1]
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    listed = await s.list_tools()
                    for t in listed.tools:
                        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                        schema = dict(schema) if isinstance(schema, dict) else {}
                        schema.setdefault("type", "object")
                        schema.setdefault("properties", {})
                        n = tool_name(name, t.name)
                        self.tools[n] = {"server": name, "tool": t.name, "schema": {"type": "function", "function": {
                            "name": n, "description": f"[MCP {name}] {(t.description or '')}"[:1000], "parameters": schema}}}
                    await self._list_extras(name, s)
                    self._sessions[name] = s
                    await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 서버 하나가 죽어도 나머지는 산다
            self.errors[name] = f"{type(exc).__name__}: {str(exc)[:200]}"

    async def _list_extras(self, name: str, s) -> None:
        """리소스·프롬프트(서버가 지원할 때만). 실패해도 도구는 그대로 쓴다."""
        try:
            rs = await s.list_resources()
            self.resources[name] = [{"uri": str(r.uri), "name": r.name or "", "description": (r.description or "")[:200],
                                     "mime": getattr(r, "mime_type", None) or getattr(r, "mimeType", None) or ""}
                                    for r in rs.resources][:200]
        except Exception:  # noqa: BLE001
            self.resources.pop(name, None)
        try:
            ps = await s.list_prompts()
            for p in ps.prompts:
                args = [{"name": a.name, "required": bool(a.required)} for a in (p.arguments or [])]
                self.prompts[tool_name(name, p.name)] = {"server": name, "name": p.name,
                                                         "description": (p.description or "")[:200], "args": args}
        except Exception:  # noqa: BLE001
            pass

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(CALL_TIMEOUT)

    # -- 리소스(Codex list_mcp_resources·read_mcp_resource 와 같은 두 도구)
    def has_resources(self) -> bool:
        return any(self.resources.get(n) for n in self._sessions)

    def resource_schemas(self) -> list[dict]:
        if not self.has_resources():
            return []
        servers = sorted(n for n in self._sessions if self.resources.get(n))
        return [
            {"type": "function", "function": {
                "name": "list_mcp_resources",
                "description": "List data the connected MCP servers offer as resources (files, docs, records). "
                               f"Servers with resources: {', '.join(servers)}.",
                "parameters": {"type": "object", "properties": {"server": {"type": "string",
                               "description": "only this server (optional)"}}}}},
            {"type": "function", "function": {
                "name": "read_mcp_resource",
                "description": "Read one MCP resource by its server and uri (from list_mcp_resources).",
                "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
                               "required": ["server", "uri"]}}}]

    def list_resources(self, server: str = "") -> tuple[bool, str]:
        rows = []
        for n in sorted(self.resources):
            if server and n != server:
                continue
            for r in self.resources[n]:
                rows.append(f"{n}  {r['uri']}  {r['name']}" + (f" — {r['description']}" if r["description"] else ""))
        return (True, "\n".join(rows)[:OUT_MAX]) if rows else (False, f"no resources{' on ' + server if server else ''}")

    def read_resource(self, server: str, uri: str) -> tuple[bool, str]:
        s = self._sessions.get(server)
        if s is None or self._loop is None:
            return False, f"MCP server {server} is not connected"
        try:
            res = self._run(s.read_resource(uri))
        except Exception as exc:  # noqa: BLE001
            return False, f"MCP read failed: {type(exc).__name__}: {str(exc)[:300]}"
        parts = []
        for c in getattr(res, "contents", None) or []:
            txt = getattr(c, "text", None)
            parts.append(txt if txt is not None else f"[binary {getattr(c, 'mime_type', '') or 'data'} not shown]")
        return True, ("\n".join(parts) or "(empty)")[:OUT_MAX]

    # -- 프롬프트(Claude Code 처럼 /mcp__서버__이름 인자… 로 부른다)
    def get_prompt(self, cmd: str, rest: str) -> tuple[bool, str]:
        p = self.prompts.get(cmd.lstrip("/"))
        if not p or p["server"] not in self._sessions or self._loop is None:
            return False, f"MCP prompt {cmd} is not available"
        import shlex
        try:
            words = shlex.split(rest)
        except ValueError:
            words = rest.split()
        names = [a["name"] for a in p["args"]]
        vals: dict[str, str] = {}
        for k, n in enumerate(names):
            if k < len(words):
                vals[n] = " ".join(words[k:]) if k == len(names) - 1 else words[k]
        miss = [a["name"] for a in p["args"] if a["required"] and a["name"] not in vals]
        if miss:
            return False, f"{cmd} needs: {' '.join('<' + m + '>' for m in miss)}"
        try:
            res = self._run(self._sessions[p["server"]].get_prompt(p["name"], vals or None))
        except Exception as exc:  # noqa: BLE001
            return False, f"MCP prompt failed: {type(exc).__name__}: {str(exc)[:300]}"
        parts = []
        for m in getattr(res, "messages", None) or []:
            c = getattr(m, "content", None)
            parts.append(getattr(c, "text", None) or "")
        text = "\n\n".join(x for x in parts if x).strip()
        return (True, text[:OUT_MAX]) if text else (False, f"{cmd} returned no text")

    # -- 동기 API
    def schemas(self) -> list[dict]:
        return [t["schema"] for t in self.tools.values()]

    def call(self, name: str, args: dict) -> tuple[bool, str]:
        t = self.tools.get(name)
        if not t or t["server"] not in self._sessions or self._loop is None:
            return False, f"MCP tool {name} is not connected"
        s = self._sessions[t["server"]]
        try:
            fut = asyncio.run_coroutine_threadsafe(s.call_tool(t["tool"], args or {}), self._loop)
            res = fut.result(CALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return False, f"MCP call failed: {type(exc).__name__}: {str(exc)[:300]}"
        parts = []
        self.last_images = []
        for c in getattr(res, "content", None) or []:
            txt = getattr(c, "text", None)
            if txt is None and getattr(c, "type", "") == "image" and getattr(c, "data", None):
                saved = self._save_image(c)      # 그림 결과: 저장해 두고 모델에게 그림으로 보여 준다
                if saved:
                    parts.append(f"[image {saved} — attached right after these tool results]")
                    continue
            parts.append(txt if txt is not None else f"[{getattr(c, 'type', 'content')} not shown]")
        err = bool(getattr(res, "is_error", None) or getattr(res, "isError", None))
        out = "\n".join(parts) or "(no output)"
        return not err, out[:OUT_MAX]

    def _save_image(self, c) -> str:
        import base64
        import time as _t
        mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None) or "image/png"
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime)
        if not ext:
            return ""
        try:
            data = base64.b64decode(c.data)
            d = self.ws / ".cap" / "images"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"mcp-{_t.strftime('%Y%m%d-%H%M%S')}-{len(self.last_images)}{ext}"
            p.write_bytes(data)
        except (ValueError, OSError):
            return ""
        rel = p.relative_to(self.ws).as_posix()
        self.last_images.append((rel, str(p.resolve())))
        return rel

    def describe(self) -> list[str]:
        out = []
        for name in self.configs:
            if name in self._sessions:
                tools = [t["tool"] for t in self.tools.values() if t["server"] == name]
                out.append(f"✓ {name}: {len(tools)} tools — {', '.join(tools)[:120]}")
                if self.resources.get(name):
                    out.append(f"    {len(self.resources[name])} resources (the model reads them with read_mcp_resource)")
                pr = [k for k, v in self.prompts.items() if v["server"] == name]
                if pr:
                    out.append("    prompts: " + "  ".join("/" + k for k in pr)[:300])
            else:
                out.append(f"✗ {name}: {self.errors.get(name, 'not connected')}")
        if "*" in self.errors:
            out.append("✗ " + self.errors["*"])
        if self.untrusted:
            out.append("Project MCP servers (.cap/mcp.json, .mcp.json) are NOT trusted yet — run /mcp trust.")
        off = [f"{n} ({src})" for n, (src, _) in self.foreign.items() if n not in self.configs]
        if off:
            out.append("Available but off (from your other agents): " + ", ".join(off)[:400])
            out.append("Turn one on with /mcp enable <name> (then /clear). Each server's tools take context space.")
        return out or ["No MCP servers. Add them in .cap/mcp.json, .mcp.json or ~/.cap/mcp.json under \"mcpServers\"."]

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
            if self._thread is not None:
                self._thread.join(5)
