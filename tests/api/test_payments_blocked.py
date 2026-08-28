"""결제 차단 플래그 회귀 테스트.

배경: 실업급여 신청 대비로 결제 기능을 임시 차단(PAYMENTS_ENABLED=false).
백엔드 플래그가 실수로 되살아나면 안 되므로 자동 검증한다.
관련 커밋: 146bee9 "fix: 실업급여 신청 대비 결제 기능 임시 차단"
"""
import pytest

pytestmark = pytest.mark.api

BLOCKED_ENDPOINTS = ["/api/billing/issue", "/api/payment/card-complete"]


@pytest.fixture
def logged_in_client(app_module, _testclient_cls):
    main = app_module
    main.app.dependency_overrides[main._require_login] = lambda: 1
    try:
        with _testclient_cls(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(main._require_login, None)


def test_payments_flag_is_disabled(app_module):
    assert app_module.PAYMENTS_ENABLED is False


@pytest.mark.parametrize("path", BLOCKED_ENDPOINTS)
def test_payment_endpoint_returns_403_beta(logged_in_client, path):
    r = logged_in_client.post(path, json={"plan": "pro", "billingKey": "x"})
    assert r.status_code == 403
    assert "베타" in r.json()["detail"]


@pytest.mark.parametrize("path", BLOCKED_ENDPOINTS)
def test_payment_endpoint_still_requires_login(anon_client, path):
    assert anon_client.post(path, json={}).status_code == 401
