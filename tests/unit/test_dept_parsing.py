"""담당자 문자열 → 부서/이름 파싱 (app/constants.py).

Redmine 담당자는 "부서_이름" 관례를 따르지만 실제 데이터는 지저분하다.
회귀가 잦은 구간이라 대표 케이스를 고정한다.
"""
import pytest

from app.constants import dept_name, short_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("기획_홍길동", "기획"),
        ("서버_김철수", "서버"),
        ("1기획_ TEST1", "기획"),   # 숫자 프리픽스 정규화
        ("1클라", "클라"),
        ("홍길동 기획", "기획"),      # 공백 구분 + 부서 후행
        ("무소속", "무소속"),         # 구분자 없음 → 원본
    ],
)
def test_dept_name(raw, expected):
    assert dept_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("기획_ TEST1", "TEST1"),
        ("서버_김철수", "김철수"),
        ("이름만", "이름만"),
    ],
)
def test_short_name(raw, expected):
    assert short_name(raw) == expected
