"""GET /api/data · /api/projects 응답 계약 (Redmine 몽키패치).

프론트 대시보드 전체가 이 페이로드 구조에 의존한다. 키가 사라지거나
타입이 바뀌면 UI가 조용히 깨지므로 스키마를 자동 검증한다.
"""
import pytest

from tests.factories import healthy_issue, overdue_issue

pytestmark = pytest.mark.api

# build_dashboard_data 가 항상 반환해야 하는 최상위 키
REQUIRED_KEYS = {
    "users", "users_active", "total_issues", "open_issues",
    "overdue", "project_risk", "users_data", "imminent_issues",
}


def test_api_data_requires_session(anon_client):
    assert anon_client.get("/api/data").status_code == 401


def test_api_data_schema(client, fake_issues):
    fake_issues.set([
        healthy_issue(1, assignee="서버_김철수"),
        overdue_issue(2, assignee="기획_이영희"),
    ])
    r = client.get("/api/data")
    assert r.status_code == 200
    body = r.json()

    assert REQUIRED_KEYS <= set(body), REQUIRED_KEYS - set(body)
    assert body["total_issues"] == 2
    assert body["overdue"] == 1
    assert isinstance(body["project_risk"], list)
    assert body["cached"] is False


def test_api_data_empty_dataset_is_safe(client, fake_issues):
    fake_issues.set([])
    r = client.get("/api/data")
    assert r.status_code == 200
    body = r.json()
    assert body["total_issues"] == 0
    assert body["project_risk"] == []


def test_api_projects_maps_identifier_and_name(client, fake_issues):
    fake_issues.set([
        healthy_issue(1, project="알파", project_identifier="alpha"),
        healthy_issue(2, project="베타", project_identifier="beta"),
    ])
    r = client.get("/api/projects")
    assert r.status_code == 200
    rows = r.json()
    assert {row["name"] for row in rows} == {"알파", "베타"}
    assert all({"identifier", "name"} <= set(row) for row in rows)
