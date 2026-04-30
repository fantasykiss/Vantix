# STRUCTURE — Vantix Codebase Map
# 작업 전 반드시 참고할 것

## 파일 구조 / REDRISK-APP/

### main.py — CORE
- 경로: /redrisk-app/main.py
- 역할: 서버 진입점. API 엔드포인트, 캐시 로직, Redmine fetch, AI 호출, 스케줄러 설정
- 현재 상태: ~1,466줄 (정리 완료)

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
04. 새 API 엔드포인트는 main.py 하단 엔드포인트 블록에 추가.
05. 큰 작업 전 백업 필수. main_backup_YYYYMMDD.py 패턴 유지.
06. .env 파일 git 커밋 금지. .gitignore 확인 후 작업.
07. 함수 내부 import 새로 추가 금지. 파일 최상단 import 블록에 추가.

---

## ⚠ 배포 전 필수 체크
- /api/risk-snapshot/trigger 엔드포인트 main.py에서 삭제 (dev-only, 보안 취약점)

---

## V1.1 예정 (배포 후 분리)
- app/ai_service.py
- app/cache.py
- app/redmine_client.py
- app/scheduler.py
- app/html_builder.py

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
