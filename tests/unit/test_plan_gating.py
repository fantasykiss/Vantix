"""요금제 게이팅 순수 로직 (app/constants.py).

채용공고: "프로그래밍 및 스크립팅 능력", "품질 검증결과 기반 서비스 개선".
결제/플랜은 회귀 위험이 높은 구간이라 단위 테스트로 고정한다.
"""
import pytest

from app.constants import (
    DEFAULT_PLAN,
    plan_allows,
    plan_info,
    plan_member_limit,
    plan_project_limit,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "plan,limit",
    [("free", 1), ("pro", 5), ("business", -1)],
)
def test_project_limit_per_plan(plan, limit):
    assert plan_project_limit(plan) == limit


@pytest.mark.parametrize(
    "plan,member",
    [("free", 3), ("pro", 15), ("business", -1)],
)
def test_member_limit_per_plan(plan, member):
    assert plan_member_limit(plan) == member


def test_report_gated_to_paid_plans():
    assert plan_allows("free", "report") is False
    assert plan_allows("pro", "report") is True
    assert plan_allows("business", "report") is True


def test_csv_export_business_only():
    assert plan_allows("free", "csv") is False
    assert plan_allows("pro", "csv") is False
    assert plan_allows("business", "csv") is True


def test_unknown_plan_falls_back_to_free():
    assert plan_info("enterprise-made-up") == plan_info(DEFAULT_PLAN)
    assert plan_project_limit("garbage") == 1
