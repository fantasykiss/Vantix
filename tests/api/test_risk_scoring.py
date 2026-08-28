"""프로젝트 리스크 점수/등급 회귀 테스트.

공식 (main.py `build_dashboard_data` 참조):
    score = overdue/total*60 + urgent/total*30 + pending/total*10
    Critical >= 30 | High >= 15 | Medium >= 5 | Low < 5

리스크 스코어가 제품의 핵심 지표라 임계값을 자동으로 고정한다.
"""
import pytest

from tests.factories import healthy_issue, overdue_issue, pending_issue, urgent_issue

pytestmark = pytest.mark.api


def _risk_by_name(app_module, issues):
    data = app_module.build_dashboard_data("")  # project_id="" → 그룹맵 네트워크 호출 skip
    return {p["name"]: p for p in data["project_risk"]}


def test_all_overdue_is_critical(app_module, fake_issues):
    fake_issues.set([overdue_issue(i, project="P") for i in range(1, 4)])
    p = _risk_by_name(app_module, None)["P"]
    assert p["risk_score"] == 60.0
    assert p["risk_level"] == "Critical"


def test_one_in_four_overdue_is_high(app_module, fake_issues):
    fake_issues.set(
        [overdue_issue(1, project="P")]
        + [healthy_issue(i, project="P") for i in range(2, 5)]
    )
    p = _risk_by_name(app_module, None)["P"]
    assert p["risk_score"] == pytest.approx(15.0)
    assert p["risk_level"] == "High"


def test_one_in_eight_urgent_is_medium(app_module, fake_issues):
    fake_issues.set(
        [urgent_issue(1, project="P")]
        + [healthy_issue(i, project="P") for i in range(2, 9)]
    )
    p = _risk_by_name(app_module, None)["P"]
    # 30 / 8 = 3.75 → Low. urgent 1 + pending 1 로 Medium 경계 확인
    fake_issues.set(
        [urgent_issue(1, project="P"), pending_issue(2, project="P")]
        + [healthy_issue(i, project="P") for i in range(3, 9)]
    )
    p = _risk_by_name(app_module, None)["P"]
    assert 5 <= p["risk_score"] < 15
    assert p["risk_level"] == "Medium"


def test_healthy_project_is_low(app_module, fake_issues):
    fake_issues.set([healthy_issue(i, project="P") for i in range(1, 6)])
    p = _risk_by_name(app_module, None)["P"]
    assert p["risk_score"] == 0.0
    assert p["risk_level"] == "Low"


def test_projects_sorted_by_score_desc(app_module, fake_issues):
    fake_issues.set([
        healthy_issue(1, project="안전"),
        overdue_issue(2, project="위험"),
        overdue_issue(3, project="위험"),
    ])
    data = app_module.build_dashboard_data("")
    names = [p["name"] for p in data["project_risk"]]
    assert names.index("위험") < names.index("안전")
