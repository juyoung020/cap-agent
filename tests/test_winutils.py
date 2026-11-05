"""Windows 격리(cmd.exe) 안의 유닉스 명령 모음(winutils)·명령 줄 옮기기·실제 격리 안 실행."""
import io
import os
import sys

import pytest

from cap import winutils as W

win = pytest.mark.skipif(os.name != "nt", reason="Windows only")


def run(*argv, stdin=b""):
    out, err = io.BytesIO(), io.StringIO()
    code = W.main(list(argv), out=out, err=err, inp=io.BytesIO(stdin))
    return code, out.getvalue().decode("utf-8"), err.getvalue()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    for name, text in {"a.txt": "alpha\nbeta yes\ngamma\n", "b.py": "def f():\n    return 1\n", ".hidden": "h\n",
                       "src/pkg/m.py": "import os\nX = 'Yes'\n", "한글.txt": "안녕 세계\n", ".git/cfg": "yes\n"}.items():
        (tmp_path / name).write_bytes(text.encode("utf-8"))        # 줄 끝은 \n 그대로(Windows 의 \r\n 변환 없이)
    return tmp_path


def test_ls_flags(tree):
    code, out, _ = run("ls")
    assert code == 0 and out.split("\n")[:-1] == ["a.txt", "b.py", "src", "한글.txt"]
    assert ".hidden" in run("ls", "-a")[1] and ".." in run("ls", "-a")[1].split("\n")
    assert ".." not in run("ls", "-A")[1].split("\n") and ".hidden" in run("ls", "-A")[1]
    assert run("ls", "-1")[1] == run("ls")[1]
    long = run("ls", "-l")[1]
    assert any(ln.startswith("d") and ln.endswith(" src") for ln in long.splitlines())
    assert any(ln.startswith("-") and " 21 " in ln and ln.endswith(" a.txt") for ln in long.splitlines())
    rec = run("ls", "-R", "src")[1]
    assert "src:" in rec and "src/pkg:" in rec and "m.py" in rec
    assert run("ls", "*.txt")[1].split() == ["a.txt", "한글.txt"]           # 와일드카드는 여기서 펼친다
    code, out, err = run("ls", "nope")
    assert code == 2 and "No such file" in err
    code, _, err = run("ls", "-Z")
    assert code == 2 and "unsupported option '-Z'" in err


def test_cat_head_tail(tree):
    assert run("cat", "a.txt", "b.py")[1] == "alpha\nbeta yes\ngamma\ndef f():\n    return 1\n"
    assert run("cat", "-n", "a.txt")[1].splitlines()[1] == "     2\tbeta yes"
    assert run("cat", stdin=b"piped\n")[1] == "piped\n"
    assert run("cat", "한글.txt")[1] == "안녕 세계\n"
    code, _, err = run("cat", "missing")
    assert code == 1 and "missing: No such file" in err
    assert run("head", "-n", "1", "a.txt")[1] == "alpha\n"
    assert run("head", "-2", "a.txt")[1] == "alpha\nbeta yes\n"
    assert run("tail", "-n", "1", "a.txt")[1] == "gamma\n"
    assert run("tail", "-n", "+2", "a.txt")[1] == "beta yes\ngamma\n"
    assert run("tail", "-1", stdin=b"x\ny\n")[1] == "y\n"
    out = run("head", "-n1", "a.txt", "b.py")[1]
    assert "==> a.txt <==\nalpha\n" in out and "==> b.py <==\ndef f():\n" in out


