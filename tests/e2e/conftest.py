"""E2E 전용 픽스처.

로그인 계정 없이 **데모 세션**(`POST /api/connect/demo`)으로 대시보드를 검증한다.
데모는 배포 환경의 `DEMO_URL`/`DEMO_KEY` 로 동작하므로 CI 는 배포된 라이브 데모를 대상으로 실행한다.

전제조건 (없으면 자동 skip):
    E2E_BASE_URL   기본 http://localhost:8000 (CI 는 배포 URL 주입)

실행:
    # 로컬 (DEMO_URL/DEMO_KEY 가 .env 에 있어야 데모 동작)
    python main.py &
    E2E_BASE_URL=http://localhost:8000 pytest -m e2e

    # 배포 데모 대상
    E2E_BASE_URL=https://web-production-cdd14.up.railway.app pytest -m e2e
"""
import os
import urllib.request

import pytest

BASE_URL = os.getenv("E2E_BASE_URL", "http://localhost:8000").rstrip("/")


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
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=5) as r:
            if r.status != 200:
                pytest.skip(f"E2E 서버 비정상: {base_url} ({r.status})")
    except Exception as exc:
        pytest.skip(f"E2E 서버 응답 없음: {base_url} ({exc})")


@pytest.fixture
def demo_page(page, base_url, _server_up):
    """데모 세션이 걸린 Playwright page. 데모 미구성 환경이면 skip."""
    resp = page.request.post(f"{base_url}/api/connect/demo")
    if resp.status != 200:
        pytest.skip(f"데모 세션 사용 불가 (DEMO_URL/DEMO_KEY 미설정?): {resp.status}")
    return page
