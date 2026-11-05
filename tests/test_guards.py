from cap.guards import check, compile_decisions
from cap.spec import Item


def D(i, text):
    return Item(f"D{i}", "규칙", text)


def test_compile_templates():
    guards, manual = compile_decisions([
        D(1, "numpy import 금지"),
        D(2, "외부 의존성 없이 표준 라이브러리만 사용"),
        D(3, "의존성은 requests, pyyaml 만"),
        D(4, "오류 메시지는 errors 모듈에서 만든다"),
        D(5, "do not import pandas"),
    ])
    kinds = {g.decision_id: g.kind for g in guards}
    assert kinds == {"D1": "forbid_import", "D2": "stdlib_only", "D3": "allow_deps", "D5": "forbid_import"}
    assert manual == ["D4"]
    assert [g.pkgs for g in guards if g.decision_id == "D1"] == [["numpy"]]


def test_check_flags_forbidden_import(tmp_path):
    (tmp_path / "calc.py").write_text("import math\nimport numpy as np\n")
    (tmp_path / "helper.py").write_text("x = 1\n")
    (tmp_path / "test_calc.py").write_text("import numpy\n")   # 테스트 파일은 검사 안 함
    guards, _ = compile_decisions([D(1, "numpy import 금지")])
    v = check(guards, tmp_path, ["calc.py", "test_calc.py"])
    assert len(v) == 1 and v[0]["file"] == "calc.py" and v[0]["line"] == 2


def test_stdlib_only_allows_local_modules(tmp_path):
    (tmp_path / "helper.py").write_text("x = 1\n")
    (tmp_path / "calc.py").write_text("import json\nimport helper\nfrom requests import get\n")
    guards, _ = compile_decisions([D(1, "표준 라이브러리만 쓴다")])
    v = check(guards, tmp_path, ["calc.py"])
    assert [x["import"] for x in v] == ["requests"]


def test_allow_deps(tmp_path):
    (tmp_path / "a.py").write_text("import yaml\nimport requests\nimport os\n")
    guards, _ = compile_decisions([D(1, "의존성은 yaml 만")])
    v = check(guards, tmp_path, ["a.py"])
    assert [x["import"] for x in v] == ["requests"]
