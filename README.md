# Vantix

AI 기반 Redmine 프로젝트 리스크 분석 대시보드 (FastAPI · 단일 서비스)

## Features

- Risk Score 계산 (지연·긴급·대기 가중 합산 → Critical/High/Medium/Low)
- AI 프로젝트 분석 (리스크 코멘트 · 지연 예측 · AI 요약 리포트)
- Dashboard (부서별 부하·마일스톤·주간 트렌드)
- Redmine REST API 연동 (5-worker 병렬 fetch + 5분 캐시)
- 주간 리스크 스냅샷 · 이메일 리포트 (APScheduler)
- SaaS: 회원/인증 · 요금제 게이팅 · 팀 워크스페이스

## 실행

```bash
source venv/bin/activate
python main.py            # http://localhost:8000
```

환경변수는 `.env` (`config.py` 참조): `BASE_URL`, `API_KEY`, `DATABASE_URL`,
`SMTP_*`, `PAYMENTS_ENABLED`, 리포트 스케줄 등.

## QA 자동화

품질 검증은 단위 → API 통합 → E2E 3계층으로 자동화되어 있다.

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
| `e2e` | `tests/e2e/` | 로그인 → 대시보드 → 리포트 사용자 시나리오 (Playwright) |

- **운영 DB 보호**: 테스트는 `.env` 로딩을 무력화하고 `TEST_DATABASE_URL` 로만 DB에 접근한다.
  미설정 시 DB 연동 테스트는 자동 skip.
- **CI**: `.github/workflows/ci.yml` (push·PR) — lint + `postgres:16` 서비스로 unit/api 실행.
  `.github/workflows/e2e.yml` — 수동/야간 스케줄.

### 문서

- [테스트 전략](docs/qa/test-strategy.md)
- [자동화 프레임워크 가이드](docs/qa/automation-framework.md)
- [테스트 케이스 매트릭스](docs/qa/test-cases.md)
- [요구역량 추적표](docs/qa/requirements-traceability.md)

## 아키텍처

`main.py` 단일 파일(약 4,200줄)에 HTML 템플릿 · 라우트 · 캐시 · Redmine 호출 · 리스크
스코어링이 모두 들어 있다. 자세한 내용은 [CLAUDE.md](CLAUDE.md).
