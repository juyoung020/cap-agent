"""임시 폴더 지우기: 읽기 전용 git 개체와 Windows 예약 이름(nul) 파일까지."""
import os
import stat
import sys

import pytest

from cap.store import rmtree


def test_rmtree_read_only_files(tmp_path):
    d = tmp_path / "x"
    (d / ".git" / "objects").mkdir(parents=True)
    f = d / ".git" / "objects" / "ab"
    f.write_text("blob")
    os.chmod(f, stat.S_IREAD)
    rmtree(d)
    assert not d.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reserved names only")
def test_rmtree_reserved_name_file(tmp_path):
    d = tmp_path / "y"
    (d / "tests").mkdir(parents=True)
    with open("\\\\?\\" + str(d / "tests" / "nul"), "w") as fh:      # 모델 명령의 '> tests/nul' 이 남긴 파일
        fh.write("x")
    rmtree(d)
    assert not d.exists()
