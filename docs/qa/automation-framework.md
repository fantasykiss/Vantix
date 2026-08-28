# 자동화 테스트 프레임워크 가이드

## 스택

| 계층 | 도구 |
|---|---|
| 러너 | pytest 9 |
| API | `starlette.testclient.TestClient` (httpx 기반) |
| E2E | Playwright (`pytest-playwright`) |
| 린트 | ruff (`pyproject.toml` → `[tool.ruff]`) |
| CI | GitHub Actions (`.github/workflows/ci.yml`, `e2e.yml`) |

## 디렉터리 구조

```
tests/
├── conftest.py          공용 픽스처 + 환경 격리 (main 임포트 전 .env 차단)
├── factories.py         Redmine 이슈 페이로드 팩토리
├── unit/                순수 로직 (외부 의존성 없음)
│   ├── test_plan_gating.py
│   └── test_dept_parsing.py
├── api/                 FastAPI TestClient 기반
│   ├── test_health.py
│   ├── test_data_contract.py
│   ├── test_risk_scoring.py
│   ├── test_auth.py            (@db 표시 일부)
│   ├── test_payments_blocked.py
│   └── test_report_share.py    (xfail: 알려진 결함 추적)
└── e2e/                 Playwright (실행 서버 필요)
    ├── conftest.py
    ├── test_login.py
    └── test_dashboard.py
```

## 마커

| 마커 | 의미 | 기본 실행 |
|---|---|---|
| `unit` | 순수 로직 | ✅ |
| `api` | TestClient API | ✅ |
| `e2e` | 브라우저 E2E | ❌ (`addopts = -m "not e2e"`) |
| `db` | Postgres 필요 | ⚠️ `TEST_DATABASE_URL` 있을 때만 |

```bash
pytest                    # unit + api (기본)
pytest -m unit            # 단위만
pytest -m "api or unit"   # 명시적
pytest -m e2e             # E2E (서버 + 계정 필요)
```

## 핵심 설계 원칙

### 1. 앱 코드 무수정
`main.py`(약 4,200줄 단일 파일)는 건드리지 않는다. 테스트는 다음 "심(seam)"에서만
개입한다:
- `main.get_issues`, `main.get_projects` → 몽키패치로 가짜 Redmine 데이터 주입
- `main._call_claude` → AI 호출 차단
- `app.dependency_overrides[_require_session / _require_login]` → 인증 우회

### 2. 운영 DB 절대 격리
`config.py` 가 `load_dotenv(override=True)` 로 운영 `DATABASE_URL` 을 강제 로드하므로,
`conftest.py` 가 **`main` 임포트 이전에** `dotenv.load_dotenv` 를 no-op 으로 교체하고
`DATABASE_URL` 을 `TEST_DATABASE_URL` 값(또는 빈 문자열)으로 덮어쓴다.

### 3. 결정론
- 이슈 마감일은 `date.today()` 기준 상대 오프셋(`factories._d`)으로 생성 → 날짜 무관 통과
- 대시보드 캐시는 `fake_issues` 픽스처가 매번 `main._cache.clear()`

### 4. 발견 결함의 테스트화
런타임 버그를 발견하면 `@pytest.mark.xfail(strict=True)` 테스트로 고정하고 사유에
파일:라인과 이슈를 남긴다. 수정되면 xfail 제거 → 회귀 방지 테스트로 승격.
예: `tests/api/test_report_share.py` (`main.py:3684` `uuid` 미임포트).

## 새 테스트 추가 방법

### API 테스트
```python
import pytest
from tests.factories import overdue_issue

pytestmark = pytest.mark.api

def test_something(client, fake_issues):
    fake_issues.set([overdue_issue(1, project="P")])
    r = client.get("/api/data")
    assert r.status_code == 200
```
- `client` — 인증 통과된 TestClient (`fake_issues` 자동 포함)
- `anon_client` — 인증 없는 클라이언트 (401/403 검증용)
- `fake_issues.set([...])` — 다음 Redmine 호출이 반환할 이슈 목록

### DB 테스트
```python
def test_db_thing(anon_client, require_db):   # require_db → TEST_DATABASE_URL 없으면 skip
    ...
```
생성한 행은 `finally` 블록에서 직접 삭제한다 (fixture teardown).

### E2E 테스트
```python
def test_flow(logged_in_page, base_url):
    logged_in_page.goto(f"{base_url}/")
    logged_in_page.wait_for_selector("#kpi-overdue")
```
`logged_in_page` — 로그인 완료된 Playwright page. 서버 미기동/계정 미설정 시 자동 skip.

## CI 파이프라인

### `ci.yml` (push·PR)
1. **lint** — `ruff check .`
2. **test** — `postgres:16` 서비스 + `pytest -m "not e2e"` + JUnit 리포트 요약

### `e2e.yml` (수동 / 매일 03:00 KST)
1. postgres 서비스 기동 → `python main.py` 백그라운드 → `/health` 폴링
2. `playwright install chromium` → `pytest -m e2e`

필요 secret: `E2E_LOGIN_EMAIL`, `E2E_LOGIN_PASSWORD`, `E2E_FERNET_KEY`