def test_grep_flags_and_exit_codes(tree):
    assert run("grep", "yes", "a.txt") == (0, "beta yes\n", "")
    assert run("grep", "-n", "gamma", "a.txt")[1] == "3:gamma\n"
    assert run("grep", "-i", "YES", "a.txt")[1] == "beta yes\n"
    assert run("grep", "-v", "a$", "a.txt")[1] == "beta yes\n"
    assert run("grep", "-E", "alpha|gamma", "a.txt")[1] == "alpha\ngamma\n"
    assert run("grep", r"alpha\|gamma", "a.txt")[1] == "alpha\ngamma\n"          # BRE 의 \|
    assert run("grep", "a+", "a.txt")[0] == 1                                   # BRE 에서 + 는 글자
    assert run("grep", "-c", "a", "a.txt")[1] == "3\n"
    assert run("grep", "-o", "-E", "[a-z]+a\\b", "a.txt")[1] == "alpha\nbeta\ngamma\n"
    assert run("grep", "-w", "bet", "a.txt")[0] == 1
    code, out, _ = run("grep", "-rn", "-i", "yes", ".")
    assert code == 0 and "./a.txt:2:beta yes" in out and "./src/pkg/m.py:2:X = 'Yes'" in out
    assert ".git" not in out                                                    # 시끄러운 폴더는 건너뜀
    assert run("grep", "-rl", "import", "--include=*.py", ".")[1] == "./src/pkg/m.py\n"
    assert run("grep", "-A", "1", "alpha", "a.txt")[1] == "alpha\nbeta yes\n"
    assert run("grep", "-C", "1", "-n", "gamma", "a.txt")[1] == "2-beta yes\n3:gamma\n"
    assert run("grep", "안녕", "한글.txt")[1] == "안녕 세계\n"
    assert run("grep", "-q", "beta", "a.txt") == (0, "", "")
    assert run("grep", "zzz", "a.txt") == (1, "", "")
    assert run("grep", "-e", "alpha", "-e", "gamma", "a.txt")[1] == "alpha\ngamma\n"
    assert run("grep", "x", stdin=b"x1\ny\nx2\n")[1] == "x1\nx2\n"
    code, _, err = run("grep", "-E", "(", "a.txt")
    assert code == 2 and "invalid regular expression" in err
    code, _, err = run("grep", "x", "src")
    assert code == 2 and "Is a directory" in err


def test_wc(tree):
    assert run("wc", "-l", "a.txt")[1] == "3 a.txt\n"
    assert run("wc", "-w", "a.txt")[1] == "4 a.txt\n"
    assert run("wc", "-c", "a.txt")[1] == "21 a.txt\n"
    assert run("wc", "a.txt")[1].split() == ["3", "4", "21", "a.txt"]
    assert run("wc", "-l", stdin=b"a\nb\n")[1] == "2\n"
    assert run("wc", "-l", "a.txt", "b.py")[1].splitlines()[-1].split() == ["5", "total"]


def test_find(tree):
    assert run("find", ".", "-name", "m.py")[1] == "./src/pkg/m.py\n"
    assert run("find", "src", "-type", "d")[1].split() == ["src", "src/pkg"]
    assert run("find", ".", "-name", "*.py", "-type", "f")[1].split() == ["./b.py", "./src/pkg/m.py"]
    assert run("find", ".", "-maxdepth", "1", "-iname", "A.TXT")[1] == "./a.txt\n"
    assert run("find", ".", "-path", "*/pkg/*")[1] == "./src/pkg/m.py\n"
    assert run("find", ".", "-name", "*.py", "-o", "-name", "a.txt")[1].split() == ["./a.txt", "./b.py",
                                                                                     "./src/pkg/m.py"]
    assert "./.git/cfg" not in run("find", ".")[1]
    assert "./b.py" not in run("find", ".", "-not", "-name", "*.py", "-type", "f")[1]
    code, _, err = run("find", ".", "-exec", "rm", "{}", ";")
    assert code == 1 and "unsupported expression '-exec'" in err
    assert run("find", ".", "-name", "*.py", "-delete")[0] == 0 and not (tree / "b.py").exists()
    assert W._windows_find(["/I", "x"]) and W._windows_find(["needle"]) and not W._windows_find([".", "-name", "x"])
    assert not W._windows_find(["./missing"]) and run("find", "./missing")[0] == 1
    assert W._find_exe_args(["/c", "/v", ""]) == ["/c", "/v", '""']                  # cmd 의 find.exe 로 넘김
    assert W._find_exe_args(["beta", "my file.txt"]) == ['"beta"', '"my file.txt"']


