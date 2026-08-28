# 테스트 케이스 매트릭스

기존에 흩어져 있던 "수동 테스트 54건"을 추적 가능한 형태로 구조화한 정본(canonical) 목록.

## 표기

- **자동화**: `자동` = 회귀 스위트에 존재 / `수동` = 사람이 실행 / `계획` = 미구현
- **테스트 파일**: 자동화된 경우 대응 테스트 위치
- **우선순위**: P1(배포 차단) · P2(중요) · P3(부가)

---

## A. 인증 / 세션

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| AUTH-01 | 빈 이메일로 회원가입 | — | 400, "유효한 이메일" | P1 | 자동 | `api/test_auth.py` |
| AUTH-02 | 형식 오류 이메일(`a@b`, `not-an-email`) | — | 400 | P1 | 자동 | `api/test_auth.py` |
| AUTH-03 | 254자 초과 이메일 | — | 400 (DEF-002 회귀) | P1 | 자동 | `api/test_auth.py` |
| AUTH-04 | 8자 미만 비밀번호 | — | 400, "8자 이상" | P1 | 자동 | `api/test_auth.py` |
| AUTH-05 | 정상 회원가입 → 인증메일 발송 | 신규 이메일 | 200, `email_sent=true` | P1 | 계획(db) | — |
| AUTH-06 | 미인증 이메일로 재가입 | 미인증 계정 존재 | 200, 인증메일 재발송 | P2 | 계획(db) | — |
| AUTH-07 | 인증 완료 이메일로 재가입 | 인증 계정 존재 | 409 | P2 | 자동(db) | `api/test_auth.py` |
| AUTH-08 | 잘못된 비밀번호 로그인 | 계정 존재 | 401 | P1 | 자동(db) | `api/test_auth.py` |
| AUTH-09 | 존재하지 않는 계정 로그인 | — | 401 (계정 존재 여부 노출 안 함) | P1 | 자동(db) | `api/test_auth.py` |
| AUTH-10 | 미인증 이메일 로그인 | 미인증 계정 | 403, `email_not_verified` | P1 | 자동(db) | `api/test_auth.py` |
| AUTH-11 | 이메일 인증 링크 클릭 | 유효 토큰 | 인증 완료 → connect 이동 | P2 | 수동 | — |
| AUTH-12 | 로그인 성공 시 쿠키 속성 | Redmine 연결 저장됨 | `vx_session` httponly+secure+lax | P1 | 수동 | — |
| AUTH-13 | 세션 없이 보호 API 호출 | — | 401 `session_expired` | P1 | 자동 | `api/test_data_contract.py` |
| AUTH-14 | 로그아웃 후 대시보드 접근 | 로그인 상태 | connect 리다이렉트 / 로그인 UI | P2 | 자동(e2e) | `e2e/test_login.py` |
| AUTH-15 | vx_session 쿠키 유실 복구 | 커밋 d2e46c0 | 무한 루프 없이 복구 | P1 | 수동 | — |
| AUTH-16 | connect-redmine rate limit | 10회 초과/10분 | 429 | P2 | 자동 | `api/test_auth.py` (`_check_rate_limit`) |
| AUTH-17 | 계정 삭제 | 로그인 + 비번확인 | 관련 데이터 삭제 | P2 | 수동 | — |

