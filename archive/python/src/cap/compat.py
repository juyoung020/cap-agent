"""Claude Code·Codex 와 같은 구조를 그대로 읽는다 — 스킬·명령·하위 에이전트·훅·MCP·플러그인.

한 곳에서 "어디를 볼지" 정한다. 각 기능(memory·extras·agents·hooks·mcp_client)은 여기의 목록을 쓴다.

| 무엇 | CAP | Claude Code | Codex |
|---|---|---|---|
| 지침 | CAP.md · CAP.local.md · ~/.cap/CAP.md · AGENTS.md · CLAUDE.md · .cap/instructions.md | CLAUDE.md | AGENTS.md |
| 스킬 | .cap/skills, ~/.cap/skills | .claude/skills, ~/.claude/skills | .agents/skills, .codex/skills, ~/.codex/skills |
| 명령 | .cap/commands, ~/.cap/commands | .claude/commands, ~/.claude/commands | ~/.codex/prompts |
| 하위 에이전트 | .cap/agents, ~/.cap/agents | .claude/agents, ~/.claude/agents | — |
| 훅 | .cap/hooks.json, ~/.cap/hooks.json | .claude/settings(.local).json "hooks", ~/.claude/settings.json | ~/.codex/hooks.json |
| MCP | .cap/mcp.json, ~/.cap/mcp.json | .mcp.json, ~/.claude.json | ~/.codex/config.toml [mcp_servers] |
| 플러그인 | .cap/plugins/*, ~/.cap/plugins/* | 켜 둔 플러그인(installed_plugins.json + enabledPlugins) | 켜 둔 플러그인(config.toml [plugins]) |

플러그인은 `.cap-plugin/`·`.claude-plugin/`·`.codex-plugin/` 의 plugin.json 중 있는 것을 읽고, 그 안의
skills·commands·agents·hooks·mcpServers(없으면 같은 이름의 기본 폴더·파일)를 쓴다. ${CLAUDE_PLUGIN_ROOT} 는 플러그인 폴더로 바꾼다.

16k 창과 안전 때문에 두 가지는 기본으로 켜지 않는다:
- 남의 MCP 서버(사용자의 Claude·Codex 설정, 플러그인): 목록에만 보이고 `/mcp enable <이름>` 으로 켠다(도구 설명이 창을 채움).
- 남의 훅(사용자의 Claude·Codex 훅, 플러그인 훅): ~/.cap/settings.json 의 "import": {"hooks": true} 로 켠다.
가져오기 전체는 "import": {"claude": false, "codex": false} 또는 환경변수 CAP_IMPORT=none 으로 끈다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_MANIFESTS = (".cap-plugin", ".claude-plugin", ".codex-plugin")
ROOT_VARS = ("${CLAUDE_PLUGIN_ROOT}", "${CODEX_PLUGIN_ROOT}", "${PLUGIN_ROOT}", "${CAP_PLUGIN_ROOT}")


def cap_home() -> Path:
    from .memory import user_home
    return user_home()


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_json() -> Path:
    return claude_home().parent / ".claude.json" if not os.environ.get("CLAUDE_CONFIG_DIR") \
        else claude_home() / ".claude.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_toml(p: Path) -> dict:
    try:
        import tomllib
    except ImportError:          # 3.10
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def settings() -> dict:
    return _read_json(cap_home() / "settings.json")


def apply_profile(name: str) -> tuple[str | None, str]:
    """Codex 의 profiles: ~/.cap/settings.json 의 "profiles": {"이름": {"mode", "sandbox", "provider", "model", "thinking"}}.
    돌려줌: (모드 또는 None, 설명). 샌드박스·모델·생각 모드는 바로 적용한다."""
    prof = (settings().get("profiles") or {}).get(name)
    if not isinstance(prof, dict):
        return None, f"no profile named {name} in ~/.cap/settings.json \"profiles\""
    from . import llm, sandbox
    if prof.get("sandbox"):
        sandbox.STATE["mode"] = str(prof["sandbox"]).lower()
    if prof.get("provider"):
        try:
            llm.use_provider(str(prof["provider"]), str(prof["model"]) if prof.get("model") else None)
        except ValueError as exc:
            return None, str(exc)
    elif prof.get("model"):
        llm.MODEL = str(prof["model"])
    if "thinking" in prof:
        llm.set_thinking(bool(prof["thinking"]))
    return (str(prof["mode"]) if prof.get("mode") else None), \
        "profile " + name + ": " + ", ".join(f"{k}={v}" for k, v in prof.items())


def import_on(what: str) -> bool:
    """what: claude · codex · hooks."""
    if os.environ.get("CAP_IMPORT", "").lower() in ("none", "off", "0"):
        return False
    imp = settings().get("import") or {}
    default = what in ("claude", "codex")
    v = imp.get(what, default) if isinstance(imp, dict) else default
    return bool(v)


# ---------------------------------------------------------------- 플러그인

@dataclass
class Plugin:
    name: str
    root: Path
    origin: str                     # cap · claude · codex
    manifest: dict = field(default_factory=dict)

    def dirs(self, kind: str) -> list[Path]:
        v = self.manifest.get(kind)
        vals = v if isinstance(v, list) else [v] if isinstance(v, str) else []
        out = [(self.root / x).resolve() for x in vals if isinstance(x, str)]
        default = self.root / kind
        if not out and default.is_dir():
            out = [default]
        return [p for p in out if p.is_dir() and _inside(p, self.root)]

    def _component(self, key: str, default: str) -> dict:
        v = self.manifest.get(key)
        if isinstance(v, dict):
            data = v
        else:
            p = (self.root / v) if isinstance(v, str) else (self.root / default)
            if not (p.is_file() and _inside(p, self.root)):
                return {}
            data = _read_json(p)
        return json.loads(_subst(json.dumps(data), self.root))

    def hooks(self) -> dict:
        d = self._component("hooks", "hooks/hooks.json")
        return d.get("hooks", d) if isinstance(d, dict) else {}

    def mcp(self) -> dict:
        d = self._component("mcpServers", ".mcp.json")
        return d.get("mcpServers", d) if isinstance(d, dict) else {}


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _subst(text: str, root: Path) -> str:
    r = root.resolve().as_posix()
    for v in ROOT_VARS:
        text = text.replace(v, r)
    return text


def _manifest(root: Path) -> dict | None:
    for sub in PLUGIN_MANIFESTS:
        d = _read_json(root / sub / "plugin.json")
        if d:
            return d
    return None


def _latest(d: Path) -> Path | None:
    vs = [x for x in d.iterdir() if x.is_dir() and not x.name.startswith(".")] if d.is_dir() else []
    return max(vs, key=lambda x: x.stat().st_mtime) if vs else None


def plugins(ws: Path) -> list[Plugin]:
    out: list[Plugin] = []
    seen: set[str] = set()

    def add(root: Path, origin: str, name: str | None = None) -> None:
        m = _manifest(root)
        if m is None:
            return
        nm = str(m.get("name") or name or root.name)
        if nm in seen:
            return
        seen.add(nm)
        out.append(Plugin(nm, root, origin, m))

    for base in (ws / ".cap" / "plugins", cap_home() / "plugins"):
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir():
                add(d, "cap")
    if import_on("claude"):
        enabled = dict((_read_json(claude_home() / "settings.json").get("enabledPlugins") or {}))
        enabled.update(_read_json(ws / ".claude" / "settings.json").get("enabledPlugins") or {})
        inst = (_read_json(claude_home() / "plugins" / "installed_plugins.json").get("plugins") or {})
        for key, entries in inst.items():
            if enabled.get(key) is not True:
                continue
            for e in entries if isinstance(entries, list) else [entries]:
                proj = e.get("projectPath")
                if proj and Path(proj).resolve() != ws.resolve():
                    continue
                p = Path(e.get("installPath") or "")
                if p.is_dir():
                    add(p, "claude", key.split("@")[0])
                    break
    if import_on("codex"):
        cfg = _read_toml(codex_home() / "config.toml").get("plugins") or {}
        for key, v in cfg.items():
            if not (isinstance(v, dict) and v.get("enabled")) or "@" not in key:
                continue
            name, mkt = key.split("@", 1)
            p = _latest(codex_home() / "plugins" / "cache" / mkt / name)
            if p:
                add(p, "codex", name)
    return out


# ---------------------------------------------------------------- 폴더 목록 (나중 것이 같은 이름을 덮는다: 프로젝트가 이긴다)

def skill_dirs(ws: Path) -> list[tuple[str, Path]]:
    out = [("plugin:" + p.name, d) for p in plugins(ws) for d in p.dirs("skills")]
    if import_on("codex"):
        out.append(("codex", codex_home() / "skills"))
    if import_on("claude"):
        out.append(("claude", claude_home() / "skills"))
    out.append(("user", cap_home() / "skills"))
    out += [("project", ws / ".agents" / "skills"), ("project", ws / ".codex" / "skills"),
            ("project", ws / ".claude" / "skills"), ("project", ws / ".cap" / "skills")]
    return [(s, d) for s, d in out if d.is_dir()]


def command_dirs(ws: Path) -> list[tuple[str, Path]]:
    """(접두어, 폴더). 플러그인 명령은 /플러그인:이름 으로도 부른다."""
    out = [(p.name, d) for p in plugins(ws) for d in p.dirs("commands")]
    if import_on("codex"):
        out.append(("", codex_home() / "prompts"))
    if import_on("claude"):
        out.append(("", claude_home() / "commands"))
    out += [("", cap_home() / "commands"), ("", ws / ".claude" / "commands"), ("", ws / ".cap" / "commands")]
    return [(s, d) for s, d in out if d.is_dir()]


def agent_dirs(ws: Path) -> list[Path]:
    out = [d for p in plugins(ws) for d in p.dirs("agents")]
    if import_on("claude"):
        out.append(claude_home() / "agents")
    out += [cap_home() / "agents", ws / ".claude" / "agents", ws / ".cap" / "agents"]
    return [d for d in out if d.is_dir()]


# ---------------------------------------------------------------- 훅·MCP 출처

def project_hook_files(ws: Path) -> list[Path]:
    """믿어 줘야(/hooks trust) 도는 프로젝트 훅 파일들."""
    return [p for p in (ws / ".cap" / "hooks.json", ws / ".claude" / "settings.json",
                        ws / ".claude" / "settings.local.json") if p.is_file()]


def imported_hooks(ws: Path) -> list[tuple[str, dict]]:
    """사용자의 Claude·Codex 훅과 켜 둔 플러그인 훅 — "import": {"hooks": true} 일 때만."""
    if not import_on("hooks"):
        return []
    out = []
    if import_on("claude"):
        out.append(("claude", _read_json(claude_home() / "settings.json").get("hooks") or {}))
    if import_on("codex"):
        out.append(("codex", _read_json(codex_home() / "hooks.json").get("hooks") or {}))
    out += [("plugin:" + p.name, p.hooks()) for p in plugins(ws) if not _inside(p.root, ws)]
    return [(o, h) for o, h in out if isinstance(h, dict) and h]


def project_plugin_hooks(ws: Path) -> list[tuple[str, dict]]:
    """작업 폴더 안 플러그인(.cap/plugins/*)의 훅 — 저장소에 들어 있는 명령이라 프로젝트 훅처럼 /hooks trust 뒤에만."""
    return [(p.name, h) for p in plugins(ws) if _inside(p.root, ws) and (h := p.hooks())]


def project_mcp_files(ws: Path) -> list[Path]:
    return [p for p in (ws / ".cap" / "mcp.json", ws / ".mcp.json") if p.is_file()]


def _servers_of(d: dict) -> dict:
    s = d.get("mcpServers") or d.get("servers") or {}
    return s if isinstance(s, dict) else {}


def foreign_mcp(ws: Path) -> dict[str, tuple[str, dict]]:
    """이름 → (출처, 설정). 사용자의 Claude·Codex MCP 서버와 플러그인 서버. 기본으로는 꺼져 있다."""
    out: dict[str, tuple[str, dict]] = {}
    if import_on("claude"):
        cj = _read_json(claude_json())
        for n, c in _servers_of(cj).items():
            out[n] = ("Claude Code", c)
        proj = (cj.get("projects") or {})
        for k, v in proj.items():
            try:
                if Path(k).resolve() == ws.resolve():
                    for n, c in _servers_of(v).items():
                        out[n] = ("Claude Code (this folder)", c)
            except OSError:
                continue
    if import_on("codex"):
        for n, c in (_read_toml(codex_home() / "config.toml").get("mcp_servers") or {}).items():
            if isinstance(c, dict):
                cfg = {k: c[k] for k in ("command", "args", "env", "cwd", "url", "type", "headers") if k in c}
                out[n] = ("Codex", cfg)
    for p in plugins(ws):
        for n, c in p.mcp().items():
            out.setdefault(n, (f"plugin {p.name}", c))
    return {n: v for n, v in out.items() if isinstance(v[1], dict) and (v[1].get("command") or v[1].get("url"))}


def enabled_foreign() -> list[str]:
    v = settings().get("mcp_enabled") or []
    return [str(x) for x in v] if isinstance(v, list) else []


def set_foreign_enabled(name: str, on: bool) -> list[str]:
    p = cap_home() / "settings.json"
    d = _read_json(p)
    cur = [x for x in (d.get("mcp_enabled") or []) if x != name]
    if on:
        cur.append(name)
    d["mcp_enabled"] = cur
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


# ---------------------------------------------------------------- 머리말(front matter)

FRONT_RX = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def front(text: str) -> tuple[dict, str]:
    """`---` 머리말 → (키·값, 본문). YAML 의 단순한 key: value 만(목록은 쉼표로 이은 문자열)."""
    m = FRONT_RX.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    key = ""
    for ln in m.group(1).splitlines():
        if re.match(r"^\s+-\s+", ln) and key:                     # 목록 항목
            meta[key] = (meta.get(key, "") + ", " + ln.split("-", 1)[1].strip()).strip(", ")
            continue
        k, sep, v = ln.partition(":")
        if sep and not ln.startswith((" ", "\t")):
            key = k.strip().lower()
            meta[key] = v.strip().strip("'\"")
    return meta, text[m.end():]


# ---------------------------------------------------------------- 플러그인 설치·목록 (cap plugin …, /plugins)

def describe_plugins(ws: Path) -> list[str]:
    out = []
    for p in plugins(ws):
        skills = sum(len(list(d.glob("*/SKILL.md"))) for d in p.dirs("skills"))
        cmds = sum(len(list(d.glob("*.md"))) for d in p.dirs("commands"))
        agents = sum(len(list(d.glob("*.md"))) for d in p.dirs("agents"))
        mcp = len(p.mcp())
        hooks = sum(len(v) for v in p.hooks().values() if isinstance(v, list))
        out.append(f"{p.name:<24} {p.origin:<6} skills {skills} · commands {cmds} · agents {agents} · mcp {mcp}"
                   f" · hooks {hooks}{'' if import_on('hooks') or not hooks else ' (off)'}")
    return out or ["No plugins. Add one with: cap plugin add <folder | git URL>"]


def plugin_add(src: str) -> tuple[bool, str]:
    """폴더(복사) 또는 git 저장소(얕은 clone)를 ~/.cap/plugins/<이름> 에. Claude·Codex·CAP 플러그인 정의 중 하나가 있어야 한다."""
    import shutil
    import subprocess
    import tempfile
    dest_root = cap_home() / "plugins"
    tmp = None
    p = Path(src).expanduser()
    if not p.is_dir():
        url = src if re.match(r"^(https?://|git@)", src) else (
            f"https://github.com/{src}.git" if re.fullmatch(r"[\w.-]+/[\w.-]+", src) else "")
        if not url:
            return False, f"not a folder or git repository: {src}"
        tmp = Path(tempfile.mkdtemp(prefix="cap_plugin_"))
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(tmp / "src")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            _rmtree(tmp)
            return False, "git clone failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200]
        p = tmp / "src"
    m = _manifest(p)
    if m is None:
        if tmp:
            _rmtree(tmp)
        return False, "no plugin manifest (.claude-plugin/plugin.json, .codex-plugin/plugin.json or .cap-plugin/plugin.json)"
    name = re.sub(r"[^\w.-]", "-", str(m.get("name") or p.name))
    dest = dest_root / name
    if dest.exists():
        _rmtree(dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(p, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv"))
    if tmp:
        _rmtree(tmp)
    return True, f"installed plugin {name} → {dest}"


def plugin_remove(name: str) -> tuple[bool, str]:
    import shutil
    d = cap_home() / "plugins" / name
    if not d.is_dir() or not _inside(d, cap_home() / "plugins"):
        return False, f"no CAP plugin named {name} (Claude Code / Codex plugins are managed by those tools)"
    _rmtree(d)
    return True, f"removed plugin {name}"


def _rmtree(path) -> None:
    from .store import rmtree
    rmtree(path)
