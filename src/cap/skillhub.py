"""스킬 찾아 설치하기 (Hermes 의 스킬 허브, Codex 의 skill-installer 와 같은 역할).

검색 출처:
- GitHub 의 공개 스킬 저장소: anthropics/skills, openai/skills (+ ~/.cap/settings.json "skill_sources": ["owner/repo"])
  — 공식 GitHub API(트리 1번)와 raw 파일로 읽는다. 색인은 ~/.cap/cache/skillhub.json 에 하루 동안 둔다.
- 이 컴퓨터에 받아 둔 Claude Code·Codex 마켓플레이스 사본(오프라인).

설치: 스킬 폴더의 파일을 작업 폴더 .cap/skills/<이름>/(또는 ~/.cap/skills) 에 내려받는다.
스킬 본문은 다음 세션의 지시문이 되므로 설치는 늘 사람에게 묻고, 받은 스크립트는 실행하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Hermes 의 기본 탭과 같은 공개 스킬 저장소. 신뢰: official(만든 회사) > org(알려진 단체) > community
DEFAULT_SOURCES = ("anthropics/skills", "openai/skills", "huggingface/skills", "NVIDIA/skills",
                   "K-Dense-AI/scientific-agent-skills", "garrytan/gstack")
TRUST = {"anthropics/skills": "official", "openai/skills": "official", "huggingface/skills": "org", "NVIDIA/skills": "org"}
TRUST_RANK = {"official": 3, "local": 3, "org": 2, "community": 1}
from . import __version__  # noqa: E402

UA = f"CAP-coding-agent/{__version__} (+https://github.com/juyoung020/cap-agent)"
CACHE_SECONDS = 24 * 3600
MAX_FILES, MAX_BYTES = 100, 3_000_000


def _cache_file() -> Path:
    from .compat import cap_home
    return cap_home() / "cache" / "skillhub.json"


def sources() -> list[str]:
    from .compat import settings
    extra = settings().get("skill_sources") or []
    return list(DEFAULT_SOURCES) + [s for s in extra if isinstance(s, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", s)]


def _headers() -> dict:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _front(text: str) -> dict:
    from .compat import front
    return front(text)[0]


def _github_index(repo: str) -> list[dict]:
    import httpx
    info = httpx.get(f"https://api.github.com/repos/{repo}", headers=_headers(), timeout=20)
    info.raise_for_status()
    branch = info.json().get("default_branch", "main")
    tree = httpx.get(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1", headers=_headers(), timeout=30)
    tree.raise_for_status()
    paths = [x["path"] for x in tree.json().get("tree", []) if x.get("type") == "blob"]
    skill_dirs = [p[:-len("/SKILL.md")] for p in paths if p.endswith("/SKILL.md")]

    def one(d: str) -> dict | None:
        try:
            r = httpx.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{d}/SKILL.md", headers={"User-Agent": UA},
                          timeout=20)
            meta = _front(r.text) if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            meta = {}
        files = [p for p in paths if p.startswith(d + "/")]
        return {"id": f"{repo}:{d}", "name": meta.get("name") or d.rsplit("/", 1)[-1],
                "description": " ".join(meta.get("description", "").split())[:300], "source": f"github {repo}",
                "files": files[:MAX_FILES], "branch": branch, "trust": TRUST.get(repo, "community")}
    with ThreadPoolExecutor(8) as ex:
        return [x for x in ex.map(one, skill_dirs) if x]


def _local_index() -> list[dict]:
    from .compat import claude_home, codex_home
    out, seen = [], set()
    for root, label in ((claude_home() / "plugins" / "marketplaces", "local Claude Code marketplace"),
                        (codex_home() / "plugins" / "cache", "local Codex plugin cache")):
        if not root.is_dir():
            continue
        for f in list(root.glob("**/SKILL.md"))[:500]:
            if any(x in ("node_modules", ".git") for x in f.parts):
                continue
            meta = _front(f.read_text(encoding="utf-8", errors="replace"))
            name = meta.get("name") or f.parent.name
            if not meta.get("description") or name in seen:
                continue
            seen.add(name)
            out.append({"id": f"local:{f.parent.resolve().as_posix()}", "name": name,
                        "description": " ".join(meta["description"].split())[:300], "source": label, "trust": "local"})
    return out


def index(refresh: bool = False) -> tuple[list[dict], list[str]]:
    """(스킬 목록, 경고). GitHub 색인은 캐시, 로컬은 매번."""
    notes: list[str] = []
    cf = _cache_file()
    cached: dict = {}
    try:
        cached = json.loads(cf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    remote: list[dict] = []
    for repo in sources():
        ent = cached.get(repo)
        if ent and not refresh and time.time() - ent.get("at", 0) < CACHE_SECONDS:
            remote += ent["skills"]
            continue
        try:
            skills = _github_index(repo)
            cached[repo] = {"at": time.time(), "skills": skills}
            remote += skills
        except Exception as exc:  # noqa: BLE001 — 네트워크가 없거나 막히면 로컬과 지난 캐시로
            notes.append(f"could not reach github {repo} ({type(exc).__name__})")
            if ent:
                remote += ent["skills"]
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return _local_index() + remote, notes



def _hit(term: str, text: str) -> bool:
    """낱말의 시작이 맞을 때만(짧은 'pr' 이 'practices' 에 걸리지 않게, 'commit' 은 'commits' 에 걸리게)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(term), text) is not None

