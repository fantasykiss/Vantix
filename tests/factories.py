"""테스트용 Redmine 이슈 페이로드 팩토리.

실제 Redmine `/issues.json` 응답과 동일한 형태의 dict를 만들어
`main.get_issues` 몽키패치에 주입한다. 이렇게 하면 네트워크 없이
build_dashboard_data / 리스크 점수 로직을 결정론적으로 검증할 수 있다.
"""
from __future__ import annotations

from datetime import date, timedelta


def _d(offset_days: int) -> str:
    return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def make_issue(
    issue_id: int,
    *,
    assignee: str = "서버_홍길동",
    status: str = "진행",
    priority: str = "보통",
    project: str = "샘플프로젝트",
    project_identifier: str = "sample",
    due_offset: int | None = None,
    subject: str | None = None,
) -> dict:
    """Redmine 이슈 1건. due_offset=None 이면 마감일 없음, 음수면 지연(overdue)."""
    issue: dict = {
        "id": issue_id,
        "subject": subject or f"이슈 {issue_id}",
        "status": {"name": status},
        "priority": {"name": priority},
        "project": {"name": project, "identifier": project_identifier},
        "tracker": {"name": "작업"},
        "assigned_to": {"name": assignee},
        "updated_on": _d(-1) + "T00:00:00Z",
        "created_on": _d(-10) + "T00:00:00Z",
    }
    if due_offset is not None:
        issue["due_date"] = _d(due_offset)
    return issue


# 편의: 리스크 레벨별 이슈 세트 -------------------------------------------------

def overdue_issue(issue_id: int, **kw) -> dict:
    return make_issue(issue_id, due_offset=-3, status="진행", **kw)


def urgent_issue(issue_id: int, **kw) -> dict:
    """오늘~D+3 마감, 미완료 → urgent 카운트."""
    return make_issue(issue_id, due_offset=1, status="진행", **kw)


def pending_issue(issue_id: int, **kw) -> dict:
    return make_issue(issue_id, status="진행대기", **kw)


def healthy_issue(issue_id: int, **kw) -> dict:
    return make_issue(issue_id, due_offset=30, status="진행", **kw)
