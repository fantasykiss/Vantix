"""E2E 전용 픽스처.

전제조건 (없으면 자동 skip):
    E2E_BASE_URL          기본 http://localhost:8000
    E2E_LOGIN_EMAIL       테스트 계정 (이메일 인증 완료 + Redmine 연결 저장 상태)
    E2E_LOGIN_PASSWORD

실행:
    source venv/bin/activate
    python main.py                       # 다른 터미널에서 서버 기동
    E2E_LOGIN_EMAIL=... E2E_LOGIN_PASSWORD=... pytest -m e2e
"""
import os

import pytest

BASE_URL = os.getenv("E2E_BASE_URL", "http://localhost:8000")
LOGIN_EMAIL = os.getenv("E2E_LOGIN_EMAIL") or os.getenv("TEST_LOGIN_EMAIL")
LOGIN_PASSWORD = os.getenv("E2E_LOGIN_PASSWORD") or os.getenv("TEST_LOGIN_PASSWORD")


def pytest_collection_modifyitems(config, items):
    """tests/e2e/* 전체에 e2e 마커 자동 부여."""
    for item in items:
        if "tests/e2e/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def _server_up(base_url):
    import urllib.request

    try:
        urllib.request.urlopen(f"{base_url}/health", timeout=3)
    except Exception:
        pytest.skip(f"E2E 서버 응답 없음: {base_url}")


@pytest.fixture
def credentials():
    if not (LOGIN_EMAIL and LOGIN_PASSWORD):
        pytest.skip("E2E_LOGIN_EMAIL / E2E_LOGIN_PASSWORD 미설정")
    return {"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD}


@pytest.fixture
def logged_in_page(page, base_url, credentials, _server_up):
    page.goto(f"{base_url}/connect")
    page.click("text=로그인")
    page.fill("#auth-email", credentials["email"])
    page.fill("#auth-password", credentials["password"])
    page.click("#auth-btn")
    page.wait_for_selector("#nav-user-email", state="visible", timeout=10000)
    return page
