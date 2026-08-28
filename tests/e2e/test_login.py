"""로그인 사용자 시나리오 E2E."""


def test_login_shows_user_email(logged_in_page, credentials):
    assert logged_in_page.inner_text("#nav-user-email") == credentials["email"]


def test_logout_returns_to_connect(logged_in_page, base_url):
    logged_in_page.evaluate("fetch('/api/auth/logout', {method:'POST'})")
    logged_in_page.goto(f"{base_url}/")
    # 세션이 끊기면 대시보드는 connect 로 리다이렉트되거나 로그인 UI 노출
    logged_in_page.wait_for_load_state("networkidle")
    assert "/connect" in logged_in_page.url or logged_in_page.locator("text=로그인").count() > 0
