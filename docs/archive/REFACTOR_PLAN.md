# REFACTOR_PLAN — Vantix 리팩토링 계획

> 작성일: 2026-07-03
> 목적: 유지보수성·확장성 확보를 위한 설계 패턴 적용 및 단계별 실행 계획
> 실행 전 STRUCTURE.md의 작업 규칙(백업 필수, beta 브랜치 작업, git push는 명시적 지시 시에만)을 그대로 따른다.

## 0. 진행 상황 (2026-07-03)

**완료 — Phase A (라우터 분리):**
- `app/routers/connect.py`, `callouts.py`, `team.py`, `admin.py` 신규 생성. main.py에서 해당 라우트 원본 제거, `app.include_router()`로 연결.
- 검증: 원본 백업(`main_backup_20260703.py`) 대비 라우트 75개 전수 대조 — 경로/메서드 1:1 일치, 누락·중복 없음. 임시 venv에서 `import main` 성공 확인(로컬 DB/Redmine 네트워크는 이 작업 환경에서 접근 불가하므로 `DATABASE_URL` 비활성 상태로 테스트), 등록된 라우트 81개(순정 FastAPI 문서 라우트 포함) 정상 확인.
- main.py 4,843줄 → 3,916줄.

**완료 — Phase B (Redmine 클라이언트 캡슐화, 축소 버전):**
- `app/redmine/client.py`에 `RedmineClient` 클래스 추가 (fetch/fetch_all/get_projects/get_issues 로직의 단일 소스). `app/redmine/factory.py`에 `build_client()`/`client_from_session()` 추가.
- main.py의 `fetch()`, `fetch_all()`, `get_projects()`, `get_issues()`는 시그니처를 그대로 유지한 **호환 래퍼**로 축소 — 내부적으로 `RedmineClient`를 호출한다. 이 4개 함수를 부르는 기존 78곳의 호출부(redmine_url=, api_key= 키워드 인자 포함)는 전혀 수정하지 않았다.
- 검증: 로컬 mock HTTP 서버(127.0.0.1)를 띄워 `get_projects`/`get_issues`/`fetch`가 새 `RedmineClient` 경로를 통해 실제로 정상 동작하는지 종단 테스트 완료.
- **의도적으로 하지 않은 것:** `build_dashboard_data`, `get_groups`, `get_versions` 등 78개 호출부에서 `redmine_url=`/`api_key=` 키워드 인자를 걷어내고 `client.method()` 직접 호출로 바꾸는 작업(원래 계획한 "파라미터 스레딩 완전 제거")은 하지 않았다. 이 세션은 실제 Redmine 서버·프로덕션 DB에 네트워크로 접근할 수 없어 대시보드 데이터 경로 전체를 라이브로 검증할 수 없었고, 결제 테스트가 진행 중인 시점에 가장 트래픽이 많은 코드 경로를 blind하게 78곳 고치는 건 리스크가 커서 보수적으로 멈췄다. 아래 "Phase B-2"로 별도 남겨둔다.

**변경 없음:** 결제(`/api/billing/*`, `/api/payment/*`) 관련 코드는 이번 작업에서 전혀 건드리지 않았다 (진행 중인 PG 테스트와 무관).

**아직 로컬 실행 검증 전:** 이 세션은 샌드박스 환경이라 실제 Redmine 서버·Postgres에 네트워크로 붙을 수 없다. `python main.py`로 실제 기동해서 브라우저로 스모크 테스트하는 건 사용자 로컬 환경(WORKFLOW.md 기준)에서 한 번 거쳐야 한다. git add/commit/push는 하지 않았다.

**Phase B-2 (미실행, 향후 별도 세션 권장):** `build_dashboard_data`/`get_groups`/`get_versions`/`build_version_data` 및 AI·리포트 라우트에서 `redmine_url=`/`api_key=` 파라미터를 걷어내고 세션에서 만든 `RedmineClient` 인스턴스를 직접 주고받도록 정리. 실제 Redmine 서버에 붙여 로컬에서 검증 가능한 세션에서 진행할 것.

---

## 1. 현재 상태 진단

| 파일 | 줄 수 |
|---|---|
| main.py | 4,843 |
| app/reporter.py | 909 |
| app/insights.py | 682 |
| config.py | 65 |
| app/constants.py | 61 |
| **합계** | **6,560** |

STRUCTURE.md에는 main.py가 "~1,466줄 (정리 완료)"로 기록되어 있지만, Phase 4(로그인·결제·팀 관리) 기능이 그대로 main.py에 누적되면서 실제로는 3배 넘게 증가했다. 현재 main.py 안에 섞여 있는 책임은 9가지다: 세션/유저 DB(SQLite+Postgres 이중 경로), API 키 암복호화, 인메모리 캐시, Redmine fetch, 리스크 스코어링, AI 호출, Rate limiting, 방문자 분석, APScheduler 잡(6개) — 그리고 이 위에 라우트 70개 이상(대시보드/관리자/인증/결제/팀/커넥트/이슈/콜아웃/리포트/AI)이 얹혀 있다.

