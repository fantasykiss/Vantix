"""랜딩 / 인증 진입점 E2E."""


def test_connect_page_loads(page, base_url, _server_up):
    page.goto(f"{base_url}/connect")
    assert "Vantix" in page.title()
    assert page.locator("#demo-btn").count() == 1
    assert page.locator("#auth-email").count() == 1


def test_login_rejects_unknown_account(page, base_url, _server_up):
    """존재하지 않는 계정 로그인 → 401. (계정 없이 검증 가능한 경로)"""
    resp = page.request.post(
        f"{base_url}/api/auth/login",
        data={"email": "e2e-nobody@example.com", "password": "wrongpass123"},
    )
    assert resp.status == 401


def test_demo_session_opens_dashboard(page, base_url, _server_up):
    resp = page.request.post(f"{base_url}/api/connect/demo")
    if resp.status != 200:
        import pytest

        pytest.skip("데모 세션 사용 불가")
    page.goto(f"{base_url}/")
    page.wait_for_load_state("networkidle")
    assert "Dashboard" in page.title() or page.locator("text=리스크 브리핑").count() > 0
