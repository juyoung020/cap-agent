""".cap/ 읽기·쓰기, 장부(JSONL), 그림자 git 스냅샷."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

CAP_DIR = ".cap"
# 스냅샷에서 빼는 것: 비밀(Hermes 07 §9-3 기본 제외), 바이너리·데이터·모델 파일, 10MB 넘는 파일
SNAPSHOT_EXCLUDE = ["*.pyc", "*.egg-info/", ".env", ".env.*", "!.env.example", "*.pem", "id_rsa*", "id_ed25519*",
                    "*.zip", "*.tar", "*.gz", "*.tgz", "*.7z", "*.rar", "*.iso", "*.mp4", "*.mov", "*.avi", "*.mkv",
                    "*.mp3", "*.wav", "*.flac", "*.so", "*.dll", "*.dylib", "*.exe", "*.bin", "*.o", "*.a", "*.lib",
                    "*.pt", "*.pth", "*.ckpt", "*.onnx", "*.safetensors", "*.gguf", "*.npz", "*.npy", "*.parquet",
                    "*.h5", "*.hdf5", "*.sqlite", "*.sqlite3", "*.db"]
SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024
SNAPSHOT_MAX_FILES = 50_000
IGNORE_DIRS = {".cap", ".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "build", "dist", ".idea", ".vscode"}


def find_ws(start: Path | None = None) -> Path:
    """`.cap/` 가 있는 가장 가까운 폴더(없으면 현재 폴더). 홈의 `~/.cap` 은 전역 설정 폴더라 작업 폴더로 치지 않는다."""
    p = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    top = _git_top(p)          # git 저장소 안이면 그 뿌리 위로는 올라가지 않는다(Codex 와 같이 저장소 = 프로젝트 경계)
    for q in [p, *p.parents]:
        if q == home and q != p:
            continue
        if (q / CAP_DIR).is_dir():
            return q
        if top is not None and q == top:
            break
    return p


def _git_top(p: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return Path(r.stdout.strip()).resolve()
    except OSError:
        return None



def rmtree(path) -> None:
    """폴더 지우기. Windows 에서 git 개체는 읽기 전용이라 그냥 지우면 남는다 — 쓰기 권한을 주고 다시 지운다."""
    import shutil
    import stat

    def unlock(fn, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            if os.name == "nt":          # nul·con 같은 예약 이름(모델 명령의 '> nul' 이 만든 파일)은 \\?\ 경로로만 지워진다
                try:
                    fn("\\\\?\\" + (p if os.path.isabs(p) else os.path.join(os.getcwd(), p)))   # abspath 는 nul 을 장치로 바꾼다
                except OSError:
                    pass
    shutil.rmtree(path, onerror=unlock)

def cap_dir(ws: Path) -> Path:
    return ws / CAP_DIR


def now_id() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_files(ws: Path) -> list[Path] | None:
    """git 저장소면 추적 중 + 새 파일 중 .gitignore 에 걸리지 않은 것(rg·Claude Code·Codex 와 같이). 아니면 None."""
    try:
        r = subprocess.run(["git", "-C", str(ws), "ls-files", "-z", "-co", "--exclude-standard"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = []
    for name in r.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        parts = name.split("/")
        if any(p in IGNORE_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        p = ws / name
        if p.is_file():                    # 지웠지만 아직 색인에 있는 파일은 뺀다
            out.append(p)
    return out or None                     # 작업 폴더째 무시되는 곳(상위 저장소의 .gitignore)이면 그냥 훑는다


def iter_files(ws: Path):
    """작업 폴더의 파일. git 저장소면 .gitignore 를 따르고, 아니면 흔한 생성물 폴더(IGNORE_DIRS)만 건너뛴다."""
    got = _git_files(ws)
    if got is not None:
        yield from got
        return
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.endswith(".egg-info")]
        for f in files:
            yield Path(root) / f


def file_hash(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def rel(ws: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        return str(p)


class Ledger:
    """모든 행동을 JSONL 한 줄씩. 최종 보고의 '통과'는 여기서만 읽는다."""

    def __init__(self, ws: Path, run_id: str):
        self.ws = ws
        self.run_id = run_id
        self.path = cap_dir(ws) / "runs" / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.node = ""

    def log(self, type_: str, **data: Any) -> dict:
        rec = {"ts": _dt.datetime.now().isoformat(timespec="milliseconds"), "run": self.run_id,
               "node": data.pop("node", self.node), "type": type_, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out


def read_ledger(ws: Path, run_id: str) -> list[dict]:
    return Ledger(ws, run_id).read()


# ---------------------------------------------------------------- 그림자 git

class Shadow:
    """작업 폴더 스냅샷: GIT_DIR=.cap/shadow.git, GIT_WORK_TREE=ws. 사용자 git 과 섞이지 않음."""

    def __init__(self, ws: Path):
        self.ws = ws
        self.git_dir = cap_dir(ws) / "shadow.git"

    def _env(self) -> dict:
        env = dict(os.environ)
        env["GIT_DIR"] = str(self.git_dir)
        env["GIT_WORK_TREE"] = str(self.ws)
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "cap"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "cap@localhost"
        return env

    def _git(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """그림자 git 은 시간 제한을 둔다(큰 폴더에서 에이전트 전체가 멈추지 않게, Hermes 07 §9-3)."""
        try:
            return subprocess.run(["git", *args], cwd=self.ws, env=self._env(), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["git", *args], 124, "", f"git {args[0]} timed out after {timeout}s")

    def _write_exclude(self, large: list[str]) -> None:
        excl = self.git_dir / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join([*(f"{d}/" for d in sorted(IGNORE_DIRS)), *SNAPSHOT_EXCLUDE, *(f"/{p}" for p in large)]) + "\n"
        if not excl.is_file() or excl.read_text(encoding="utf-8") != text:
            excl.write_text(text, encoding="utf-8")

    def _scan(self) -> tuple[list[str], int]:
        """10MB 넘는 파일 목록과 파일 수(5만 개에서 멈춤)."""
        large, n = [], 0
        for root, dirs, files in os.walk(self.ws):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.endswith(".egg-info")]
            for f in files:
                n += 1
                if n > SNAPSHOT_MAX_FILES:
                    return large, n
                p = Path(root) / f
                try:
                    if p.stat().st_size > SNAPSHOT_MAX_BYTES:
                        large.append(p.relative_to(self.ws).as_posix())
                except OSError:
                    continue
        return large, n

    def ensure(self) -> bool:
        try:
            if not (self.git_dir / "HEAD").exists():
                self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                r = self._git("init", "-q")
                if r.returncode != 0:
                    return False
                self._write_exclude([])
            return True
        except FileNotFoundError:
            return False

    def snapshot(self, label: str) -> str | None:
        """작업 폴더 스냅샷. 비밀·바이너리·10MB 넘는 파일은 빼고, 파일이 5만 개를 넘는 폴더는 찍지 않는다."""
        if not self.ensure():
            return None
        large, n = self._scan()
        if n > SNAPSHOT_MAX_FILES:
            return None
        self._write_exclude(large)
        if self._git("add", "-A", timeout=60).returncode != 0:
            return None
        r = self._git("commit", "-q", "--allow-empty", "-m", label)
        if r.returncode != 0:
            return None
        h = self._git("rev-parse", "HEAD")
        return h.stdout.strip() or None

    def restore(self, sha: str) -> bool:
        if not self.ensure():
            return False
        # 색인을 스냅샷 트리로 바꾸고 → 파일을 꺼내고 → 색인에 없는(스냅샷 뒤에 생긴) 파일을 지운다
        r = self._git("read-tree", sha)
        if r.returncode != 0:
            return False
        r = self._git("checkout-index", "-a", "-f")
        self._git("clean", "-fdq")
        return r.returncode == 0

    def restore_files(self, sha: str, files: list[str]) -> list[str]:
        """주어진 파일만 스냅샷 때 내용으로 되돌린다(그때 없던 파일은 지운다). 나머지 작업 폴더는 건드리지 않는다."""
        done = []
        for f in files:
            p = self.ws / f
            r = subprocess.run(["git", "show", f"{sha}:{f}"], cwd=self.ws, env=self._env(), capture_output=True,
                               timeout=30)
            try:
                if r.returncode == 0:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(r.stdout)
                elif p.exists():
                    p.unlink()
                done.append(f)
            except OSError:
                continue
        return done

    def changed_files(self, since: str) -> list[str]:
        self._git("add", "-A")
        r = self._git("diff", "--name-only", "--cached", since)
        return [x for x in r.stdout.splitlines() if x.strip()]

    def archive(self, sha: str) -> bytes | None:
        """스냅샷 트리를 tar 바이트로(변경 전 테스트 상태를 임시 폴더에서 재려고)."""
        if not self.ensure():
            return None
        r = subprocess.run(["git", "archive", "--format=tar", sha], cwd=self.ws, env=self._env(),
                           capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def diff(self, sha: str, stat: bool = False) -> str:
        """스냅샷 이후 작업 폴더 변경(새 파일 포함, Hermes 07 §9-1 /diff)."""
        if not self.ensure():
            return ""
        self._git("add", "-A")
        r = self._git("diff", "--cached", "--stat" if stat else "--no-color", sha)
        return r.stdout

    def diff_files(self, sha: str, files: list[str]) -> str:
        """스냅샷 이후 몇몇 파일만의 변경."""
        if not files or not self.ensure():
            return ""
        self._git("add", "-A")
        return self._git("diff", "--cached", "--no-color", "-U2", sha, "--", *files).stdout

    def show(self, sha: str, path: str) -> str | None:
        r = self._git("show", f"{sha}:{path}")
        return r.stdout if r.returncode == 0 else None
