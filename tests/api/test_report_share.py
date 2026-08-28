"""POST /api/report/share — 리포트 공유 링크 발급.

회귀 방지: main.py:3684 가 `uuid` (미임포트) 대신 `_uuid` 를 쓰도록 수정됨
(2026-08-28, QA 자동화로 발견). 이 테스트가 재발을 막는다.
"""
import pytest

pytestmark = pytest.mark.api


def test_report_share_issues_token(client):
    r = client.post("/api/report/share", json={"html": "<h1>주간 리포트</h1>"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["token"]


def test_report_share_rejects_empty_html(client):
    # 검증 분기는 uuid 라인 이전이라 정상 동작한다
    r = client.post("/api/report/share", json={"html": ""})
    assert r.status_code == 400
    assert r.json()["ok"] is False
