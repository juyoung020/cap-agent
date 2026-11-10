"""설치 전 검사 — 스킬·플러그인을 임시 폴더에 받은 뒤, 들이기 전에 본다 (Hermes 의 skills_guard·격리 설치와 같은 생각).

high   = 설치하지 않는다(사람이 CLI 에서 --force 로만): 받은 것을 셸로 바로 실행, 비밀 파일 읽기, 사용자에게 숨기라는 지시,
         이전 지시 무시, 인코딩된 명령 실행, 셸 시작 파일·예약 작업에 심기
medium = 설치하되 경고: 넓은 삭제, 스크립트가 바깥으로 데이터 보내기
info   = 알림: 명령을 실행하는 훅, 띄우는 MCP 서버
정규식이라 문서 속 예시에도 걸릴 수 있다(보수적으로 막는 쪽).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

TEXT_EXT = {".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1", ".cmd", ".bat",
            ".json", ".yaml", ".yml", ".toml", ".rb", ".pl", ""}
MAX_FILE = 200_000

RULES: list[tuple[str, str, re.Pattern]] = [
    ("high", "pipes a download straight into a shell",
     re.compile(r"\b(curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|"
                r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(iex|Invoke-Expression)\b", re.I)),
    ("high", "reads secret files",
     re.compile(r"(\bcat\b|\btype\b|Get-Content|open\(|read_text\(|readFileSync\()[^\n]{0,80}"
                r"(\.env\b|id_rsa|id_ed25519|\.ssh/|credentials\.json|\.netrc|\.pypirc|\.aws/credentials|auth\.json)", re.I)),
    ("high", "tells the agent to hide things from the user",
     re.compile(r"\b(do not|don't|never)\s+(tell|show|mention|inform|reveal to)\s+(the\s+)?user\b", re.I)),
    ("high", "asks to ignore earlier instructions",
     re.compile(r"\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules)\b", re.I)),
    ("high", "runs an encoded command",
     re.compile(r"base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh\b|FromBase64String[^\n]*(iex|Invoke-Expression)|"
                r"\b(exec|eval)\(\s*(base64\.|codecs\.decode|bytes\.fromhex|atob\()", re.I)),
    ("high", "installs itself into shell startup or scheduled tasks",
     re.compile(r">>?\s*~?/?[^\s]*(\.bashrc|\.zshrc|\.bash_profile|\.profile)\b|\bcrontab\s+-|schtasks\s+/create|"
                r"Register-ScheduledTask|Library/LaunchAgents", re.I)),
    ("medium", "deletes broadly",
     re.compile(r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\$HOME|\*)(\s|$)|Remove-Item\s+[^\n]*-Recurse[^\n]*(C:\\\\|\$HOME|~)",
                re.I)),
    ("medium", "sends data out from a script",
     re.compile(r"requests\.(post|put)\(|urllib\.request\.urlopen\([^\n]*data=|\bcurl\b[^\n]*\s(-d|--data|-F|--form)\b|"
                r"fetch\([^\n]*method:\s*['\"]POST", re.I)),
]


@dataclass
class Finding:
    level: str
    what: str
    where: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.what} — {self.where}"


def scan_dir(root: Path, limit_files: int = 400) -> list[Finding]:
    out: list[Finding] = []
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts][:limit_files]
    for f in files:
        rel = f.relative_to(root).as_posix()
        if f.suffix.lower() not in TEXT_EXT or f.stat().st_size > MAX_FILE:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            for level, what, rx in RULES:
                if rx.search(ln):
                    out.append(Finding(level, what, f"{rel}:{i}: {ln.strip()[:120]}"))
    out += _plugin_info(root)
    return out


def _plugin_info(root: Path) -> list[Finding]:
    """플러그인이 실행할 것(훅 명령·MCP 서버)을 알림으로."""
    out = []
    for hp in (root / "hooks" / "hooks.json",):
        try:
            hooks = json.loads(hp.read_text(encoding="utf-8")).get("hooks") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for ev, groups in hooks.items():
            for g in groups if isinstance(groups, list) else []:
                for h in g.get("hooks") or []:
                    if h.get("command"):
                        out.append(Finding("info", f"hook on {ev} runs a command", str(h["command"])[:120]))
    try:
        servers = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        servers = servers.get("mcpServers", servers)
        for n, c in (servers or {}).items():
            if isinstance(c, dict):
                out.append(Finding("info", f"starts MCP server {n}",
                                   (str(c.get("command") or c.get("url") or "") + " " + " ".join(map(str, c.get("args") or [])))[:120]))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return out


def verdict(findings: list[Finding]) -> tuple[bool, str]:
    """(설치해도 되나, 요약)."""
    high = [f for f in findings if f.level == "high"]
    lines = [str(f) for f in sorted(findings, key=lambda f: {"high": 0, "medium": 1, "info": 2}[f.level])[:12]]
    more = f"\n… +{len(findings) - 12} more" if len(findings) > 12 else ""
    summary = ("security check: " + (f"{len(high)} high-risk finding(s) — NOT installed" if high else
                                     f"passed ({len(findings)} note(s))" if findings else "passed")
               + ("\n" + "\n".join(lines) + more if lines else ""))
    return not high, summary