def test_rm_cp_mv_mkdir_touch(tree):
    assert run("mkdir", "-p", "n/o/p")[0] == 0 and (tree / "n" / "o" / "p").is_dir()
    assert run("mkdir", "-p", "n/o/p")[0] == 0                                   # 있으면 그냥 통과
    code, _, err = run("mkdir", "n")
    assert code == 1 and "File exists" in err
    assert run("mkdir", "x/y")[0] == 1                                           # -p 없으면 부모가 있어야
    assert run("touch", "n/o/p/f.txt")[0] == 0 and (tree / "n" / "o" / "p" / "f.txt").exists()
    assert run("cp", "a.txt", "n/")[0] == 0 and (tree / "n" / "a.txt").read_text(encoding="utf-8").startswith("alpha")
    code, _, err = run("cp", "n", "n2")
    assert code == 1 and "-r not specified" in err
    assert run("cp", "-r", "n", "n2")[0] == 0 and (tree / "n2" / "o" / "p" / "f.txt").exists()
    assert run("mv", "n2/a.txt", "n2/z.txt")[0] == 0 and (tree / "n2" / "z.txt").exists()
    assert run("mv", "b.py", "n2")[0] == 0 and (tree / "n2" / "b.py").exists() and not (tree / "b.py").exists()
    (tree / "n2" / "z2.txt").write_text("new", encoding="utf-8")
    assert run("mv", "n2/z2.txt", "n2/z.txt")[0] == 0 and (tree / "n2" / "z.txt").read_text() == "new"   # 덮어씀
    code, _, err = run("rm", "n")
    assert code == 1 and "Is a directory" in err
    assert run("rm", "-r", "n")[0] == 0 and not (tree / "n").exists()
    assert run("rm", "missing")[0] == 1 and run("rm", "-f", "missing")[0] == 0
    assert run("rm", "-rf", "n2")[0] == 0 and not (tree / "n2").exists()
    code, _, err = run("rm", "-rf", ".")
    assert code == 1 and "refusing" in err and (tree / "a.txt").exists()
    (tree / "t1.log").write_text("x")
    (tree / "t2.log").write_text("x")
    assert run("rm", "*.log")[0] == 0 and not list(tree.glob("*.log"))


def test_pwd_which(tree, monkeypatch):
    assert run("pwd")[1] == str(tree) + "\n"
    (tree / "bin").mkdir()
    (tree / "bin" / "tool.cmd").write_text("@echo hi\n")
    monkeypatch.setenv("PATH", str(tree / "bin"))
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    assert run("which", "tool")[1].strip().lower() == str(tree / "bin" / "tool.cmd").lower()
    code, _, err = run("which", "nope")
    assert code == 1 and "no nope" in err


def test_sed_print_range_only(tree):
    assert run("sed", "-n", "2,3p", "a.txt")[1] == "beta yes\ngamma\n"
    assert run("sed", "-n", "2p", "a.txt")[1] == "beta yes\n"
    assert run("sed", "-n", "2,$p", "a.txt")[1] == "beta yes\ngamma\n"
    assert run("sed", "-n", "1,1p", stdin=b"x\ny\n")[1] == "x\n"
    for bad in (["-i", "s/a/b/", "a.txt"], ["s/a/b/", "a.txt"], ["2,3p", "a.txt"]):
        code, _, err = run("sed", *bad)
        assert code == 1 and "only `sed -n 'A,Bp' FILE`" in err and "edit_file" in err