## B. 대시보드 / 데이터

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| DASH-01 | `/api/data` 응답 스키마 | 세션 유효 | 필수 키 전부 존재 | P2 | 자동 | `api/test_data_contract.py` |
| DASH-02 | 이슈 0건 데이터셋 | 빈 프로젝트 | 200, `total_issues=0`, 크래시 없음 | P2 | 자동 | `api/test_data_contract.py` |
| DASH-03 | `/api/projects` identifier·name 매핑 | 세션 유효 | 각 행에 두 키 존재 | P3 | 자동 | `api/test_data_contract.py` |
| DASH-04 | 캐시 히트(신선) | 5분 내 재요청 | `cached=true` | P3 | 수동 | — |
| DASH-05 | stale-while-revalidate | 만료 캐시 | 즉시 응답 + 백그라운드 갱신 | P3 | 수동 | — |
| DASH-06 | KPI 위젯 렌더 | 로그인 | overdue/open/users/imminent/risk 표시 | P2 | 자동(e2e) | `e2e/test_dashboard.py` |
| DASH-07 | 리스크 점수 숫자 표시 | 로그인 | `#risk-num` 이 숫자 | P2 | 자동(e2e) | `e2e/test_dashboard.py` |
| DASH-08 | 선택 안 한 프로젝트 접근 | 프로젝트 선택 유저 | 403 `project_not_allowed` | P2 | 수동 | — |
| DASH-09 | 부서별 지연/대기 집계 | "부서_이름" 이슈 | 기획/서버/클라 분리 집계 | P2 | 수동 | — |

## C. 리스크 스코어링

| ID | 시나리오 | 입력 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| RISK-01 | 전건 지연 | overdue 3 / 3 | score 60, Critical | P1 | 자동 | `api/test_risk_scoring.py` |
| RISK-02 | 25% 지연 | overdue 1 / 4 | score 15, High | P1 | 자동 | `api/test_risk_scoring.py` |
| RISK-03 | urgent+pending 소량 | 1+1 / 8 | 5 ≤ score < 15, Medium | P1 | 자동 | `api/test_risk_scoring.py` |
| RISK-04 | 정상 프로젝트 | healthy 5 | score 0, Low | P1 | 자동 | `api/test_risk_scoring.py` |
| RISK-05 | 점수 내림차순 정렬 | 혼합 | 위험 프로젝트가 앞 | P2 | 자동 | `api/test_risk_scoring.py` |
| RISK-06 | 부서 파싱: 숫자 프리픽스 | `1기획_홍길동` | dept=`기획` | P2 | 자동 | `unit/test_dept_parsing.py` |
| RISK-07 | 부서 파싱: 공백 구분 | `홍길동 기획` | dept=`기획` | P2 | 자동 | `unit/test_dept_parsing.py` |

## D. 리포트

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| RPT-01 | 리포트 프리뷰 열기 | 세션 유효 | HTML 렌더 | P2 | 계획(e2e) | — |
| RPT-02 | 리포트 공유 링크 발급 | 세션 유효 | 200 + token | P2 | **xfail** | `api/test_report_share.py` — `main.py:3684` `uuid` 결함 |
| RPT-03 | 빈 html 공유 요청 | — | 400 `ok=false` | P3 | 자동 | `api/test_report_share.py` |
| RPT-04 | 공유 리포트 조회 | 유효 token | HTML 200 | P3 | 계획 | — |
| RPT-05 | TTL 만료 리포트 조회 | 24h 경과 | 만료 처리 | P3 | 수동 | — |
| RPT-06 | 이메일 리포트 발송 | Pro+ 플랜 | 발송 (SMTP 차단 시 문서화) | P3 | 수동 | — |

## E. 결제 / 플랜

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| PAY-01 | `PAYMENTS_ENABLED` 플래그 | — | `False` (베타) | P1 | 자동 | `api/test_payments_blocked.py` |
| PAY-02 | 빌링키 발급 요청 차단 | 로그인 | 403 "베타" | P1 | 자동 | `api/test_payments_blocked.py` |
| PAY-03 | 카드결제 완료 요청 차단 | 로그인 | 403 "베타" | P1 | 자동 | `api/test_payments_blocked.py` |
| PAY-04 | 비로그인 결제 API 호출 | — | 401 | P1 | 자동 | `api/test_payments_blocked.py` |
| PAY-05 | Free 플랜 프로젝트 수 제한 | Free | 1개 초과 불가 | P1 | 자동(unit) | `unit/test_plan_gating.py` |
| PAY-06 | 리포트 기능 게이팅 | Free vs Pro | Free=차단, Pro=허용 | P1 | 자동(unit) | `unit/test_plan_gating.py` |
| PAY-07 | CSV 내보내기 Business 전용 | 각 플랜 | Business 만 허용 | P2 | 자동(unit) | `unit/test_plan_gating.py` |
| PAY-08 | 알 수 없는 플랜 폴백 | `plan="garbage"` | Free 로 폴백 | P2 | 자동(unit) | `unit/test_plan_gating.py` |
| PAY-09 | 멤버는 결제 불가 | 워크스페이스 멤버 | 403 "오너만" | P2 | 계획(db) | — |
| PAY-10 | 구독 취소 → Free 강등 | active 빌링키 | plan=free | P2 | 수동 | — |
| PAY-11 | 환불 + PortOne 취소 API | 결제 이력 존재 | 취소 처리 | P2 | 수동 | — |

