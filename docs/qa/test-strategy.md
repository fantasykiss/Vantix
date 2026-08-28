# Vantix 테스트 전략

> 이 문서는 Vantix(레드마인 리스크 대시보드 / SaaS)의 품질 검증 전략을 정의한다.
> QA 자동화 엔지니어 직무 요구(API 자동화·E2E·프로세스 관리)에 맞춰 구성했다.

## 1. 목표와 품질 기준

| 항목 | 기준 |
|---|---|
| 배포 게이트 | `main`/`dev` push·PR 시 `unit` + `api` 스위트 100% 통과 |
| 리스크 스코어 정확도 | 공식(overdue×60 + urgent×30 + pending×10)/등급 임계값 회귀 0건 |
| 인증/결제 회귀 | 입력검증·권한·결제차단 플래그 자동 검증 상시 green |
| E2E 핵심 시나리오 | 로그인 → 대시보드 → 리포트 열람: 야간 스케줄 통과 |
| 결함 추적 | 발견 결함은 실패/xfail 테스트로 고정 후 이슈화 (예: `test_report_share.py`) |

## 2. 테스트 피라미드

```
        ╱ E2E ╲          tests/e2e/     Playwright, 실서버+계정 필요, 수동/야간
      ╱─────────╲
     ╱  API 통합  ╲       tests/api/     FastAPI TestClient, Redmine 몽키패치
   ╱───────────────╲
  ╱   단위 (unit)    ╲    tests/unit/    순수 로직, 외부 의존성 0
 ╱───────────────────╲
```

- **단위** — `app/constants.py`(플랜 게이팅, 부서 파싱). 밀리초 단위, 항상 실행.
- **API 통합** — 라우트 계약, 스키마, 권한(401/403), 리스크 스코어 end-to-end.
  Redmine REST 호출은 `main.get_issues` / `main.get_projects` 심에서 가짜 데이터로 대체.
- **E2E** — 실제 브라우저로 사용자 시나리오. 가장 비싸므로 핵심 흐름만.

## 3. 범위

### 대상 (in scope)
- REST API: `/health`, `/api/data`, `/api/projects`, `/api/auth/*`, `/api/billing/*`, `/api/report/share`
- 도메인 로직: 리스크 스코어링, 요금제 게이팅, 담당자→부서 파싱
- 사용자 흐름: 회원가입/로그인/로그아웃, 대시보드 렌더, 리포트

### 비대상 (out of scope, 현재)
- Redmine 서버 자체 동작 (외부 시스템)
- 이메일 실발송 (SMTP 포트 차단, 코드만 완성)
- PortOne 실결제 (베타 기간 `PAYMENTS_ENABLED=false`)
- AI 응답 품질 (LLM 출력은 비결정적 — 호출/캐시 경로만 검증)

## 4. 환경 매트릭스

| 환경 | DB | Redmine | 용도 |
|---|---|---|---|
| 로컬 unit/api | 없음 (파일 폴백) | 몽키패치 | 개발 중 즉시 피드백 |
| CI `test` 잡 | `postgres:16` 서비스 컨테이너 (`TEST_DATABASE_URL`) | 몽키패치 | PR 게이트 |
| CI `e2e` 잡 | 일회용 postgres | 실제 or mock 계정 | 야간 회귀 |
| 운영 (Railway) | 운영 Postgres | 실제 | — 테스트 금지 |

> **운영 DB 보호**: `tests/conftest.py` 가 `.env` 로딩을 무력화하고 `DATABASE_URL` 을
> `TEST_DATABASE_URL` 로만 채운다. `TEST_DATABASE_URL` 미설정 시 DB 테스트는 skip.

## 5. Entry / Exit 기준

**Entry** (테스트 시작 조건)
- 변경이 `dev` 브랜치에 있고 앱이 로컬에서 기동됨
- `requirements-dev.txt` 설치 완료

**Exit** (릴리스 가능 조건)
- `pytest -m "not e2e"` 전부 통과 (xfail 제외 신규 FAIL 0)
- `ruff check .` 통과
- E2E 핵심 시나리오 최근 실행 green
- 신규 결함은 이슈 등록 + 회귀 테스트 추가

## 6. 리스크 기반 우선순위

| 우선순위 | 영역 | 근거 |
|---|---|---|
| P1 | 인증·세션·결제 차단 | 보안/과금 사고 직결 |
| P1 | 리스크 스코어링 | 제품 핵심 지표, 잘못되면 신뢰 상실 |
| P2 | 대시보드 데이터 계약 | UI 전체가 의존 |
| P2 | 리포트 생성/공유 | 유료 기능 |
| P3 | AI 인사이트 | 실패해도 degrade, 핵심 아님 |

## 7. 관련 문서
- [자동화 프레임워크 가이드](automation-framework.md)
- [테스트 케이스 매트릭스](test-cases.md)
- [요구역량 추적표](requirements-traceability.md)
- 장애 대응: `launch-checklist.md` (저장소 루트)