def test_diff_unified(tree):
    (tree / "c.txt").write_bytes(b"alpha\nBETA\ngamma\n")
    code, out, _ = run("diff", "-u", "a.txt", "c.txt")
    assert code == 1 and out.startswith("--- a.txt\n+++ c.txt\n@@") and "-beta yes\n+BETA\n" in out
    assert run("diff", "a.txt", "a.txt") == (0, "", "")
    assert run("diff", "-q", "a.txt", "c.txt")[1] == "Files a.txt and c.txt differ\n"
    assert run("diff", "-u", "a.txt", "nope")[0] == 2


def test_dir_windows_style(tree):
    bare = run("dir", "/b")[1].split("\n")
    assert "a.txt" in bare and "src" in bare
    assert run("dir", "/b", "*.py")[1] == "b.py\n"
    deep = run("dir", "/s", "/b", "*.py")[1].splitlines()
    assert str(tree / "src" / "pkg" / "m.py") in deep and not any(".git" in p for p in deep)
    assert "<DIR>" in run("dir")[1]
    assert run("dir", "/b", "nothing*")[0] == 1


def test_split_line_quotes():
    args = W.split_line("""-n 'two words' "x y" *.py "*.txt" a'b c'd "q\\"uote" """)
    assert list(args) == ["-n", "two words", "x y", "*.py", "*.txt", "ab cd", 'q"uote']
    assert [a.quoted for a in args] == [False, True, True, False, True, True, True]


def test_translate():
    t = W.translate
    assert t("mkdir -p a/b && touch a/b/c") == "cap-mkdir -p a/b && touch a/b/c"
    assert t("mkdir a/b") == "cap-mkdir a/b"
    assert t(r"mkdir a\b") == r"mkdir a\b"                        # cmd 의 mkdir 로 되는 것은 그대로
    assert t("dir /b && cd src && dir") == "cap-dir /b && cd src && cap-dir"
    assert t("export FOO=bar && python x.py") == 'set "FOO=bar" && python x.py'
    assert t("export A=1 B='x y'") == 'set "A=1" && set "B=x y"'
    assert t("PYTHONPATH=src python -m pytest -q") == 'set "PYTHONPATH=src" && python -m pytest -q'
    assert t("python x.py > /dev/null 2>&1") == "python x.py > NUL 2>&1"
    assert t("python x.py 2>/dev/null") == "python x.py 2>NUL"
    assert t("grep -n 'def foo' x.py | head -5") == 'grep -n "def foo" x.py | head -5'
    assert t("python -c 'print(1)'") == 'python -c "print(1)"'
    assert t("python -c 'import os; print(1)' && ls") == 'python -c "import os; print(1)" && ls'
    assert t("""python -c 'print("hi")'""") == """python -c 'print("hi")'"""     # 안에 " 가 있으면 그대로
    assert t("cd src; ls") == "cd src & ls"
    assert t('python -c "a=1; print(a)"') == 'python -c "a=1; print(a)"'      # 따옴표 안은 그대로
    assert t("echo don't stop; echo it's") == "echo don't stop & echo it's"
    assert t('echo "mkdir -p x" && git log --format=%H') == 'echo "mkdir -p x" && git log --format=%H'
    assert t("set PATH=a;b") == "set PATH=a;b"
    assert t("x=1") == "x=1" and t("export PATH=$PATH:x") == "export PATH=$PATH:x"
    assert t("python -m pytest -q") == "python -m pytest -q"
    assert t("ls | grep x || dir") == "ls | grep x || cap-dir"


def test_install_writes_shims_and_sandbox_env_puts_bin_first(tmp_path):
    from cap import appcontainer_exec as A
    b = tmp_path / "bin"
    W.install(str(b), r"C:\Py\python.exe")
    shim = (b / "ls.cmd").read_text(encoding="utf-8")
    assert shim.startswith('@"C:\\Py\\python.exe" -I -X utf8 "%~dp0winutils.py" ls %*')
    assert (b / "cap-mkdir.cmd").read_text(encoding="utf-8").split()[-2] == "mkdir"
    assert (b / "cap-dir.cmd").exists() and (b / "python3.cmd").exists() and not (b / "mkdir.cmd").exists()
    assert (b / "winutils.py").read_bytes() == open(W.__file__, "rb").read()
    env = A.sandbox_env({"PATH": r"C:\Windows\System32"}, str(tmp_path / "sbx"))
    assert env["PATH"] == str(tmp_path / "sbx" / "bin") + r";C:\Windows\System32"
    assert (tmp_path / "sbx" / "bin" / "grep.cmd").exists()


