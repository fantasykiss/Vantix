"""GET /health 응답 계약.

채용공고: "CI/CD 파이프라인에 대한 API 통합 자동화 테스트 및 주기적 실행".
헬스체크는 업타임 모니터링과 배포 게이트가 물려있어 계약을 고정한다.
"""
import pytest

pytestmark = pytest.mark.api


def test_health_ok_without_database(anon_client):
    r = anon_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body) == {"ok", "db"}


def test_health_is_unauthenticated(anon_client):
    # 세션 쿠키 없이도 200 — 모니터링 도구가 인증 없이 호출한다.
    assert anon_client.get("/health").status_code == 200
