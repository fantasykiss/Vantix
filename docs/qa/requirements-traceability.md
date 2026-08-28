# 요구역량 ↔ 저장소 산출물 추적표

채용공고(현대차·기아·제네시스 대고객 서비스 품질관리 / QA 자동화 엔지니어)의
직무 항목별로 이 저장소에서 대응되는 산출물을 매핑한다.

## ■ API 자동화 테스트 설계 및 구현

| 요구 | 산출물 | 위치 |
|---|---|---|
| API 자동화 테스트 시나리오 작성·개발 | 헬스체크·대시보드·인증·결제·리포트 API 테스트 | `tests/api/` |
| REST 응답 계약 검증 | `/api/data` 필수 키·타입 스키마 assertion | `tests/api/test_data_contract.py` |
| 도메인 로직 검증 | 리스크 스코어 공식·등급 임계값 회귀 | `tests/api/test_risk_scoring.py` |
| 권한/인증 경계 검증 | 401/403, rate limit, 입력 검증 | `tests/api/test_auth.py` |
| CI 파이프라인 통합·주기 실행 | push·PR 마다 API 스위트 실행 + JUnit 요약 | `.github/workflows/ci.yml` |
| 외부 의존성 격리 | Redmine REST 몽키패치, 운영 DB 차단 | `tests/conftest.py` |

## ■ End-to-End 자동화 테스트 개발 및 운영

| 요구 | 산출물 | 위치 |
|---|---|---|
| 사용자 시나리오 기반 E2E 설계 | 로그인 → 대시보드 → 리포트 흐름 | `tests/e2e/` |
| 웹 앱 기능 흐름 검증 | KPI 위젯 렌더, 리스크 점수 표시, `/api/data` 호출 | `tests/e2e/test_dashboard.py` |
| 주요 기능 회귀 자동화 | 로그인/로그아웃 세션 흐름 | `tests/e2e/test_login.py` |
| 주기적 실행 | 매일 03:00 KST 스케줄 + 수동 트리거 | `.github/workflows/e2e.yml` |
| 테스트 실패 원인 분석 | 실패 시 앱 로그(`app.log`) 수집 | `e2e.yml` `App log (on failure)` 스텝 |
| 배포 품질 향상 개선활동 | 발견 결함의 xfail 고정 → 수정 후 회귀 테스트 승격 | `tests/api/test_report_share.py` |

## ■ 프로세스 관리

| 요구 | 산출물 | 위치 |
|---|---|---|
| 테스트 자동화 프레임워크 설계·유지보수 | 계층(unit/api/e2e)·마커·픽스처 규약 | `docs/qa/automation-framework.md`, `pytest.ini` |
| 테스트 진행상황·결과 공유 | CI test-summary 액션(JUnit → PR 요약) | `.github/workflows/ci.yml` |
| QA 프로세스 개선·자동화 전략 수립 | 테스트 전략·피라미드·entry/exit·리스크 우선순위 | `docs/qa/test-strategy.md` |
| 문서화 | 테스트 케이스 매트릭스(정본 65건, 추적 가능) | `docs/qa/test-cases.md` |
| 이슈 추적 도구 연계 | (현) 결함→테스트 고정 규약. (계획) Jira 연동 | `automation-framework.md` §4 |

## 지원자격 대응

| 자격 요건 | 근거 |
|---|---|
| 자동화 테스트 도구 활용 | pytest, Playwright, ruff, GitHub Actions |
| 프로그래밍 및 스크립팅 | Python 테스트 스위트, CI YAML, 픽스처/팩토리 설계 |
| 테스트 관리·이슈 추적 도구 | 테스트 케이스 매트릭스 + 결함 추적 규약 (`test_report_share.py`) |
| 품질 검증결과 기반 서비스 개선 | 자동화로 발견한 실제 결함 문서화 → 수정 태스크 연계 |

## 자동화로 발견한 결함 (예시)

| 결함 | 영향 | 상태 |
|---|---|---|
| `main.py:3684` — `uuid` 미임포트 (`import uuid as _uuid` 만 존재) | `POST /api/report/share` 호출 시 항상 `NameError` → 500. "리포트 공유" 버튼 전면 미작동 | ruff `F821` + xfail 테스트로 고정, 수정 태스크 등록 |

## 갭 / 다음 단계

- [ ] Jira/Confluence 연동 — 테스트 결과 자동 게시, 결함 자동 이슈 생성
- [ ] E2E 커버리지 확대 — 리포트 편집기, 프로젝트 선택, 팀 초대 흐름
- [ ] 계약 테스트 — Redmine API 스키마 변경 감지 (현재는 몽키패치 고정값)
- [ ] 성능/부하 — 대시보드 병렬 이슈 fetch(최대 5 워커) 임계 측정
- [ ] 접근성 — 반응형 뷰포트 자동 검증
- [ ] 커버리지 측정 — `pytest-cov` 도입, 임계선 설정
