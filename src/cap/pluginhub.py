"""플러그인 찾아 설치하기 — Claude Code·Codex 의 마켓플레이스 카탈로그를 그대로 읽는다.

마켓플레이스(= 플러그인 목록을 담은 저장소·폴더):
- Claude Code: ~/.claude/plugins/known_marketplaces.json 의 것(받아 둔 사본이 있으면 그 파일을 읽음)
- Codex: ~/.codex/config.toml 의 [marketplaces.*] (local 폴더 또는 git)
- CAP: ~/.cap/settings.json 의 "plugin_marketplaces": ["owner/repo" 또는 폴더]
- 기본: anthropics/claude-plugins-official
카탈로그 파일: .claude-plugin/marketplace.json (Claude) 또는 .agents/plugins/marketplace.json (Codex).
플러그인 출처: 저장소 안 상대 경로("./plugins/x" 또는 {"source":"local","path":...}), {"source":"url","url",sha},
{"source":"git-subdir","url","path","ref","sha"}, {"source":"github","repo"}.

설치: 임시 폴더에 받아 scan.py 로 검사 → 통과하면 ~/.cap/plugins/<이름> 에. 출처·커밋·검사 결과를 .cap-source.json 에 남긴다.
플러그인은 훅·MCP 서버로 명령을 실행할 수 있어 모델이 부르는 설치(plugin_install)는 늘 사람에게 묻는다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_MARKETS = ("anthropics/claude-plugins-official",)
CATALOG_PATHS = (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")
CACHE_SECONDS = 24 * 3600
from . import __version__  # noqa: E402

UA = f"CAP-coding-agent/{__version__} (+https://github.com/juyoung020/cap-agent)"


def _cache_file() -> Path:
    from .compat import cap_home
    return cap_home() / "cache" / "pluginhub.json"


def _gh_repo(url: str) -> str:
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


def markets() -> list[dict]:
    """[{name, local: Path|None, repo: 'owner/repo'|''}]"""
    from .compat import _read_json, _read_toml, claude_home, codex_home, import_on, settings
    out: dict[str, dict] = {}
    if import_on("claude"):
        for name, v in (_read_json(claude_home() / "plugins" / "known_marketplaces.json") or {}).items():
            src = (v or {}).get("source") or {}
            loc = Path(v.get("installLocation") or "") if v.get("installLocation") else None
            out[name] = {"name": name, "local": loc if loc and loc.is_dir() else None,
                         "repo": src.get("repo") or _gh_repo(src.get("url", ""))}
    if import_on("codex"):
        for name, v in (_read_toml(codex_home() / "config.toml").get("marketplaces") or {}).items():
            if not isinstance(v, dict) or name in out:
                continue
            src = str(v.get("source") or "")
            if v.get("source_type") == "local":
                p = Path(src.replace("\\\\?\\", "").replace("\\?\\", ""))
                out[name] = {"name": name, "local": p if p.is_dir() else None, "repo": ""}
            else:
                out[name] = {"name": name, "local": None, "repo": _gh_repo(src)}
    for m in list(DEFAULT_MARKETS) + [x for x in (settings().get("plugin_marketplaces") or []) if isinstance(x, str)]:
        p = Path(m).expanduser()
        if p.is_dir():
            out.setdefault(p.name, {"name": p.name, "local": p, "repo": ""})
        elif re.fullmatch(r"[\w.-]+/[\w.-]+", m) and not any(v["repo"] == m for v in out.values()):
            out[m.split("/")[1]] = {"name": m.split("/")[1], "local": None, "repo": m}
    return [v for v in out.values() if v["local"] or v["repo"]]


def _remote_catalog(repo: str) -> tuple[dict, str]:
    import httpx
    for branch in ("main", "master"):
        for cp in CATALOG_PATHS:
            r = httpx.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{cp}", headers={"User-Agent": UA},
                          timeout=20, follow_redirects=True)
            if r.status_code == 200:
                return r.json(), branch
    return {}, ""


def _local_catalog(root: Path) -> dict:
    for cp in CATALOG_PATHS:
        try:
            return json.loads((root / cp).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def index(refresh: bool = False) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    try:
        cache = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    items: list[dict] = []
    for m in markets():
        cat, branch = {}, ""
        if m["local"]:
            cat = _local_catalog(m["local"])
        if not cat and m["repo"]:
            ent = cache.get(m["repo"])
            if ent and not refresh and time.time() - ent.get("at", 0) < CACHE_SECONDS:
                cat, branch = ent["catalog"], ent.get("branch", "main")
            else:
                try:
                    cat, branch = _remote_catalog(m["repo"])
                    cache[m["repo"]] = {"at": time.time(), "catalog": cat, "branch": branch}
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"could not read marketplace {m['repo']} ({type(exc).__name__})")
                    if ent:
                        cat, branch = ent["catalog"], ent.get("branch", "main")
        for p in (cat.get("plugins") or []) if isinstance(cat, dict) else []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            desc = p.get("description") or ""
            src = p.get("source")
            if not desc and m["local"] and isinstance(src, (str, dict)):
                rel = src if isinstance(src, str) else src.get("path", "")
                if rel:
                    from .compat import _manifest
                    desc = ((_manifest(m["local"] / rel) or {}).get("description") or "")
            items.append({"id": f"{p['name']}@{m['name']}", "name": p["name"],
                          "description": " ".join(str(desc).split())[:300], "category": p.get("category", ""),
                          "market": m["name"], "source": src, "market_local": str(m["local"] or ""),
                          "market_repo": m["repo"], "branch": branch or "main"})
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return items, notes



def _hit(term: str, text: str) -> bool:
    """낱말의 시작이 맞을 때만(짧은 'pr' 이 'practices' 에 걸리지 않게, 'commit' 은 'commits' 에 걸리게)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(term), text) is not None