**확인된 코드 스멜:**

1. **파라미터 스레딩** — `redmine_url`, `api_key`가 `fetch()`, `fetch_all()`, `get_issues()`, `build_dashboard_data()` 등 Redmine 관련 함수 시그니처 거의 전부에 반복 전파된다. 멀티테넌트(고객마다 다른 Redmine 서버) 구조인데 커넥션 정보를 객체로 감싸지 않아 함수 하나 추가할 때마다 두 파라미터를 계속 들고 다녀야 한다.
2. **DB 접근 분산** — `_db_conn`(Postgres pool), `_PgUsersConn`, `_users_db`(SQLite) 세 가지 애드혹 클래스가 따로 존재하고, SQLite/Postgres 분기 로직이 라우트 함수 내부에도 섞여 있다.
3. **검증 계층 부재** — 결제(`/api/billing/*`), 팀 초대(`/api/team/invite`) 같은 민감한 라우트조차 `await request.json()`으로 수동 파싱한다. Pydantic 스키마가 없어 입력 검증이 라우트마다 제각각이다.
4. **리스크 스코어 공식 중복** — 동일한 가중합 방식(overdue×60% + urgent×30% + pending×10%)이 `build_dashboard_data`(1573행, 프로젝트 단위)와 `get_groups` 내부(2148행, 담당자 과부하 지표)에 서로 다른 변형(가중치·정규화·임계값 상이)으로 각각 하드코딩돼 있다. 공식을 바꾸려면 두 곳을 따로 고쳐야 한다.
5. **계획된 분리가 실행되지 않음** — STRUCTURE.md는 이미 V1.1 분리 계획(`app/ai_service.py`, `app/cache.py`, `app/redmine_client.py`, `app/scheduler.py`, `app/html_builder.py`)을 세워뒀지만 실행되지 않았고, 오히려 그 사이 인증·결제·팀 기능이 새로 main.py에 직접 추가됐다.

---

## 2. 목표 아키텍처

Router → Service → Repository/Client 3계층 구조로 재편한다.

```
app/
├── routers/            # FastAPI APIRouter — 라우트 선언 + 요청/응답만, 로직 없음
│   ├── dashboard.py     /api/data /api/groups /api/versions /api/forecast /api/risk-history
│   ├── auth.py          /api/auth/*
│   ├── billing.py       /api/billing/* /api/payment/*
│   ├── team.py          /api/team/*
│   ├── connect.py       /connect /api/connect* /api/disconnect
│   ├── issues.py        /api/issue/* /api/bulk-*
│   ├── callouts.py      /api/callouts*
│   ├── reports.py       /api/report/*
│   ├── ai.py             /api/ai/* /api/insights
│   └── admin.py          /admin /api/admin/*
├── services/            # 비즈니스 로직
│   ├── risk_scoring.py
│   ├── billing_service.py
│   ├── team_service.py
│   └── report_service.py
├── repositories/        # DB 접근 캡슐화 (SQLite/Postgres 분기를 여기 안으로 숨김)
│   ├── user_repo.py
│   ├── session_repo.py
│   ├── connection_repo.py
│   └── callout_repo.py
├── redmine/
│   ├── client.py         RedmineClient — url/key를 인스턴스 상태로 보관
│   └── factory.py         RedmineClientFactory
├── scheduler/
│   ├── jobs.py            현재 main.py 하단의 6개 _job_* 함수
│   └── bootstrap.py
├── schemas/              # Pydantic 요청/응답 모델
├── cache.py
├── ai_service.py         # _call_claude + app/insights.py 통합 검토
├── constants.py          (기존 유지)
└── reporter.py           (기존 유지)
main.py                  # FastAPI() 생성 + include_router + startup 이벤트만 (목표 100줄 이내)
```

---

## 3. 적용 디자인 패턴 매핑

| 문제 | 패턴 | 적용 위치 | 기대 효과 |
|---|---|---|---|
| redmine_url/api_key 파라미터 반복 전파 | **Factory + 캡슐화** | `app/redmine/client.py`, `factory.py` | 함수 시그니처 단순화, 커넥션 객체 재사용 |
| SQLite ↔ Postgres 분기가 라우트에 노출 | **Repository Pattern** | `app/repositories/*.py` | 저장소 교체·단위 테스트 용이, 라우트가 SQL을 몰라도 됨 |
| 리스크 스코어 공식이 2곳에서 각기 다른 변형으로 하드코딩 | **Strategy Pattern** | `app/services/risk_scoring.py` | 공식 변경 시 한 곳만 수정, 향후 플랜별/프로젝트별 스코어링 확장 가능 |
| 포트원 V2 결제 로직이 라우트에 직접 결합 | **Adapter Pattern** | `billing_service.py` + `PaymentGateway` 인터페이스 | 추후 PG사 추가·교체 시 라우트 코드 불변 |
| 세션/권한 체크(`_require_session` 등) | **Dependency Injection** (FastAPI Depends — 이미 부분 적용) | `app/auth/dependencies.py`로 위치만 정리 | 기존에 잘 쓰고 있는 패턴, 파일만 이동 |
| APScheduler 잡 6개가 main.py 하단에 나열 | **Job 등록 패턴** | `app/scheduler/jobs.py` + `bootstrap.py` | 잡 추가/제거 시 main.py 수정 불필요 |
| 요청 바디 수동 파싱 | **DTO(Pydantic 스키마)** | `app/schemas/*.py` | 자동 검증 + 자동 API 문서화, 결제·팀 라우트부터 안정성 확보 |
| 캐시 전역 dict 상태 | **Singleton 명시화** | `app/cache.py`의 `CacheStore` 클래스 | 테스트 시 mock 대체 가능 |

