# STRUCTURE — Vantix Codebase Map
# 작업 전 반드시 참고할 것

## 파일 구조 / REDRISK-APP/

### main.py — CORE
- 경로: /redrisk-app/main.py
- 역할: 서버 진입점. 세션/유저 DB, 캐시 로직, 리스크 스코어링, AI 호출, 스케줄러, 결제·팀·대시보드 등 나머지 라우트
- 현재 상태: 3,916줄 (2026-07-03 리팩토링 후. REFACTOR_PLAN.md 참고)
- ⚠ connect/callouts/team/admin 라우트는 main.py에서 빠지고 app/routers/로 이전됨. 새 관련 엔드포인트는 main.py가 아니라 해당 라우터 파일에 추가할 것.
- ⚠ Redmine fetch 로직(fetch/fetch_all/get_projects/get_issues)은 app/redmine/client.py의 RedmineClient로 이전됨. main.py의 동일 이름 함수들은 호환용 래퍼일 뿐 — 신규 코드는 `app.redmine.factory.build_client()`를 직접 쓸 것.

### app/routers/ — MODULE (2026-07-03 신설)
- connect.py: /connect, /api/connect*, /api/disconnect, /api/update-connection
- callouts.py: /api/callouts*
- team.py: /api/team*, /api/account/projects
- admin.py: /api/track, /api/feedback, /api/admin/*, /admin
- 각 파일은 `import main as _m`으로 main.py의 세션/DB/캐시 등 공용 인프라를 참조한다 (순환 임포트 주의 — `from main import X` 금지, 반드시 `import main as _m` 후 `_m.X` 형태로 사용).

### app/redmine/ — MODULE (2026-07-03 신설)
- client.py: RedmineClient 클래스 — fetch/fetch_all/get_projects/get_issues 단일 소스
- factory.py: build_client(), client_from_session() — 클라이언트 생성 팩토리
- ⚠ main.py 내 78개 호출부의 redmine_url=/api_key= 파라미터는 아직 안 걷어냄 (Phase B-2, 미착수)

### templates/index.html — HTML
- 경로: /redrisk-app/templates/index.html
- 역할: 프론트엔드 전체. 대시보드 UI, JS 로직, CSS 스타일 모두 포함
- 규칙: 프론트엔드 수정은 여기서만

### config.py — CONFIG
- 경로: /redrisk-app/config.py
- 역할: 환경변수 로딩. BASE_URL, API_KEY, ANTHROPIC_API_KEY, 스케줄 설정, DEFAULT 값들

### app/constants.py — MODULE
- 경로: /redrisk-app/app/constants.py
- 역할: 공유 상수. CLOSED_SET, RESOLVED_SET, HOLD_SET, DEPT_NORMALIZE, dept_name(), short_name()

### app/reporter.py — MODULE
- 경로: /redrisk-app/app/reporter.py
- 역할: 주간 리포트 생성 + 이메일 발송. build_report_data(), render_html_report(), send_report_email()

### REFACTOR_PLAN.md — DOC (2026-07-03 신설)
- 경로: /redrisk-app/REFACTOR_PLAN.md
- 역할: 리팩토링 전체 계획(Phase A~F), 진행 상황, 검증 내역, 다음에 이어서 할 것(Phase B-2, C~F). 구조 관련 작업 전에 STRUCTURE.md와 함께 참고할 것.

### .env — SECRET
- 경로: /redrisk-app/.env
- 역할: API 키, URL 등 민감 정보
- 규칙: git에 절대 커밋 금지

### risk_history.json — DATA
- 경로: /redrisk-app/risk_history.json
- 역할: 리스크 스냅샷 히스토리 (자동 생성). 최대 52주 보관
- 규칙: 수동 편집 금지

---

## 작업 규칙 / WORK RULES

01. 프론트엔드 수정은 templates/index.html 에서만. main.py에 HTML 문자열 추가 금지.
02. 새 상수/상태값 추가 시 app/constants.py 에 추가. main.py에 직접 정의 금지.
03. 새 설정값(기본값, 날짜 등)은 config.py 에 추가. 하드코딩 금지.
04. 새 API 엔드포인트는 성격에 맞는 곳에 추가: connect/callouts/team/admin 관련이면 app/routers/의 해당 파일에, 그 외(대시보드/인증/결제/이슈/리포트/AI)는 main.py 하단 엔드포인트 블록에.
05. 큰 작업 전 백업 필수. main_backup_YYYYMMDD.py 패턴 유지.
06. .env 파일 git 커밋 금지. .gitignore 확인 후 작업.
07. 함수 내부 import 새로 추가 금지. 파일 최상단 import 블록에 추가.

---

## ⚠ 배포 전 필수 체크
- /api/risk-snapshot/trigger 엔드포인트 main.py에서 삭제 (dev-only, 보안 취약점)

---

## V1.1 예정 (배포 후 분리)
- app/ai_service.py — 미착수
- app/cache.py — 미착수
- app/redmine_client.py — ✅ 완료 (app/redmine/client.py + factory.py, 2026-07-03). 단, main.py 호출부 78곳 이관은 아직(Phase B-2)
- app/scheduler.py — 미착수
- app/html_builder.py — 미착수
- (2026-07-03 추가) app/routers/{connect,callouts,team,admin}.py — ✅ 완료. 상세는 REFACTOR_PLAN.md

---

## 디자인 시스템
- border-radius: 0 — 모든 요소 예외 없음
- 폰트: DM Sans + DM Mono
- 배경: #fbf9f8 (Canvas)
- 포인트: Black #000000 / White #ffffff
- 컬러 리뉴얼 예정: Crimson Red #B40023 + Ivory #FCF0D6

---

## Git 규칙
- 브랜치: main (직접 커밋, PR 없음)
- 저장소: fantasykiss/Vantix
- 커밋 시점: 명시적 지시 시에만
- 명령: git add . && git commit -m "..." && git push origin main