def search(query: str, limit: int = 8) -> tuple[bool, str]:
    terms = [t for t in re.findall(r"[\w-]+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return False, "give a few words, e.g. 'commit git' or 'security review'"
    items, notes = index()
    scored = []
    for it in items:
        name, desc = it["name"].lower(), (it["description"] + " " + it["category"]).lower()
        score = sum(3 * _hit(t, name) + _hit(t, desc) for t in terms)
        matched = sum(1 for t in terms if _hit(t, name) or _hit(t, desc))
        if score and matched * 2 >= len(terms):          # 검색어 낱말의 절반 이상이 맞는 것만
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    lines = [f"- {it['id']}\n  {it['description'][:200] or '(no description)'}" for _, it in scored[:limit]]
    note = ("\n(" + "; ".join(notes) + ")") if notes else ""
    if not lines:
        return False, f"no plugins match '{query}' in {len({i['market'] for i in items})} marketplaces{note}"
    return True, (f"{len(scored)} matching plugins" + (f" (shown {limit})" if len(scored) > limit else "") + ":\n"
                  + "\n".join(lines) + "\nInstall one with plugin_install(id=...); the user will be asked." + note)


def _git(*a: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def _fetch(url: str, ref: str, subpath: str, into: Path) -> tuple[bool, str]:
    """url 저장소의 ref(커밋·태그·브랜치)에서 subpath(없으면 전체)를 into 로. 얕게, 필요한 경로만."""
    repo = into.parent / "repo"
    if _git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, str(repo)).returncode != 0:
        return False, f"git clone failed: {url}"
    if ref:
        r = _git("fetch", "--depth", "1", "origin", ref, cwd=repo)
        target = "FETCH_HEAD" if r.returncode == 0 else "HEAD"
    else:
        target = "HEAD"
    if subpath:
        _git("sparse-checkout", "set", "--no-cone", subpath.strip("./") + "/", cwd=repo)
    r = _git("checkout", "-q", target, cwd=repo)
    if r.returncode != 0:
        return False, "git checkout failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:160]
    src = repo / subpath.strip("./") if subpath else repo
    if not src.is_dir():
        return False, f"path {subpath} not found in {url}"
    shutil.copytree(src, into, ignore=shutil.ignore_patterns(".git"))
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return True, sha


def contents(root: Path, name: str) -> str:
    """설치한 플러그인이 더하는 것: 명령·스킬·에이전트·MCP 서버 이름(모델이 따로 찾지 않아도 되게)."""
    from .compat import Plugin, _manifest
    pl = Plugin(name, root, "cap", _manifest(root) or {})
    cmds = sorted(f"/{name}:{f.stem}" for d in pl.dirs("commands") for f in d.glob("*.md"))
    skills = sorted(f.parent.name for d in pl.dirs("skills") for f in d.glob("*/SKILL.md"))
    agents = sorted(f.stem for d in pl.dirs("agents") for f in d.glob("*.md"))
    parts = [f"commands: {', '.join(cmds)} (also without the '{name}:' prefix if no other command has that name)"
             if cmds else "", f"skills: {', '.join(skills)}" if skills else "",
             f"agents: {', '.join(agents)}" if agents else "",
             f"MCP servers (off until /mcp enable): {', '.join(pl.mcp())}" if pl.mcp() else ""]
    return "Adds — " + "; ".join(x for x in parts if x) if any(parts) else "Adds nothing CAP can use."


def install(plugin_id: str, force: bool = False) -> tuple[bool, str]:
    from .compat import _manifest, cap_home
    from .scan import scan_dir, verdict
    items, _ = index()
    it = next((x for x in items if x["id"] == plugin_id), None) or \
        next((x for x in items if x["name"] == plugin_id), None)
    if not it:
        return False, f"unknown plugin {plugin_id} — search first with plugin_search"
    tmp = Path(tempfile.mkdtemp(prefix="cap_plugin_q_"))
    got = tmp / "plugin"
    try:
        src = it["source"]
        if isinstance(src, dict) and src.get("source") == "local":
            src = src.get("path", "")
        if isinstance(src, str):                                   # 마켓 저장소 안 상대 경로
            if it["market_local"]:
                d = (Path(it["market_local"]) / src).resolve()
                if not d.is_dir():
                    return False, f"{src} not found in marketplace {it['market']}"
                shutil.copytree(d, got, ignore=shutil.ignore_patterns(".git"))
                sha = "local"
            else:
                ok, sha = _fetch(f"https://github.com/{it['market_repo']}.git", it["branch"], src, got)
                if not ok:
                    return False, sha
        elif isinstance(src, dict) and src.get("source") in ("url", "git-subdir", "github"):
            url = src.get("url") or f"https://github.com/{src.get('repo', '')}.git"
            ok, sha = _fetch(url, src.get("sha") or src.get("ref") or "", src.get("path", ""), got)
            if not ok:
                return False, sha
        else:
            return False, f"unsupported plugin source: {json.dumps(src)[:120]}"
        if _manifest(got) is None:
            (got / ".claude-plugin").mkdir(exist_ok=True)           # 카탈로그만 있고 정의가 없는 플러그인(Claude 는 허용)
            (got / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": it["name"],
                                                                            "description": it["description"]}),
                                                                 encoding="utf-8")
        ok, report = verdict(scan_dir(got))
        if not ok and not force:
            return False, f"{report}\n(Plugin {it['name']} was not installed. A person can review it and run " \
                          f"`cap plugin add {it['id']} --force`.)"
        dest = cap_home() / "plugins" / re.sub(r"[^\w.-]", "-", it["name"])
        if dest.exists():
            _rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(got, dest)
        (dest / ".cap-source.json").write_text(json.dumps({"id": it["id"], "source": it["source"], "commit": sha,
                                                           "installed": time.time(),
                                                           "check": report.splitlines()[0]}), encoding="utf-8")
        return True, f"installed plugin {it['name']} → {dest} (commit {sha[:12]})\n{report}\n" + \
            contents(dest, it["name"]) + "\nThey are available in the next conversation (/clear)."
    finally:
        _rmtree(tmp)


SEARCH_SCHEMA = {"type": "function", "function": {
    "name": "plugin_search",
    "description": ("Search plugin marketplaces (Claude Code / Codex catalogs) for a plugin that adds skills, "
                    "commands, agents or MCP tools, e.g. 'commit pr', 'security review', 'playwright'."),
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
INSTALL_SCHEMA = {"type": "function", "function": {
    "name": "plugin_install",
    "description": "Install a plugin found with plugin_search (id like name@marketplace). The user will be asked.",
    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}


def _rmtree(path) -> None:
    from .store import rmtree
    rmtree(path)
