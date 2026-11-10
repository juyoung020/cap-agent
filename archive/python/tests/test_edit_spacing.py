"""실제 실행에서 본 9B 실패: 한글 문자열의 띄어쓰기를 바꿔 old 를 복사함('0으로' → '0 으로')."""

from cap.tools import apply_edit

FILE = 'def divide(a, b):\n    """b 가 0 이면 ValueError("0으로 나눌 수 없습니다")."""\n    return a // b\n'
OLD = 'def divide(a, b):\n    """b 가 0 이면 ValueError("0 으로 나눌 수 없습니다")."""\n    return a // b'
NEW = ('def divide(a, b):\n    """b 가 0 이면 ValueError("0 으로 나눌 수 없습니다")."""\n    if b == 0:\n'
       '        raise ValueError("0 으로 나눌 수 없습니다")\n    return a / b')


def test_inner_spacing_stage_matches_and_keeps_file_spelling():
    r = apply_edit(FILE, OLD, NEW)
    assert r.ok and r.stage == "spaces"
    assert '"""b 가 0 이면 ValueError("0으로 나눌 수 없습니다")."""' in r.new_text   # 손대지 않은 줄은 원래대로
    assert 'raise ValueError("0으로 나눌 수 없습니다")' in r.new_text                # 새 줄의 문자열도 파일 표기로
    assert "return a / b" in r.new_text


def test_exact_edit_also_restores_known_string():
    r = apply_edit(FILE, "    return a // b", '    if b == 0:\n        raise ValueError("0 으로 나눌 수 없습니다")\n    return a / b')
    assert r.ok and r.stage == "exact"
    assert 'raise ValueError("0으로 나눌 수 없습니다")' in r.new_text
