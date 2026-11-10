"""Windows 격리(cmd.exe) 안에서 쓰는 작은 유닉스 명령 모음 — 파이썬 표준 라이브러리만.

AppContainer 안에서는 Git Bash·PowerShell 이 뜨지 않아 셸이 cmd.exe 다. 작은 모델은 ls·cat·grep 같은 유닉스 명령을
늘 쓰므로, 격리 폴더(.cap/sbx/bin)에 명령마다 .cmd 를 두고(install) 이 파일을 부른다:
    python -I -X utf8 <bin>\\winutils.py <명령> <인자...>
명령: ls cat head tail grep wc find rm cp mv mkdir touch pwd which sed(-n 'A,Bp' 만) diff(-u) dir(cmd 식 /b /s /a).
- 인자: cmd.exe 는 작은따옴표·와일드카드를 모른다. 원래 명령 줄(GetCommandLineW)을 다시 나눠 '…'·"…" 를 따옴표로
  다루고, 따옴표 없는 *.py 같은 인자는 여기서 펼친다(맞는 것이 없으면 그대로 — bash 와 같음).
- 출력은 UTF-8, 종료 코드는 원래 도구처럼(grep: 찾음 0·없음 1·오류 2).
- grep -r·find·dir /s 는 .git·.venv·node_modules·__pycache__·.cap 같은 폴더 안으로 들어가지 않는다(출력이 모델에게 감).
cmd 자체 명령(mkdir·dir)은 .cmd 로 덮지 못하므로 translate() 가 명령 줄에서 cap-mkdir·cap-dir 로 바꾼다
(그 밖에 export X=Y·X=Y cmd → set, /dev/null → 격리 임시 파일, '…' → "…", "; " → "& ").
"""
from __future__ import annotations

import difflib
import fnmatch
import glob as _glob
import os
import re
import shutil
import stat
import subprocess
import sys
import time

NOISE = {".git", ".hg", ".svn", ".cap", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
         ".pytest_cache", ".ruff_cache"}
COMMANDS = ("ls", "cat", "head", "tail", "grep", "wc", "find", "rm", "cp", "mv", "mkdir", "touch", "pwd", "which",
            "sed", "diff", "dir")
SHIM_NAMES = {c: c for c in COMMANDS if c not in ("mkdir", "dir")} | {"cap-mkdir": "mkdir", "cap-dir": "dir"}