기존에 이미 잘 적용된 패턴도 있다 — `app/constants.py`의 `plan_allows(plan, feature)`는 사실상 플랜별 기능 게이팅을 위한 간단한 Strategy/Policy 형태로, 이 방식을 다른 영역(예: 리스크 스코어링 가중치)에도 일관되게 확장하면 된다.

---

## 4. 단계별 실행 계획 (Strangler Fig — 서비스 중단 없이 점진적 이전)

각 Phase는 별도 세션에서 진행하고, 시작 전 `main_backup_YYYYMMDD.py` 백업, beta 브랜치에서 작업 후 로컬 검증까지 마친다.

**Phase A — 저위험 기계적 분리**
로직 변경 없이 라우트만 도메인별 `APIRouter`로 잘라 옮긴다. 독립성이 높은 것부터: `connect.py` → `callouts.py` → `team.py` → `admin.py`. 각 라우터 이동 후 해당 기능만 로컬에서 스모크 테스트.

**Phase B — Redmine 클라이언트 캡슐화 (가장 레버리지 큰 작업)**
`RedmineClient` 클래스를 도입해 `fetch`/`fetch_all`/`get_issues`/`get_projects`를 메서드로 옮긴다. `redmine_url`, `api_key` 파라미터 스레딩이 사라지고 `build_dashboard_data`, `get_groups`, `get_versions` 등이 `client.method()` 호출로 축소된다. 멀티테넌시 버그가 가장 잘 나는 지점이라 별도로 꼼꼼히 검증.

**Phase C — Repository로 DB 계층 분리**
`_db_conn`, `_PgUsersConn`, `_users_db`를 `UserRepository`, `SessionRepository`, `ConnectionRepository`, `CalloutRepository`로 통합하고 SQLite/Postgres 분기를 리포지토리 내부로 숨긴다.

**Phase D — 리스크 스코어링 & AI를 서비스로 승격**
`risk_scoring.py`에 공식을 통합해 현재 2곳의 중복(프로젝트 스코어 vs 담당자 과부하 스코어)을 공통 계산 함수 + 가중치 설정으로 정리한다. `app/insights.py`와 `_call_claude`를 `app/ai_service.py`로 통합할지 검토.

**Phase E — Pydantic 스키마 도입**
돈·권한이 걸린 라우트부터 우선 적용: 결제 → 팀 초대 → 인증. 이후 나머지 라우트로 확대.

**Phase F — main.py 슬림화**
FastAPI 앱 생성 + `include_router` + startup 이벤트만 남기고 목표 100줄 이내로 축소.

---

## 5. 우선순위 권장

Vantix는 이미 Railway에 배포돼 외부 사용자가 접근 중이고 지금 Phase 4(결제·플랜)를 막 붙인 시점이라, 전면 재작성은 리스크가 크다.

- **런칭 안정화 전:** Phase A(라우터 분리)만 먼저 끝내는 걸 권장. 로직을 안 건드리므로 회귀 위험이 낮고, main.py 가독성이 즉시 좋아진다.
- **Phase B(Redmine 클라이언트 캡슐화)는 예외적으로 우선순위를 당길 가치가 있다.** 멀티테넌시 버그(다른 고객의 redmine_url/api_key가 섞이는 사고)가 나기 가장 쉬운 지점이고, 결제 고객이 늘어날수록 사고 비용이 커지기 때문이다.
- Phase C~F(Repository, 스코어링 통합, Pydantic, main.py 슬림화)는 트래픽·매출이 어느 정도 안정된 뒤 진행해도 무방하다.

---

## 6. 진행 방식 체크리스트

- [ ] Phase 시작 전 `main_backup_YYYYMMDD.py` 백업
- [ ] beta 브랜치에서 작업, 로컬 검증 완료 후에만 main 머지 제안
- [ ] git add/commit/push는 사용자가 명시적으로 지시할 때만 실행
- [ ] Phase 하나당 별도 세션으로 진행 (한 세션에 몰아서 하지 않기)
- [ ] 각 Phase 완료 후 STRUCTURE.md의 "V1.1 예정" 섹션을 실제 진행 상황으로 갱신