def test_shell_args_translates_for_appcontainer_only(tmp_path, monkeypatch):
    from cap import sandbox as sb
    from cap import tools as T
    seen = []
    monkeypatch.setattr(sb, "wrap", lambda inner, cwd, b, timeout=None: (seen.append(inner) or ["x"], ""))
    T.shell_args("mkdir -p a/b && dir", tmp_path, "appcontainer")
    assert seen[-1].endswith("&& cap-mkdir -p a/b && cap-dir")
    T.shell_args("mkdir -p a/b && dir", tmp_path, "landlock")
    assert seen[-1].endswith("mkdir -p a/b && dir") and "cap-" not in seen[-1]


def test_module_runs_as_a_script(tree):
    import subprocess
    r = subprocess.run([sys.executable, W.__file__, "grep", "-c", "a", "a.txt"], capture_output=True)
    assert r.returncode == 0 and r.stdout == b"3\n"
    r = subprocess.run([sys.executable, W.__file__, "nope"], capture_output=True)
    assert r.returncode == 2 and b"usage" in r.stderr


@win
def test_real_appcontainer_unix_commands(tmp_path):
    from cap import sandbox as S
    from cap import tools as T
    ok, msg = S.appc_setup()          # 시험마다 홈이 비어 있어(conftest) 매번 준비한다 — 파이썬 폴더 권한이라 1분쯤
    assert ok, msg
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "x.txt").write_bytes(b"alpha\nbeta yes\ngamma\n")
    (ws / "한글.txt").write_bytes("안녕 세계\n".encode("utf-8"))

    def sh(cmd):
        return T.run_shell(cmd, ws, timeout=60, sandbox="appcontainer")
    try:
        code, out = sh("ls")
        assert code == 0 and out.split() == ["x.txt", "한글.txt"], out
        code, out = sh("cat x.txt | grep yes")
        assert code == 0 and out.strip() == "beta yes", out
        code, out = sh("grep -n 'beta yes' *.txt")
        assert code == 0 and out.strip() == "x.txt:2:beta yes", out
        code, out = sh("cat 한글.txt && grep -c 안녕 한글.txt")
        assert code == 0 and out.split() == ["안녕", "세계", "1"], out
        code, out = sh("mkdir -p a/b && touch a/b/c && find . -name c")
        assert code == 0 and out.strip() == "./a/b/c" and (ws / "a" / "b" / "c").is_file(), out
        code, out = sh("rm -r a && ls")
        assert code == 0 and not (ws / "a").exists() and "a" not in out.split(), out
        code, out = sh("grep zzz x.txt || echo none")
        assert code == 0 and out.strip() == "none", out
        code, out = sh("grep zzz x.txt")
        assert code == 1, out
        code, out = sh("dir /b")
        assert code == 0 and "x.txt" in out.split(), out
        code, out = sh('type x.txt | find "beta"')                                  # cmd 식 find 는 find.exe 로
        assert code == 0 and out.strip() == "beta yes", out
        code, out = sh("export FOO=bar && python -c \"import os; print(os.environ['FOO'])\" > /dev/null && echo ok")
        assert code == 0 and out.strip() == "ok", out
        code, out = sh("head -n 1 x.txt; wc -l x.txt; pwd")
        assert code == 0 and out.split("\n")[:3] == ["alpha", "3 x.txt", str(ws.resolve())], out
    finally:
        assert "Removed" in S.appc_undo(ws)