class Fail(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


class Arg(str):
    """명령 줄의 인자. quoted: 따옴표로 감싼 것(와일드카드를 펼치지 않는다)."""
    quoted = False


def _arg(s: str, quoted: bool) -> Arg:
    a = Arg(s)
    a.quoted = quoted
    return a


def split_line(s: str) -> list[Arg]:
    """cmd 가 넘긴 인자 줄을 나눈다: 공백으로, '…'·"…" 는 한 덩어리("…" 안의 \\" 는 ")."""
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        buf, quoted = [], False
        while i < n and s[i] not in " \t":
            c = s[i]
            if c in "\"'":
                j, part = i + 1, []
                while j < n and s[j] != c:
                    if c == '"' and s[j] == "\\" and j + 1 < n and s[j + 1] == '"':
                        j += 1
                    part.append(s[j])
                    j += 1
                buf.append("".join(part))
                quoted, i = True, j + 1
            else:
                buf.append(c)
                i += 1
        out.append(_arg("".join(buf), quoted))
    return out


def _raw_args(name: str) -> list[Arg] | None:
    """이 프로세스의 원래 명령 줄에서 '<…winutils.py> <name>' 뒤를 다시 나눈다(Windows 만)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCommandLineW.restype = ctypes.c_wchar_p
        raw = k.GetCommandLineW() or ""
    except (OSError, AttributeError):
        return None
    m = re.search(r"winutils(?:\.py)?\"?\s+(\S+)", raw)
    if not m or m.group(1).strip('"') != name:
        return None
    return split_line(raw[m.end():])


def expand(args: list[str]) -> list[str]:
    """따옴표 없는 와일드카드 인자를 펼친다(맞는 것이 없으면 그대로)."""
    out = []
    for a in args:
        if not getattr(a, "quoted", False) and any(c in a for c in "*?[") and a != "-":
            hits = sorted(_glob.glob(a))
            if hits:
                out += hits
                continue
        out.append(a)
    return out


def parse(cmd: str, args: list[str], flags: str, valued: str = "", longs: dict | None = None):
    """짧은 옵션(-la, -n5, -n 5)과 긴 옵션(--name[=v]). longs: 긴 이름 → 짧은 글자(None 이면 무시)."""
    longs = longs or {}
    opt: dict = {}
    ops: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            ops += args[i + 1:]
            break
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name not in longs:
                raise Fail(f"{cmd}: unsupported option '{a}'", 2)
            key = longs[name]
            if key is None:
                pass
            elif key in valued:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise Fail(f"{cmd}: option '{a}' needs a value", 2)
                    val = args[i]
                opt.setdefault(key, []).append(val)
            else:
                opt[key] = True
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in valued:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        if i >= len(args):
                            raise Fail(f"{cmd}: option '-{c}' needs a value", 2)
                        val = args[i]
                    opt.setdefault(c, []).append(val)
                    break
                if c not in flags:
                    sup = " ".join("-" + f for f in flags + valued)
                    raise Fail(f"{cmd}: unsupported option '-{c}' (supported here: {sup})", 2)
                opt[c] = True
                j += 1
        else:
            ops.append(a)
        i += 1
    return opt, ops


class IO:
    def __init__(self, out=None, err=None, inp=None):
        self.out = out if out is not None else sys.stdout.buffer
        self.err_ = err if err is not None else sys.stderr
        self.inp = inp if inp is not None else sys.stdin.buffer
        self.code = 0

    def w(self, s) -> None:
        self.out.write(s if isinstance(s, bytes) else s.encode("utf-8", "replace"))

    def line(self, s: str) -> None:
        self.w(s + "\n")

    def err(self, msg: str, code: int = 1) -> None:
        self.err_.write(msg + "\n")
        self.code = max(self.code, code)


def _read(path: str, io: IO) -> bytes:
    if path == "-":
        return io.inp.read()
    if os.path.isdir(path):
        raise Fail(f"{path}: Is a directory")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise Fail(f"{path}: No such file or directory") from None
    except OSError as exc:
        raise Fail(f"{path}: {exc.strerror or exc}") from None


def _lines(data: bytes) -> list[bytes]:
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    return out + [parts[-1]] if parts[-1] else out


def _walk(root: str, maxdepth: int | None = None):
    """(보여 줄 경로, 깊이, 폴더인가) — 뿌리 자신부터. 시끄러운 폴더(NOISE) 안으로는 들어가지 않는다."""
    yield root, 0, os.path.isdir(root)
    if not os.path.isdir(root):
        return
    stack = [(root, 1)]
    while stack:
        d, depth = stack.pop()
        if maxdepth is not None and depth > maxdepth:
            continue
        try:
            ents = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        subs = []
        for e in ents:
            p = (d if d.endswith(("/", "\\")) else d + "/") + e.name
            isdir = e.is_dir(follow_symlinks=False)
            yield p, depth, isdir
            if isdir and e.name not in NOISE:
                subs.append((p, depth + 1))
        stack += reversed(subs)


# ---------------------------------------------------------------- 명령
def _mode(st: os.stat_result) -> str:
    return stat.filemode(st.st_mode)


def _size(n: int, human: bool) -> str:
    if not human:
        return str(n)
    for unit in ("", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "" or n >= 10 else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def cmd_ls(args, io):
    o, ops = parse("ls", args, "laA1RtrhpFdS", "", {"all": "a", "almost-all": "A", "recursive": "R",
                                                   "human-readable": "h", "directory": "d", "color": None})
    ops = expand(ops) or ["."]

    def show(path, name):
        suffix = "/" if ("p" in o or "F" in o) and os.path.isdir(path) else ""
        if "l" not in o:
            return name + suffix
        try:
            st = os.stat(path)
        except OSError:
            return name
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        return f"{_mode(st)} {_size(st.st_size, 'h' in o):>8} {t} {name}{suffix}"

    def order(items):        # items: (name, path)
        if "t" in o:
            items.sort(key=lambda x: -os.stat(x[1]).st_mtime if os.path.exists(x[1]) else 0)
        elif "S" in o:
            items.sort(key=lambda x: -os.stat(x[1]).st_size if os.path.exists(x[1]) else 0)
        else:
            items.sort(key=lambda x: x[0].lower())
        return items[::-1] if "r" in o else items

    def listing(d, header):
        if header:
            io.line(f"{d}:")
        try:
            names = os.listdir(d)
        except OSError as exc:
            io.err(f"ls: cannot open directory '{d}': {exc.strerror}", 2)
            return
        if not ("a" in o or "A" in o):
            names = [n for n in names if not n.startswith(".")]
        items = order([(n, os.path.join(d, n)) for n in names])
        if "a" in o:
            items = [(".", d), ("..", os.path.join(d, ".."))] + items
        for n, p in items:
            io.line(show(p, n))
        if "R" in o:
            for n, p in items:
                if n not in (".", "..") and os.path.isdir(p) and not os.path.islink(p):
                    io.line("")
                    listing((d if d.endswith(("/", "\\")) else d + "/") + n, True)

    files, dirs = [], []
    for p in ops:
        if not os.path.lexists(p):
            io.err(f"ls: cannot access '{p}': No such file or directory", 2)
        elif os.path.isdir(p) and "d" not in o:
            dirs.append(p)
        else:
            files.append(p)
    for n, p in order([(f, f) for f in files]):
        io.line(show(p, n))
    for k, d in enumerate(dirs):
        if k or files:
            io.line("")
        listing(d, len(ops) > 1 or "R" in o)
    return io.code


def cmd_cat(args, io):
    o, ops = parse("cat", args, "n")
    num = 0
    for p in expand(ops) or ["-"]:
        try:
            data = _read(p, io)
        except Fail as exc:
            io.err(f"cat: {exc}")
            continue
        if "n" in o:
            for ln in _lines(data):
                num += 1
                io.w(f"{num:6d}\t".encode() + ln)
        else:
            io.w(data)
    return io.code


def _head_tail(name, args, io):
    args = [("-n" + a[1:]) if re.fullmatch(r"-\d+", a) else a for a in args]     # head -5
    o, ops = parse(name, args, "qv", "nc", {"lines": "n", "bytes": "c", "quiet": "q", "silent": "q"})
    raw = (o.get("n") or o.get("c") or ["10"])[-1]
    by_bytes = "c" in o
    m = re.fullmatch(r"([+-]?)(\d+)", raw)
    if not m:
        raise Fail(f"{name}: invalid number of lines: '{raw}'")
    sign, n = m.group(1), int(m.group(2))
    files = expand(ops) or ["-"]
    for k, p in enumerate(files):
        try:
            data = _read(p, io)
        except Fail as exc:
            io.err(f"{name}: {exc}")
            continue
        if len(files) > 1 and "q" not in o:
            io.line(("\n" if k else "") + f"==> {'standard input' if p == '-' else p} <==")
        seq = data if by_bytes else _lines(data)
        if name == "head":
            part = seq[:-n] if sign == "-" else seq[:n]
        else:
            part = seq[n - 1:] if sign == "+" else (seq[-n:] if n else seq[:0])
        io.w(part if by_bytes else b"".join(part))
    return io.code


def cmd_head(args, io):
    return _head_tail("head", args, io)


def cmd_tail(args, io):
    return _head_tail("tail", args, io)


_INC, _EXC, _EXD = "\x01", "\x02", "\x03"      # 긴 이름만 있는 grep 옵션(--include 등)의 열쇠


def _bre(p: str) -> str:
    """기본 정규식(BRE) → 파이썬: \\| \\( \\) \\{ \\} \\+ \\? 이 특수, 맨 글자는 문자 그대로."""
    out, i = [], 0
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            n = p[i + 1]
            out.append(n if n in "|(){}+?" else c + n)
            i += 2
            continue
        out.append("\\" + c if c in "|(){}+?" else c)
        i += 1
    return "".join(out)


def cmd_grep(args, io):
    o, ops = parse("grep", args, "nirRlLvEFPcwohHqsx", "eABCm" + _INC + _EXC + _EXD, {
        "include": _INC, "exclude": _EXC, "exclude-dir": _EXD, "color": None, "colour": None,
        "line-number": "n", "ignore-case": "i", "recursive": "r", "files-with-matches": "l",
        "files-without-match": "L", "invert-match": "v", "extended-regexp": "E", "fixed-strings": "F",
        "perl-regexp": "P", "count": "c", "word-regexp": "w", "only-matching": "o", "no-filename": "h",
        "with-filename": "H", "quiet": "q", "silent": "q", "no-messages": "s", "line-regexp": "x",
        "regexp": "e", "max-count": "m", "context": "C", "after-context": "A", "before-context": "B"})
    pats = o.get("e") or []
    if not pats:
        if not ops:
            raise Fail("usage: grep [-nirlvEFcwohqx] [-A N] [-B N] [-C N] PATTERN [FILE...]", 2)
        pats, ops = [ops[0]], ops[1:]
    conv = []
    for p in pats:
        if "F" in o:
            conv.append(re.escape(p))
        else:
            p = p.replace(r"\<", r"\b").replace(r"\>", r"\b")
            conv.append(p if ("E" in o or "P" in o) else _bre(p))
    rx_s = "|".join(f"(?:{c})" for c in conv)
    if "w" in o:
        rx_s = rf"(?<!\w)(?:{rx_s})(?!\w)"
    if "x" in o:
        rx_s = rf"^(?:{rx_s})$"
    try:
        rx = re.compile(rx_s, re.I if "i" in o else 0)
    except re.error as exc:
        raise Fail(f"grep: invalid regular expression '{pats[0]}': {exc}", 2) from None
    recursive = "r" in o or "R" in o
    files = expand(ops) or (["."] if recursive else ["-"])
    inc, exc_, exd = o.get(_INC, []), o.get(_EXC, []), o.get(_EXD, [])
    after = int((o.get("A") or o.get("C") or ["0"])[-1])
    before = int((o.get("B") or o.get("C") or ["0"])[-1])
    maxc = int(o["m"][-1]) if "m" in o else None
    todo = []
    for f in files:
        if f != "-" and os.path.isdir(f):
            if not recursive:
                if "s" not in o:
                    io.err(f"grep: {f}: Is a directory", 2)
                continue
            for p, _, isdir in _walk(f):
                base = os.path.basename(p)
                if isdir or any(fnmatch.fnmatch(part, d) for d in exd for part in re.split(r"[\\/]", p)):
                    continue
                if (inc and not any(fnmatch.fnmatch(base, g) for g in inc)) or any(fnmatch.fnmatch(base, g)
                                                                                   for g in exc_):
                    continue
                todo.append(p)
        else:
            todo.append(f)
    show_name = "H" in o or ("h" not in o and (len(todo) > 1 or recursive))
    found = False
    for f in todo:
        try:
            data = _read(f, io)
        except Fail as exc:
            if "s" not in o:
                io.err(f"grep: {exc}", 2)
            continue
        name = "(standard input)" if f == "-" else f
        text = data.decode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
        hits = []
        for k, ln in enumerate(lines):
            if bool(rx.search(ln)) != ("v" in o):
                hits.append(k)
                if maxc is not None and len(hits) >= maxc:
                    break
        if hits:
            found = True
        if "q" in o:
            if found:
                return 0
            continue
        if "l" in o or "L" in o:
            if bool(hits) == ("l" in o):
                io.line(name)
            continue
        if "c" in o:
            io.line((f"{name}:" if show_name else "") + str(len(hits)))
            continue
        if hits and b"\0" in data[:8192]:
            io.line(f"Binary file {name} matches")
            continue
        pre = (lambda k, sep: (f"{name}{sep}" if show_name else "") + (f"{k + 1}{sep}" if "n" in o else ""))
        if "o" in o and "v" not in o:
            for k in hits:
                for m in rx.finditer(lines[k]):
                    if m.group(0):
                        io.line(pre(k, ":") + m.group(0))
            continue
        shown, last = set(hits), -2
        ctx = sorted({j for k in hits for j in range(max(0, k - before), min(len(lines), k + after + 1))})
        for j in ctx:
            if (after or before) and last >= 0 and j > last + 1:
                io.line("--")
            io.line(pre(j, ":" if j in shown else "-") + lines[j])
            last = j
    if io.code:
        return io.code
    return 0 if found else 1


def cmd_wc(args, io):
    o, ops = parse("wc", args, "lwcm", "", {"lines": "l", "words": "w", "bytes": "c", "chars": "m"})
    keys = [k for k in "lwmc" if k in o] or ["l", "w", "c"]
    rows, tot = [], [0] * len(keys)
    for p in expand(ops) or ["-"]:
        try:
            data = _read(p, io)
        except Fail as exc:
            io.err(f"wc: {exc}")
            continue
        val = {"l": data.count(b"\n"), "w": len(data.split()), "c": len(data),
               "m": len(data.decode("utf-8", "replace")) if "m" in keys else 0}
        nums = [val[k] for k in keys]
        tot = [a + b for a, b in zip(tot, nums)]
        rows.append((nums, "" if p == "-" else p))
    if len(rows) > 1:
        rows.append((tot, "total"))
    width = max([len(str(n)) for r in rows for n in r[0]] + [1])
    if len(keys) > 1 and any(not r[1] for r in rows):
        width = max(width, 7)                      # 표준 입력에서 여러 칸이면 GNU 처럼 7칸
    for nums, name in rows:
        io.line(" ".join(f"{n:>{width}}" for n in nums) + (f" {name}" if name else ""))
    return io.code


FIND_HELP = "supported here: -name -iname -path -type f|d -maxdepth N -mindepth N -not/! -o -print -delete"


def _windows_find(args) -> bool:
    """cmd 의 find.exe 식(find "text" file, /I /V /C /N)이면 True."""
    if any(a.upper() in ("/I", "/V", "/C", "/N", "/OFF", "/OFFLINE") for a in args):
        return True
    first = args[0] if args else ""
    return (bool(args) and not any(a.startswith("-") for a in args) and not os.path.exists(first)
            and not first.startswith(".") and not any(c in first for c in "/\\"))


def _find_exe_args(args) -> list[str]:
    """find.exe 는 찾을 글자를 "…" 로 받아야 한다(인자를 다시 나누며 벗겨진 따옴표를 되살림)."""
    out, text = [], False
    for a in args:
        if a.startswith("/") and not text:
            out.append(a)
        elif not text:
            out.append('"' + a.replace('"', "") + '"')
            text = True
        else:
            out.append(f'"{a}"' if " " in a else a)
    return out


def cmd_find(args, io):
    if _windows_find(args):
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "find.exe")
        io.out.flush()
        return subprocess.call(f'"{exe}" ' + " ".join(_find_exe_args(args)))
    roots = []
    i = 0
    while i < len(args) and not (args[i].startswith("-") or args[i] in ("!", "(")):
        roots.append(args[i])
        i += 1
    groups, cur, neg = [[]], None, False
    maxd = mind = None
    delete = False
    while i < len(args):
        a = args[i]
        if a in ("!", "-not"):
            neg = not neg
        elif a in ("-o", "-or"):
            groups.append([])
        elif a in ("-a", "-and", "-print"):
            pass
        elif a == "-delete":
            delete = True
        elif a in ("-name", "-iname", "-path", "-wholename", "-type", "-maxdepth", "-mindepth"):
            if i + 1 >= len(args):
                raise Fail(f"find: missing argument to '{a}'")
            v = args[i + 1]
            i += 1
            if a == "-maxdepth":
                maxd = int(v)
            elif a == "-mindepth":
                mind = int(v)
            else:
                if a == "-type" and v not in ("f", "d"):
                    raise Fail(f"find: -type {v} is not supported ({FIND_HELP})")
                groups[-1].append((neg, a, v))
            neg = False
        else:
            raise Fail(f"find: unsupported expression '{a}' ({FIND_HELP})")
        i += 1

    def test(neg, pred, v, path, isdir):
        base = os.path.basename(path.rstrip("/\\")) or path
        if pred == "-name":
            r = fnmatch.fnmatchcase(base, v)
        elif pred == "-iname":
            r = fnmatch.fnmatchcase(base.lower(), v.lower())
        elif pred in ("-path", "-wholename"):
            r = fnmatch.fnmatchcase(path.replace("\\", "/"), v.replace("\\", "/"))
        else:
            r = isdir if v == "d" else not isdir
        return r != neg

    hits = []
    for root in roots or ["."]:
        if not os.path.lexists(root):
            io.err(f"find: '{root}': No such file or directory")
            continue
        for path, depth, isdir in _walk(root, maxd):
            if mind is not None and depth < mind:
                continue
            if any(all(test(*t, path, isdir) for t in g) for g in groups):
                if delete and os.path.basename(path.rstrip("/\\")) in (".", ".."):
                    continue
                if delete:
                    hits.append((path, isdir))
                else:
                    io.line(path)
    for path, isdir in reversed(hits):
        try:
            os.rmdir(path) if isdir else os.remove(path)
        except OSError as exc:
            io.err(f"find: cannot delete '{path}': {exc.strerror}")
    return io.code


def _onerror(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _is_root(p: str) -> bool:
    a = os.path.abspath(p)
    return os.path.dirname(a) == a


def cmd_rm(args, io):
    o, ops = parse("rm", args, "rRfivd", "", {"recursive": "r", "force": "f", "verbose": "v", "dir": "d"})
    ops = expand(ops)
    if not ops and "f" not in o:
        raise Fail("rm: missing operand")
    for p in ops:
        if os.path.basename(p.rstrip("/\\")) in (".", "..") or _is_root(p):
            io.err(f"rm: refusing to remove '{p}'")
            continue
        if not os.path.lexists(p):
            if "f" not in o:
                io.err(f"rm: cannot remove '{p}': No such file or directory")
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                if "r" in o or "R" in o:
                    shutil.rmtree(p, **({"onexc": _onerror} if sys.version_info >= (3, 12) else {"onerror": _onerror}))
                elif "d" in o:
                    os.rmdir(p)
                else:
                    io.err(f"rm: cannot remove '{p}': Is a directory")
                    continue
            else:
                try:
                    os.remove(p)
                except PermissionError:
                    os.chmod(p, stat.S_IWRITE)
                    os.remove(p)
            if "v" in o:
                io.line(f"removed '{p}'")
        except OSError as exc:
            io.err(f"rm: cannot remove '{p}': {exc.strerror or exc}")
    return io.code


def _targets(cmd, ops):
    if len(ops) < 2:
        raise Fail(f"{cmd}: missing destination file operand after '{ops[0]}'" if ops else f"{cmd}: missing file operand")
    *srcs, dst = ops
    if len(srcs) > 1 and not os.path.isdir(dst):
        raise Fail(f"{cmd}: target '{dst}' is not a directory")
    return srcs, dst


def cmd_cp(args, io):
    o, ops = parse("cp", args, "rRafpvn", "", {"recursive": "r", "force": "f", "verbose": "v", "archive": "a"})
    srcs, dst = _targets("cp", expand(ops))
    for s in srcs:
        t = os.path.join(dst, os.path.basename(s.rstrip("/\\"))) if os.path.isdir(dst) else dst
        try:
            if os.path.isdir(s):
                if not ({"r", "R", "a"} & set(o)):
                    io.err(f"cp: -r not specified; omitting directory '{s}'")
                    continue
                if os.path.abspath(t).startswith(os.path.abspath(s) + os.sep):
                    io.err(f"cp: cannot copy a directory, '{s}', into itself, '{t}'")
                    continue
                shutil.copytree(s, t, dirs_exist_ok=True)
            elif not os.path.exists(s):
                io.err(f"cp: cannot stat '{s}': No such file or directory")
                continue
            elif os.path.exists(t) and os.path.samefile(s, t):
                io.err(f"cp: '{s}' and '{t}' are the same file")
                continue
            elif "n" in o and os.path.exists(t):
                continue
            else:
                shutil.copy2(s, t)
            if "v" in o:
                io.line(f"'{s}' -> '{t}'")
        except OSError as exc:
            io.err(f"cp: cannot copy '{s}': {exc.strerror or exc}")
    return io.code


def cmd_mv(args, io):
    o, ops = parse("mv", args, "fvn", "", {"force": "f", "verbose": "v"})
    srcs, dst = _targets("mv", expand(ops))
    for s in srcs:
        t = os.path.join(dst, os.path.basename(s.rstrip("/\\"))) if os.path.isdir(dst) else dst
        if not os.path.lexists(s):
            io.err(f"mv: cannot stat '{s}': No such file or directory")
            continue
        if "n" in o and os.path.exists(t):
            continue
        try:
            if os.path.isfile(s) and os.path.isfile(t):
                os.replace(s, t)
            else:
                shutil.move(s, t)
            if "v" in o:
                io.line(f"renamed '{s}' -> '{t}'")
        except OSError as exc:
            io.err(f"mv: cannot move '{s}' to '{t}': {exc.strerror or exc}")
    return io.code


def cmd_mkdir(args, io):
    o, ops = parse("mkdir", args, "pv", "m", {"parents": "p", "verbose": "v", "mode": "m"})
    if not ops:
        raise Fail("mkdir: missing operand")
    for p in ops:
        try:
            if "p" in o:
                if os.path.exists(p) and not os.path.isdir(p):
                    raise FileExistsError(17, "File exists")
                os.makedirs(p, exist_ok=True)
            else:
                os.mkdir(p)
            if "v" in o:
                io.line(f"mkdir: created directory '{p}'")
        except FileExistsError:
            io.err(f"mkdir: cannot create directory '{p}': File exists")
        except FileNotFoundError:
            io.err(f"mkdir: cannot create directory '{p}': No such file or directory")
        except OSError as exc:
            io.err(f"mkdir: cannot create directory '{p}': {exc.strerror or exc}")
    return io.code


def cmd_touch(args, io):
    o, ops = parse("touch", args, "cam", "", {"no-create": "c"})
    if not ops:
        raise Fail("touch: missing file operand")
    for p in expand(ops):
        try:
            if os.path.exists(p):
                os.utime(p)
            elif "c" not in o:
                open(p, "ab").close()
        except OSError as exc:
            io.err(f"touch: cannot touch '{p}': {exc.strerror or exc}")
    return io.code


def cmd_pwd(args, io):
    parse("pwd", args, "LP")
    io.line(os.getcwd())
    return 0


def cmd_which(args, io):
    o, ops = parse("which", args, "a")
    exts = [""] + [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for name in ops:
        hits = []
        for d in [x for x in os.environ.get("PATH", "").split(os.pathsep) if x]:
            for e in exts:
                p = os.path.join(d, name + e)
                if (e or os.path.splitext(name)[1]) and os.path.isfile(p) and p not in hits:
                    hits.append(p)
        if not hits:
            io.err(f"which: no {name} in PATH")
        for p in hits if "a" in o else hits[:1]:
            io.line(p)
    return io.code


SED_HELP = ("sed: only `sed -n 'A,Bp' FILE` (print lines A to B; also 'Np', 'A,$p') is supported in this sandbox; "
            "read files with read_file and change them with edit_file")


def cmd_sed(args, io):
    try:
        o, ops = parse("sed", args, "n", "e")
    except Fail:
        raise Fail(SED_HELP) from None
    script = (o.get("e") or [None])[-1]
    if script is None and ops:
        script, ops = ops[0], ops[1:]
    m = re.fullmatch(r"\s*(\d+|\$)(?:\s*,\s*(\d+|\$))?\s*p\s*;?\s*", script or "")
    if "n" not in o or not m:
        raise Fail(SED_HELP)
    files = expand(ops) or ["-"]
    lines = []
    for p in files:
        try:
            lines += _lines(_read(p, io))
        except Fail as exc:
            io.err(f"sed: {exc}", 2)
    end = len(lines)
    a = end if m.group(1) == "$" else int(m.group(1))
    b = a if m.group(2) is None else (end if m.group(2) == "$" else int(m.group(2)))
    io.w(b"".join(lines[max(a, 1) - 1:b]))
    return io.code


def cmd_diff(args, io):
    o, ops = parse("diff", args, "uqNa", "U", {"unified": "U", "brief": "q", "text": "a", "new-file": "N"})
    if len(ops) != 2:
        raise Fail("diff: need exactly two files (diff -u A B)", 2)
    a, b = ops
    if os.path.isdir(a) and os.path.isdir(b):
        raise Fail("diff: comparing two directories is not supported here; compare files", 2)
    if os.path.isdir(b):
        b = os.path.join(b, os.path.basename(a))
    elif os.path.isdir(a):
        a = os.path.join(a, os.path.basename(b))
    texts = []
    for p in (a, b):
        if "N" in o and p != "-" and not os.path.exists(p):
            texts.append("")
            continue
        try:
            texts.append(_read(p, io).decode("utf-8", "replace"))
        except Fail as exc:
            raise Fail(f"diff: {exc}", 2) from None
    if texts[0] == texts[1]:
        return 0
    if "q" in o:
        io.line(f"Files {a} and {b} differ")
        return 1
    n = int((o.get("U") or ["3"])[-1])
    for ln in difflib.unified_diff(texts[0].splitlines(True), texts[1].splitlines(True), a, b, n=n):
        io.w(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    return 1


def cmd_dir(args, io):
    """cmd 의 dir 대신(격리 안에서는 드라이브 뿌리를 못 봐서 진짜 dir 이 실패한다): /b /s /a[d|-d] 만."""
    bare = recursive = False
    attrs = None
    ops = []
    for a in args:
        u = a.upper()
        if u.startswith("/") and len(a) > 1 and not os.path.exists(a):
            if u == "/B":
                bare = True
            elif u == "/S":
                recursive = True
            elif u.startswith("/A"):
                attrs = u[2:].lstrip(":") or "*"
            continue
        ops.append(a)
    show_hidden = attrs is not None

    def keep(path, name, isdir):
        if attrs == "D" and not isdir or attrs == "-D" and isdir:
            return False
        if show_hidden:
            return True
        try:
            return not (getattr(os.stat(path), "st_file_attributes", 0) & 2)     # FILE_ATTRIBUTE_HIDDEN
        except OSError:
            return True

    def line(path, name, isdir):
        if bare:
            return os.path.abspath(path) if recursive else name
        try:
            st = os.stat(path)
        except OSError:
            return name
        t = time.strftime("%Y-%m-%d  %H:%M", time.localtime(st.st_mtime))
        return f"{t}    {'<DIR>' if isdir else '':<9}{'' if isdir else f'{st.st_size:,}':>10} {name}"

    found = False
    for spec in ops or ["."]:
        if os.path.isdir(spec):
            base, pat = spec, "*"
        elif os.path.exists(spec):
            base, pat = os.path.dirname(spec) or ".", os.path.basename(spec)
        else:
            base, pat = os.path.dirname(spec) or ".", os.path.basename(spec)
        if not os.path.isdir(base):
            continue
        dirs = [base]
        while dirs:
            d = dirs.pop(0)
            try:
                ents = sorted(os.scandir(d), key=lambda e: e.name.lower())
            except OSError:
                continue
            rows = []
            for e in ents:
                isdir = e.is_dir()
                if fnmatch.fnmatch(e.name.lower(), pat.lower()) and keep(e.path, e.name, isdir):
                    rows.append(line(e.path, e.name, isdir))
                if recursive and isdir and e.name not in NOISE:
                    dirs.append(e.path)
            if rows:
                found = True
                if not bare and recursive:
                    io.line(f"\n Directory of {os.path.abspath(d)}\n")
                for r in rows:
                    io.line(r)
    if not found:
        io.err("File Not Found")
    return io.code


# ---------------------------------------------------------------- 명령 줄 옮기기(cmd.exe 앞에서)
_SEP = {"&&", "||", "|", "&", ";", "("}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _lex(line: str) -> list[list[str]]:
    """[종류, 원문] 목록. 종류: ws · op(&& || | & ; 명령 자리의 '(') · word. 원문을 이으면 원래 줄."""
    toks: list[list[str]] = []
    i, n = 0, len(line)

    def at_cmd():
        prev = next((t for t in reversed(toks) if t[0] != "ws"), None)
        return prev is None or (prev[0] == "op" and prev[1] in _SEP)

    def semi(j):
        return line[j] == ";" and (j + 1 == n or line[j + 1] in " \t")

    while i < n:
        c = line[i]
        if c in " \t":
            j = i
            while j < n and line[j] in " \t":
                j += 1
            toks.append(["ws", line[i:j]])
            i = j
        elif line[i:i + 2] in ("&&", "||"):
            toks.append(["op", line[i:i + 2]])
            i += 2
        elif c in "|&" or semi(i) or (c == "(" and at_cmd()):
            toks.append(["op", c])
            i += 1
        else:
            j = i
            while j < n:
                c = line[j]
                if c in " \t" or semi(j) or (c in "|&" and not (c == "&" and j > i and line[j - 1] in "<>")):
                    break
                if c == '"':
                    k = line.find('"', j + 1)
                    j = n if k < 0 else k + 1
                    continue
                if c == "'" and (j == i or line[j - 1] == "="):
                    k = line.find("'", j + 1)
                    if k > 0 and (k + 1 == n or line[k + 1] in " \t|&;)"):
                        j = k + 1
                        continue
                j += 1
            toks.append(["word", line[i:j]])
            i = j
    return toks


def _unquote(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


DEVNULL_OUT = '"%TEMP%\\cap-devnull"'      # 격리 안 TEMP = Windows 가 주는 격리 전용 임시 폴더 (NUL 을 못 여는 컴퓨터가 있다)
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_VAR_ALIAS = {"PWD": "CD"}


def _var(m: re.Match, known: dict[str, str]) -> str:
    """$X·${X} → 이 줄에서 앞서 정한 값, 아니면 %X% ($PWD → %CD%)."""
    name = m.group(1) or m.group(2)
    if name in known:
        return known[name]
    return f"%{_VAR_ALIAS.get(name, name)}%"


def translate(line: str) -> str:
    """cmd.exe 에 넘기기 전에 흔한 bash 습관을 조심스럽게 옮긴다(따옴표 안은 건드리지 않음):
    mkdir -p a/b → cap-mkdir, dir → cap-dir, export X=Y / X=Y cmd → set "X=Y" &&, >/dev/null → 격리 임시 파일(</dev/null → <NUL),
    '…' → "…"(안에 " 가 없을 때), "a; b" → "a & b", $X·${X} → %X%(같은 줄에서 정한 값이면 그 값, $PWD → %CD%).
    나머지는 그대로($(...)·for·if 같은 bash 문법은 옮기지 않는다)."""
    t = _lex(line)
    known: dict[str, str] = {}            # 이 줄에서 앞서 정한 변수(cmd 는 %X% 를 줄을 읽을 때 한 번에 펴서 뒤에서 못 본다)
    for k, tok in enumerate(t):
        if tok[0] == "op" and tok[1] == ";":
            tok[1] = "&" if k and t[k - 1][0] == "ws" else " &"
        elif tok[0] == "word":
            w = tok[1]
            if "'" not in w and "$" in w:
                # X=$Y… 는 같은 줄에서 정한 값만 넣는다(PATH=$PATH:x 같은 건 구분자가 달라 그대로 두어 실패가 보이게)
                names = [m.group(1) or m.group(2) for m in _VAR.finditer(w)]
                if not _ASSIGN.match(w) or all(n in known for n in names):
                    w = _VAR.sub(lambda m: _var(m, known), w)
            a = _ASSIGN.match(w)
            if a and "$" not in w and "%" not in a.group(2):
                known[a.group(1)] = _unquote(a.group(2))
            w = re.sub(r"^((?:[^'\"]*=)?)'([^'\"]*[^'\"\\])'$", r'\1"\2"', w)
            prev = next((x[1] for x in reversed(t[:k]) if x[0] != "ws"), "")
            m = re.match(r"^(.*?)/dev/null$", w)
            if m and (m.group(1).endswith(">") or (not m.group(1) and prev.endswith(">"))):
                w = m.group(1) + DEVNULL_OUT              # 출력 버리기: NUL 을 못 여는 컴퓨터가 있어 격리 임시 파일로
            elif m and (m.group(1).endswith("<") or (not m.group(1) and prev.endswith("<"))):
                w = m.group(1) + "NUL"
            tok[1] = w
    k = 0
    while k < len(t):
        kind, w = t[k]
        prev = next((x for x in reversed(t[:k]) if x[0] != "ws"), None)
        if kind != "word" or not (prev is None or (prev[0] == "op" and prev[1] in _SEP)):
            k += 1
            continue
        end = next((j for j in range(k + 1, len(t)) if t[j][0] == "op"), len(t))
        args = [x[1] for x in t[k + 1:end] if x[0] == "word"]
        low = w.lower()
        if low == "mkdir" and any(a.startswith("-") or "/" in a for a in args):
            t[k][1] = "cap-mkdir"
        elif low == "dir":
            t[k][1] = "cap-dir"
        elif low == "export" and args and all(_ASSIGN.match(a) and "$" not in a for a in args):
            sets = [f'set "{m.group(1)}={_unquote(m.group(2))}"' for m in map(_ASSIGN.match, args)]
            t[k:end] = [["word", " && ".join(sets)]] + ([["ws", " "]] if end < len(t) else [])
        elif _ASSIGN.match(w) and "$" not in w and args:
            j, sets = k, []
            while j < end and (t[j][0] == "ws" or (_ASSIGN.match(t[j][1]) and "$" not in t[j][1])):
                if t[j][0] == "word":
                    m = _ASSIGN.match(t[j][1])
                    sets.append(f'set "{m.group(1)}={_unquote(m.group(2))}"')
                j += 1
            if j < end:          # 뒤에 진짜 명령이 있을 때만(X=Y 혼자면 그대로)
                t[k:j] = [["word", " && ".join(sets)], ["ws", " "], ["op", "&&"], ["ws", " "]]
                k += 4
                continue
        k += 1
    return "".join(x[1] for x in t)


_PY_C = re.compile(r"(?:^|[\s&|;(])(?:python[\d.]*|py)(?:\.exe)?(?:\s+(?:-[XW]\s+\S+|-[A-Za-z]\S*))*\s+-c\s+$")
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_JOINERS = ("&&", "||", "|", "&", "(")
MULTILINE_ARG = ("[cmd.exe] this command was not run: a quoted argument spans several lines, and cmd.exe cannot pass "
                 "that. Put the text in a file with write_file and pass the file, or use one line.")


def flatten(cmd: str, inline_dir: str) -> str:
    """여러 줄 명령을 cmd.exe 가 받는 한 줄로 옮긴다(cmd.exe 는 첫 줄만 돌리고 나머지를 말없이 버린다).
    - 따옴표 밖의 줄바꿈: 차례로 실행(`a & b`). 줄 끝의 역슬래시 · `&&` · `||` · `|` 는 다음 줄과 잇는다. `#` 줄은 뺀다.
    - `python -c "여러 줄"`: 본문을 inline_dir 의 파일로 두고 `python -c "exec(open(...).read())"` 로(작업 폴더 기준 import 유지).
    - `명령 <<'EOF' … EOF`: 본문을 파일로 두고 `명령 < 파일`.
    - 그 밖의 여러 줄 따옴표 인자: 옮길 방법이 없어, 이유를 출력하고 exit 2 로 끝나는 명령으로 바꾼다."""
    src = cmd.replace("\r\n", "\n").strip()
    if "\n" not in src:
        return cmd

    def save(text: str, ext: str) -> str:
        import hashlib
        import time
        os.makedirs(inline_dir, exist_ok=True)
        now = time.time()
        for old in os.listdir(inline_dir):                  # 한 시간 지난 조각은 치운다
            q = os.path.join(inline_dir, old)
            try:
                if now - os.path.getmtime(q) > 3600:
                    os.remove(q)
            except OSError:
                pass
        data = text.encode("utf-8")
        path = os.path.join(inline_dir, hashlib.sha1(data).hexdigest()[:12] + ext)
        with open(path, "wb") as f:
            f.write(data)
        return path

    lines: list[str] = []
    cur = ""
    docs: list[tuple[str, bool, str]] = []       # 본문을 기다리는 heredoc: (끝 표시, 탭 떼기, 자리 표시)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and src[i + 1:i + 2] == "\n":          # 줄 잇기
            i += 2
            continue
        if c == "\n":
            i += 1
            for tag, dash, mark in docs:
                body: list[str] = []
                while i < n:
                    j = src.find("\n", i)
                    line = src[i:] if j < 0 else src[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip() == tag:
                        break
                    body.append(line.lstrip("\t") if dash else line)
                cur = cur.replace(mark, '< "' + save("\n".join(body) + "\n", ".txt") + '"')
            docs = []
            lines.append(cur)
            cur = ""
            continue
        if c == '"' or (c == "'" and (not cur or cur[-1] in " \t=(")):
            j = i + 1
            if c == '"':
                while j < n and src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
            else:
                j = src.find("'", j)
                j = n if j < 0 else j
            if j >= n:                                       # 닫히지 않은 따옴표: 그대로 두어 셸 오류가 보이게
                cur += src[i:]
                break
            seg = src[i:j + 1]
            if "\n" in seg:
                if not _PY_C.search(cur):
                    return "echo " + MULTILINE_ARG + " 1>&2 & exit /b 2"
                body_text = seg[1:-1]
                if c == '"':
                    body_text = re.sub(r'\\(["\\$`])', r"\1", body_text)      # bash 큰따옴표 안의 \" \\ \$ \`
                path = save(body_text, ".py").replace("\\", "/").replace("'", "\\'")
                seg = f"\"exec(open('{path}', encoding='utf-8').read())\""
            cur += seg
            i = j + 1
            continue
        m = _HEREDOC.match(src, i) if c == "<" else None
        if m:
            mark = f"\x00{len(docs)}\x00"
            docs.append((m.group(3), bool(m.group(1)), mark))
            cur += mark
            i = m.end()
            continue
        cur += c
        i += 1
    for _, _, mark in docs:                                  # 본문 없이 끝난 heredoc
        cur = cur.replace(mark, "< NUL")
    lines.append(cur)
    out = ""
    for ln in (x.strip() for x in lines):
        if not ln or ln.startswith("#"):
            continue
        if not out:
            out = ln
        elif out.endswith(_JOINERS):
            out += " " + ln
        else:
            out += " & " + ln
    return out


# ---------------------------------------------------------------- 격리 폴더에 깔기
def install(bin_dir: str, python: str) -> str:
    """bin_dir 에 winutils.py 사본과 명령마다 .cmd 를 둔다(내용이 같으면 다시 쓰지 않음). bin_dir 을 돌려준다."""
    os.makedirs(bin_dir, exist_ok=True)
    with open(__file__, "rb") as f:
        files = {"winutils.py": f.read()}
    for shim, name in SHIM_NAMES.items():
        files[shim + ".cmd"] = f'@"{python}" -I -X utf8 "%~dp0winutils.py" {name} %*\r\n'.encode("utf-8")
    files["python3.cmd"] = b"@python %*\r\n"
    for fname, data in files.items():
        p = os.path.join(bin_dir, fname)
        try:
            with open(p, "rb") as f:
                if f.read() == data:
                    continue
        except OSError:
            pass
        with open(p, "wb") as f:
            f.write(data)
    return bin_dir


def main(argv: list[str] | None = None, out=None, err=None, inp=None) -> int:
    io = IO(out, err, inp)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        io.err("usage: winutils <" + "|".join(COMMANDS) + "> [args...]", 2)
        return 2
    name, args = argv[0], argv[1:]
    if out is None:
        raw = _raw_args(name)
        if raw is not None:
            args = raw
    try:
        code = globals()["cmd_" + name](args, io)
    except Fail as exc:
        msg = str(exc)
        io.err(msg if msg.startswith(name) or msg.startswith("usage") else f"{name}: {msg}", exc.code)
        code = exc.code
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 0
    try:
        io.out.flush()
    except (BrokenPipeError, OSError):
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
