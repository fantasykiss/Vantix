# Vantix

**AI 기반 Redmine 프로젝트 리스크 분석 대시보드** — 팀 전체의 일정 리스크를 한 화면에서 조기에 포착한다.

[![CI](https://github.com/fantasykiss/Vantix/actions/workflows/ci.yml/badge.svg)](https://github.com/fantasykiss/Vantix/actions/workflows/ci.yml)

🔗 **라이브 데모**: <https://web-production-cdd14.up.railway.app/connect> → "실제 기능 둘러보기" (샘플 데이터, 가입 불필요)

---

## 문제 / 배경

게임 개발 현장에서 Redmine에는 이슈 데이터가 쌓이지만, **"지금 어느 프로젝트가 위험한가"를 한눈에 보는 뷰가 없었다.**
PM·QA·개발·아트·기획이 각자 자기 이슈만 보고, 팀 전체의 마감 초과·부하 쏠림·마일스톤 지연은
매주 수작업으로 취합해야 했다.

Vantix는 Redmine REST API를 주기적으로 읽어

- 프로젝트별 **리스크 점수**(마감 초과·긴급·대기 가중치)를 자동 산출하고
- 담당자·부서별 부하와 마일스톤 진행을 집계하며
- LLM으로 **"무엇을 먼저 조치해야 하는가"**를 자연어로 요약한다.

## 스크린샷

| 랜딩 | 리스크 브리핑 대시보드 |
|---|---|
| [![landing](docs/screenshots/01-landing.png)](docs/screenshots/01-landing.png) | [![dashboard](docs/screenshots/02-dashboard.png)](docs/screenshots/02-dashboard.png) |

**리포트 내보내기 — 섹션 편집기**

[![report](docs/screenshots/03-report.png)](docs/screenshots/03-report.png)

> 스크린샷은 `python scripts/capture_screenshots.py`로 데모 세션에서 자동 생성한다.

## 핵심 기능

- **Risk Score** — 프로젝트별 `마감초과×60 + 긴급×30 + 대기×10` 가중 합산 → Critical/High/Medium/Low 4단계
- **주간 리스크 트렌드** — APScheduler 스냅샷, 13주 이력 차트
- **AI 인사이트 3종** — 리스크 코멘트 · 지연 예측 · 요약 리포트 (Claude)
- **담당자 부하 분석** — 담당자 문자열(`부서_이름`) 파싱 → 부서별 지연·과부하 집계
- **리포트** — HTML 내보내기 · 링크 공유 · 이메일 발송, 섹션 편집기
- **SaaS** — 이메일 인증 · 요금제 게이팅(Free/Pro/Business) · 팀 워크스페이스 · 포트원 결제

## 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 언어 · 런타임 | Python 3.11 |
| 웹 프레임워크 | FastAPI · Uvicorn · Starlette |
| 데이터베이스 | PostgreSQL (`psycopg2`) — 회원·결제·세션 / 파일 폴백 |
| 스케줄링 | APScheduler (`CronTrigger`, Asia/Seoul) |
| AI | Anthropic Claude API (리스크 코멘트·지연 예측·요약) |
| 프론트엔드 | 서버 렌더 단일 HTML SPA (Vanilla JS, `fetch`) |
| 외부 연동 | Redmine REST API · 포트원(빌링키/카카오페이) · Resend·SMTP |
| 보안 | `passlib`(bcrypt) · `cryptography`(Fernet, API 키 암호화) · 세션 토큰화 |
| **QA 자동화** | **pytest · Playwright · ruff** |
| **CI/CD** | **GitHub Actions** (postgres 서비스 컨테이너) |
| 배포 | Railway (Nixpacks) · 온프레미스: Docker Compose (`onpremise/`) |

## 아키텍처

```
┌─────────────┐      ┌──────────────────────────────────┐      ┌──────────────┐
│  Browser    │─────▶│  FastAPI  (main.py + app/routers) │─────▶│  Redmine     │
│  (HTML SPA) │◀─────│                                  │      │  REST API    │
└─────────────┘      │  ├─ 인메모리 캐시 (5분 TTL,        │      └──────────────┘
                     │  │   병렬 fetch 최대 5 worker)      │
                     │  ├─ 리스크 스코어링 / 부서 집계     │      ┌──────────────┐
                     │  ├─ APScheduler                    │─────▶│  Anthropic   │
                     │  │   (주간 리포트·스냅샷·DB 백업)    │      │  Claude API  │
                     │  └─ 인증 · 결제 · 팀              │      └──────────────┘
                     └───────────────┬──────────────────┘
                                     ▼
                          ┌────────────────────┐
                          │  PostgreSQL         │
                          │  회원·결제·세션      │
                          └────────────────────┘
```

- `main.py` (~4,200줄) — 페이지 템플릿 · 대시보드 라우트 · 캐시 · 리스크 스코어링 · 스케줄러
- `app/routers/` — connect · callouts · team · admin (기능별 분리)
- `app/redmine/` — Redmine 클라이언트 (fetch·재시도·병렬)
- `app/reporter.py` · `app/insights.py` — 리포트 렌더 · AI 프롬프트

코드베이스 상세 맵: [docs/STRUCTURE.md](docs/STRUCTURE.md)

## QA 자동화

> 이 저장소의 핵심 — 품질 검증을 단위 → API 통합 → E2E 3계층으로 자동화.

```bash
pip install -r requirements-dev.txt

pytest                       # 단위 + API (기본, e2e 제외)
pytest -m unit               # 순수 로직만 (밀리초)
pytest -m e2e                # 브라우저 E2E (서버 기동 + 테스트 계정 필요)
ruff check .                 # 린트
```

| 계층 | 위치 | 내용 |
|---|---|---|
| `unit` | `tests/unit/` | 요금제 게이팅, 담당자→부서 파싱 |
| `api` | `tests/api/` | 헬스체크·대시보드 계약·리스크 스코어·인증·결제 차단 |
| `e2e` | `tests/e2e/` | 데모 세션 → 대시보드 KPI·리스크 위젯 렌더 검증 (Playwright, 배포 데모 대상) |

- **운영 DB 보호** — 테스트는 `.env` 로딩을 무력화하고 `TEST_DATABASE_URL`로만 DB에 접근. 미설정 시 DB 테스트 자동 skip
- **CI** — `.github/workflows/ci.yml` (push·PR): ruff + `postgres:16` 서비스로 unit/api 실행, JUnit 리포트 요약
- **E2E** — `.github/workflows/e2e.yml`: 배포된 라이브 데모 대상 Playwright 실행 (주간 스케줄 + 수동), secret·DB 불필요
- **결함 추적** — 자동화로 발견한 실제 버그를 회귀 테스트로 고정 (예: `/api/report/share` `NameError`)

### QA 문서

- [테스트 전략](docs/qa/test-strategy.md) — 피라미드, 범위, 환경 매트릭스, entry/exit 기준
- [자동화 프레임워크 가이드](docs/qa/automation-framework.md) — 구조·픽스처·마커·새 테스트 추가법
- [테스트 케이스 매트릭스](docs/qa/test-cases.md) — 65건, 자동/수동/계획 추적
- [요구역량 추적표](docs/qa/requirements-traceability.md)

## 실행

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # BASE_URL, API_KEY, DATABASE_URL 등 채우기
python main.py                # http://localhost:8000
```

환경변수는 `config.py` 참조: `BASE_URL`/`API_KEY`(Redmine), `DATABASE_URL`(Postgres),
`ANTHROPIC_API_KEY`, `SMTP_*`, `PAYMENTS_ENABLED`, 리포트 스케줄.