def search(query: str, limit: int = 8) -> tuple[bool, str]:
    terms = [t for t in re.findall(r"[\w-]+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return False, "give a few words, e.g. 'pdf forms' or 'react testing'"
    items, notes = index()
    scored = []
    for it in items:
        name, desc = it["name"].lower(), it["description"].lower()
        score = sum(3 * _hit(t, name) + _hit(t, desc) for t in terms)
        matched = sum(1 for t in terms if _hit(t, name) or _hit(t, desc))
        if score and matched * 2 >= len(terms):          # 검색어 낱말의 절반 이상이 맞는 것만
            scored.append((score * 10 + TRUST_RANK.get(it.get("trust", "community"), 1), it))
    scored.sort(key=lambda x: -x[0])
    lines = [f"- {it['id']}\n  {it['name']}: {it['description'][:200]} ({it['source']}, {it.get('trust', 'community')})"
             for _, it in scored[:limit]]
    head = f"{len(scored)} matching skills" + (f" (shown {limit})" if len(scored) > limit else "")
    tail = ("\nInstall one with skill_install(id=...); the user will be asked. Already installed skills: use skill(name)."
            if lines else "")
    note = ("\n(" + "; ".join(notes) + ")") if notes else ""
    return bool(lines), (head + ":\n" + "\n".join(lines) + tail + note) if lines else f"no skills match '{query}'{note}"


def install(ws: Path, skill_id: str, scope: str = "project", force: bool = False) -> tuple[bool, str]:
    from .compat import cap_home
    from .policy import is_secret_path
    items, _ = index()
    it = next((x for x in items if x["id"] == skill_id), None)
    if not it:
        return False, f"unknown skill id {skill_id} — search first with skill_search"
    name = re.sub(r"[^a-z0-9-]+", "-", it["name"].lower()).strip("-")[:50] or "skill"
    dest = (cap_home() / "skills" / name) if scope == "user" else (ws / ".cap" / "skills" / name)
    files: dict[str, bytes] = {}
    if skill_id.startswith("local:"):
        src = Path(skill_id[len("local:"):])
        for f in sorted(src.rglob("*"))[:MAX_FILES]:
            if f.is_file() and not is_secret_path(f.name):
                files[f.relative_to(src).as_posix()] = f.read_bytes()
    else:
        import httpx
        repo, d = skill_id.split(":", 1)
        for path in it.get("files", [])[:MAX_FILES]:
            rel = path[len(d) + 1:]
            if is_secret_path(Path(rel).name):
                continue
            r = httpx.get(f"https://raw.githubusercontent.com/{repo}/{it.get('branch', 'main')}/{path}",
                          headers={"User-Agent": UA}, timeout=30)
            if r.status_code != 200:
                return False, f"download failed for {path} (HTTP {r.status_code}); nothing installed"
            files[rel] = r.content
    if "SKILL.md" not in files:
        return False, "the skill has no SKILL.md; nothing installed"
    if sum(len(b) for b in files.values()) > MAX_BYTES:
        return False, f"skill is larger than {MAX_BYTES // 1_000_000} MB; nothing installed"
    import shutil
    import tempfile

    from .scan import scan_dir, verdict
    quarantine = Path(tempfile.mkdtemp(prefix="cap_skill_q_"))     # 먼저 격리 폴더에 받아 검사한다
    try:
        for rel, data in files.items():
            q = (quarantine / rel).resolve()
            if quarantine.resolve() not in q.parents:
                continue                                  # 경로 탈출 방지
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(data)
        ok, report = verdict(scan_dir(quarantine))
        if not ok and not force:
            return False, f"{report}\n(Skill {name} was not installed. A person can review it and run "\
                          f"`cap skill install {skill_id} --force`.)"
        if dest.exists():
            _rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(quarantine, dest)
        (dest / ".cap-source.json").write_text(json.dumps({"id": skill_id, "source": it.get("source"),
                                                           "trust": it.get("trust"), "installed": time.time(),
                                                           "check": report.splitlines()[0]}), encoding="utf-8")
    finally:
        _rmtree(quarantine)
    scripts = [r for r in files if r.endswith((".py", ".sh", ".js", ".ps1", ".cmd", ".bat"))]
    return True, (f"installed skill {name} ({len(files)} files) → {dest}. Open it with skill(name=\"{name}\")."
                  + (f" It contains scripts ({', '.join(scripts[:5])}); they were not run." if scripts else "")
                  + "\n" + report)


SEARCH_SCHEMA = {"type": "function", "function": {
    "name": "skill_search",
    "description": ("Search public skill libraries (Anthropic and OpenAI skill repos, plus marketplaces on this "
                    "computer) for a ready-made step-by-step skill, e.g. 'pdf forms', 'excel charts', 'mcp server'. "
                    "Use it when the task needs know-how no installed skill covers."),
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
INSTALL_SCHEMA = {"type": "function", "function": {
    "name": "skill_install",
    "description": "Install a skill found with skill_search (by its id) into this project. The user will be asked.",
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string"},
        "scope": {"type": "string", "enum": ["project", "user"]}}, "required": ["id"]}}}


def _rmtree(path) -> None:
    from .store import rmtree
    rmtree(path)