## F. 팀 워크스페이스

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 |
|---|---|---|---|---|---|
| TEAM-01 | 팀원 초대 (이메일) | 오너 | 초대 pending 생성 | P2 | 수동 |
| TEAM-02 | 초대 수락 → 멤버 합류 | 유효 토큰 | 워크스페이스 멤버 | P2 | 수동 |
| TEAM-03 | 멤버 수 제한 | Free 3 / Pro 15 | 초과 차단 | P2 | 계획(db) |
| TEAM-04 | 멤버는 오너 플랜 상속 | 멤버 로그인 | 오너 플랜 기준 게이팅 | P2 | 수동 |

## G. 헬스체크 / 운영

| ID | 시나리오 | 전제 | 기대 결과 | 우선 | 자동화 | 테스트 파일 |
|---|---|---|---|---|---|---|
| OPS-01 | `/health` 응답 계약 | — | 200, `{ok, db}` | P1 | 자동 | `api/test_health.py` |
| OPS-02 | `/health` 인증 불필요 | 쿠키 없음 | 200 | P1 | 자동 | `api/test_health.py` |
| OPS-03 | DB 장애 시 `/health` | DB down | 503 `ok=false` | P2 | 수동 | — |
| OPS-04 | 백업 복구 테스트 | 주간 백업 파일 | 임시 postgres 복원 성공 | P1 | 수동 | `launch-checklist.md` |
| OPS-05 | 전역 500 → error_log 기록 | 의도적 예외 | `vantix_error_log` 적재 | P2 | 수동 | — |

## H. 반응형 / UI

| ID | 시나리오 | 뷰포트 | 기대 결과 | 우선 | 자동화 |
|---|---|---|---|---|---|
| UI-01 | connect 페이지 모바일 | 375px | 레이아웃 정상 | P3 | 계획(e2e) |
| UI-02 | 대시보드 모바일 | 375px | KPI 스택, 가로 스크롤 없음 | P3 | 계획(e2e) |
| UI-03 | 결제 모달 카카오페이만 노출 | 데스크톱 | 신용카드 버튼 숨김 (커밋 c2cc7f3) | P3 | 수동 |

## I. 장애 롤백

| ID | 시나리오 | 기대 결과 | 우선 | 자동화 |
|---|---|---|---|---|
| ROLL-01 | Railway 배포 실패 | 이전 버전 자동 유지 | P1 | 수동 |
| ROLL-02 | 스케줄러 잡 실패 | 다른 잡 영향 없음, 잡로그 기록 | P2 | 수동 |
| ROLL-03 | Redmine API 다운 | 대시보드 degrade, 캐시 서빙 | P2 | 수동 |

---

## 커버리지 요약

| 영역 | 총 | 자동 | 수동 | 계획 |
|---|---|---|---|---|
| A. 인증/세션 | 17 | 8 | 6 | 3 |
| B. 대시보드 | 9 | 4 | 4 | 1 |
| C. 리스크 스코어 | 7 | 7 | 0 | 0 |
| D. 리포트 | 6 | 2 | 3 | 1 |
| E. 결제/플랜 | 11 | 8 | 2 | 1 |
| F. 팀 | 4 | 0 | 3 | 1 |
| G. 운영 | 5 | 2 | 3 | 0 |
| H. UI | 3 | 0 | 1 | 2 |
| I. 롤백 | 3 | 0 | 3 | 0 |
| **합계** | **65** | **31** | **28** | **6** |
