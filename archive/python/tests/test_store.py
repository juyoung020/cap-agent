from cap.store import Ledger, Shadow


def test_shadow_snapshot_and_restore(tmp_path):
    (tmp_path / ".cap").mkdir()
    (tmp_path / "a.py").write_text("v1\n")
    sh = Shadow(tmp_path)
    s1 = sh.snapshot("cp1")
    (tmp_path / "a.py").write_text("v2\n")
    (tmp_path / "new.txt").write_text("later\n")
    sh.snapshot("cp2")
    assert set(sh.changed_files(s1)) == {"a.py", "new.txt"}
    assert sh.restore(s1)
    assert (tmp_path / "a.py").read_text() == "v1\n"
    assert not (tmp_path / "new.txt").exists()          # 스냅샷 뒤에 생긴 파일은 지움
    assert (tmp_path / ".cap").is_dir()                 # 하네스 폴더는 건드리지 않음


def test_ledger_roundtrip(tmp_path):
    led = Ledger(tmp_path, "r")
    led.log("test", cmd="pytest", passed=True)
    led.log("tool", tool="edit_file", changed=["a.py"])
    recs = led.read()
    assert [r["type"] for r in recs] == ["test", "tool"] and recs[0]["passed"] is True
