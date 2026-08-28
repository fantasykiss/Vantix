"""공용 pytest 픽스처.

핵심 설계
---------
* `main` 임포트는 부작용(스케줄러 start, 캐시 워밍 스레드)을 동반한다.
  테스트 환경변수를 임포트 이전에 세팅해 외부 호출을 무력화하고,
  세션 종료 시 스케줄러를 내린다.
* Redmine REST 호출은 `main.get_issues` / `main.get_projects` 심(seam)에서
  몽키패치한다. 그 아래(app.redmine.client)는 건드리지 않는다.
* 인증 의존성(`_require_session`)은 FastAPI `dependency_overrides`로 우회한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --- main 임포트 전에 환경 격리 ----------------------------------------------
#
# config.py 는 `load_dotenv(override=True)` 로 .env 를 강제 로드한다.
# .env 의 DATABASE_URL 은 Railway '운영' DB 를 가리키므로, 테스트가
# 절대 운영 DB 를 건드리지 않도록 dotenv 로딩 자체를 무력화한다.
try:
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
except ImportError:
    pass

os.environ["BASE_URL"] = "http://redmine.invalid"
os.environ["API_KEY"] = "test-key"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["PAYMENTS_ENABLED"] = "false"
os.environ["RESEND_API_KEY"] = ""

# DB 연동 테스트는 명시적으로 지정한 '테스트 전용' DB 에서만 돈다.
# TEST_DATABASE_URL 이 없으면 파일 폴백 모드로 뜨고 db 마크 테스트는 skip.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")

# 세션/유저 파일이 저장소 루트를 오염시키지 않도록 tmp 로 격리
_TMP = Path(os.environ.get("PYTEST_TMP", "/tmp")) / "vantix-test"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["SESSIONS_PATH"] = str(_TMP / "sessions.json")


@pytest.fixture(scope="session")
def app_module():
    import main

    yield main

    # 스케줄러가 살아있으면 pytest 프로세스가 종료를 지연시킨다.
    try:
        main._scheduler.shutdown(wait=False)
    except Exception:
        pass


@pytest.fixture(scope="session")
def _testclient_cls():
    from starlette.testclient import TestClient

    return TestClient


@pytest.fixture
def fake_issues(app_module, monkeypatch):
    """`set_issues([...])` 로 다음 build_dashboard_data 호출의 이슈 목록을 지정.

    `/api/data` 는 project_id|updated_after|url 키로 캐시하므로
    테스트 간 격리를 위해 대시보드 캐시도 함께 비운다.
    """
    app_module._cache.clear()
    store: dict = {"issues": []}

    def _get_issues(project_id="", updated_after="", redmine_url=None, api_key=None):
        return store["issues"]

    def _get_projects(redmine_url=None, api_key=None):
        names = sorted({i["project"]["name"] for i in store["issues"]})
        return [{"id": idx + 1, "name": n, "identifier": n} for idx, n in enumerate(names)]

    monkeypatch.setattr(app_module, "get_issues", _get_issues)
    monkeypatch.setattr(app_module, "get_projects", _get_projects)
    # AI 프리생성 스레드가 실제 API를 때리지 않도록 차단
    monkeypatch.setattr(app_module, "_call_claude", lambda *a, **k: "[]", raising=False)

    def set_issues(issues):
        store["issues"] = list(issues)
        app_module._cache.clear()

    set_issues.__doc__ = "이슈 목록 주입"
    return type("FakeIssues", (), {"set": staticmethod(set_issues), "store": store})


@pytest.fixture
def client(app_module, _testclient_cls, fake_issues):
    """인증을 통과시킨 TestClient. 세션은 가짜 Redmine 자격증명을 반환한다."""
    main = app_module

    def _fake_session():
        return {"url": "http://redmine.invalid", "key": "test-key", "created": 0.0}

    main.app.dependency_overrides[main._require_session] = _fake_session
    try:
        with _testclient_cls(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(main._require_session, None)


@pytest.fixture
def anon_client(app_module, _testclient_cls):
    """인증 우회 없는 순수 클라이언트 (401 검증용)."""
    with _testclient_cls(app_module.app) as c:
        yield c


@pytest.fixture
def require_db():
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL 미설정 — DB 연동 테스트 건너뜀 (운영 DB 보호)")
