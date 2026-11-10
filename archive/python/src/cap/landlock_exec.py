"""리눅스 샌드박스 실행기 (Codex 의 linux-sandbox 와 같은 Landlock 방식). 표준 라이브러리만 쓴다.

    python3 landlock_exec.py --write <폴더> [--write <폴더> ...] [--no-net] -- <명령> [인자...]

- 파일: 어디든 읽고 실행할 수 있지만, 쓰기·지우기·만들기는 --write 로 준 폴더(와 /dev 의 장치 파일)에서만.
- 네트워크(--no-net): TCP 연결·바인드 금지(Landlock ABI 4 이상).
- 관리자 권한이 필요 없다(Landlock 은 일반 사용자가 자기 자신에게 거는 제한). 한 번 걸면 자식 프로세스까지 풀 수 없다.
- 커널이 Landlock 을 지원하지 않으면 종료 코드 97 로 끝난다(조용히 격리 없이 돌지 않는다).
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446
PR_SET_NO_NEW_PRIVS = 38
RULE_PATH_BENEATH = 1

# 파일 접근 권한 비트 (linux/landlock.h)
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1

READ = EXECUTE | READ_FILE | READ_DIR
SANDBOX_ERROR = 97


def fs_rights(abi: int) -> int:
    r = (1 << 13) - 1                       # ABI 1: 0..12
    if abi >= 2:
        r |= REFER
    if abi >= 3:
        r |= TRUNCATE
    if abi >= 5:
        r |= IOCTL_DEV
    return r


def main(argv: list[str]) -> int:
    writes, no_net = [], False
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--write":
            writes.append(argv[i + 1])
            i += 2
        elif argv[i] == "--no-net":
            no_net = True
            i += 1
        else:
            print(f"landlock_exec: unknown option {argv[i]}", file=sys.stderr)
            return SANDBOX_ERROR
    cmd = argv[i + 1:]
    if not cmd:
        print("landlock_exec: no command", file=sys.stderr)
        return SANDBOX_ERROR
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        print("landlock_exec: this kernel has no Landlock; refusing to run unsandboxed", file=sys.stderr)
        return SANDBOX_ERROR
    handled_fs = fs_rights(abi)
    handled_net = (NET_BIND_TCP | NET_CONNECT_TCP) if (no_net and abi >= 4) else 0
    attr = struct.pack("=QQ", handled_fs, handled_net) if abi >= 4 else struct.pack("=Q", handled_fs)
    buf = ctypes.create_string_buffer(attr, len(attr))
    rs = libc.syscall(SYS_CREATE, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if rs < 0:
        print(f"landlock_exec: create_ruleset failed (errno {ctypes.get_errno()})", file=sys.stderr)
        return SANDBOX_ERROR

    def allow(path: str, rights: int) -> None:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return                              # 없는 폴더는 건너뛴다
        rule = struct.pack("=Qi", rights, fd)   # landlock_path_beneath_attr (packed)
        rb = ctypes.create_string_buffer(rule, len(rule))
        if libc.syscall(SYS_ADD_RULE, ctypes.c_int(rs), ctypes.c_int(RULE_PATH_BENEATH), rb, ctypes.c_uint32(0)) < 0:
            print(f"landlock_exec: add_rule {path} failed (errno {ctypes.get_errno()})", file=sys.stderr)
        os.close(fd)

    allow("/", READ & handled_fs)
    file_rw = (READ_FILE | WRITE_FILE | TRUNCATE | IOCTL_DEV) & handled_fs
    allow("/dev", file_rw | READ_DIR)        # /dev/null·/dev/tty 같은 장치 파일 쓰기
    for w in writes:
        allow(w, handled_fs)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        print("landlock_exec: prctl(NO_NEW_PRIVS) failed", file=sys.stderr)
        return SANDBOX_ERROR
    if libc.syscall(SYS_RESTRICT, ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
        print(f"landlock_exec: restrict_self failed (errno {ctypes.get_errno()})", file=sys.stderr)
        return SANDBOX_ERROR
    os.close(rs)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"landlock_exec: {cmd[0]}: {exc}", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
