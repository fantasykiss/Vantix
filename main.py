#!/usr/bin/env python3
"""
Redmine 실시간 웹 대시보드
실행: python main.py
접속: http://localhost:8000
"""

import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ==================== 서버 설정 ====================
from config import BASE_URL, API_KEY, ANTHROPIC_API_KEY, EMAIL_CFG, REPORT_DAY, REPORT_HOUR, REPORT_MINUTE, REDMINE_PUBLIC_URL, FERNET_KEY, ADMIN_PASSWORD
import uuid as _uuid
from cryptography.fernet import Fernet, InvalidToken

# ── API Key 암호화 ────────────────────────────────────────────
_fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None

def _encrypt_key(raw: str) -> str:
    if _fernet:
        return _fernet.encrypt(raw.encode()).decode()
    return raw

def _decrypt_key(stored: str) -> str:
    if _fernet:
        try:
            return _fernet.decrypt(stored.encode()).decode()
        except (InvalidToken, Exception):
            return stored  # 기존 평문 세션 호환
    return stored

# ── 세션 스토어 (Postgres 우선, 파일 폴백) ──────────────────
SESSION_TTL = 86400 * 30      # 30일
_SESSION_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
_DATABASE_URL = os.getenv("DATABASE_URL", "")

_db_pool = None
import concurrent.futures as _cf
_db_executor = _cf.ThreadPoolExecutor(max_workers=5, thread_name_prefix="vx-db")

def _get_pool():
    global _db_pool
    if _db_pool is None and _DATABASE_URL:
        import psycopg2.pool
        _db_pool = psycopg2.pool.ThreadedConnectionPool(2, 10, _DATABASE_URL)
    return _db_pool

class _db_conn:
    """커넥션 풀에서 꺼내 쓰고 반납하는 컨텍스트 매니저"""
    def __enter__(self):
        pool = _get_pool()
        self._conn = pool.getconn()
        return self._conn
    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        _get_pool().putconn(self._conn)

async def _adb(fn):
    """동기 DB 함수를 스레드에서 실행 → 이벤트 루프 블로킹 방지"""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(_db_executor, fn)

def _init_session_table():
    if not _DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vantix_sessions (
                        token TEXT PRIMARY KEY,
                        url   TEXT NOT NULL,
                        key   TEXT NOT NULL,
                        created DOUBLE PRECISION NOT NULL
                    )
                """)
            conn.commit()
    except Exception as e:
        print(f"[session] DB 테이블 초기화 실패: {e}")

def _init_analytics_tables():
    if not _DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS analytics_events (
                        id        SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        page      TEXT,
                        element   TEXT,
                        duration  INTEGER,
                        ts        DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vantix_feedback (
                        id        SERIAL PRIMARY KEY,
                        type      TEXT NOT NULL,
                        message   TEXT NOT NULL,
                        name      TEXT,
                        email     TEXT,
                        ip        TEXT,
                        ts        DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("""
                    ALTER TABLE vantix_feedback ADD COLUMN IF NOT EXISTS ip TEXT
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vantix_callouts (
                        id      TEXT PRIMARY KEY,
                        from_name TEXT,
                        date    TEXT,
                        text    TEXT NOT NULL,
                        color   TEXT,
                        done    BOOLEAN DEFAULT FALSE,
                        seen    BOOLEAN DEFAULT FALSE,
                        created DOUBLE PRECISION NOT NULL
                    )
                """)
            conn.commit()
    except Exception as e:
        print(f"[analytics] DB 테이블 초기화 실패: {e}")

def _save_session(token: str, url: str, key: str, created: float):
    encrypted_key = _encrypt_key(key)
    if _DATABASE_URL:
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO vantix_sessions (token, url, key, created) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (token) DO UPDATE SET url=EXCLUDED.url, key=EXCLUDED.key, created=EXCLUDED.created",
                        (token, url, encrypted_key, created)
                    )
                conn.commit()
            return
        except Exception as e:
            print(f"[session] DB 저장 실패: {e}")
    # 파일 폴백
    try:
        data = {}
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, "r") as f:
                data = json.load(f)
        data[token] = {"url": url, "key": encrypted_key, "created": created}
        with open(_SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def _delete_session(token: str):
    _session_cache.pop(token, None)
    if _DATABASE_URL:
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM vantix_sessions WHERE token=%s", (token,))
                conn.commit()
            return
        except Exception as e:
            print(f"[session] DB 삭제 실패: {e}")
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, "r") as f:
                data = json.load(f)
            data.pop(token, None)
            with open(_SESSION_FILE, "w") as f:
                json.dump(data, f)
    except Exception:
        pass

_session_cache: dict = {}   # token → {url, key, created}  ← 만료 전까지 영구 유지
_callout_cache: list | None = None          # GET /api/callouts 결과 메모리 캐시
_callout_cache_ts: float = 0.0
_CALLOUT_CACHE_TTL = 30                     # 30초: 쓰기 시 즉시 무효화되므로 짧아도 OK

def _warm_session_cache():
    """서버 시작 시 DB 세션 전체를 메모리에 로드 — 이후 _get_session은 DB 미접촉"""
    if not _DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT token, url, key, created FROM vantix_sessions WHERE created > %s",
                            (time.time() - SESSION_TTL,))
                rows = cur.fetchall()
        for token, url, key, created in rows:
            _session_cache[token] = {"url": url, "key": _decrypt_key(key), "created": created}
        print(f"[session] 캐시 워밍 완료: {len(rows)}개 세션")
    except Exception as e:
        print(f"[session] 캐시 워밍 실패: {e}")

def _get_session(token: str) -> dict | None:
    if not token:
        return None
    # 메모리 캐시에서 즉시 반환 (DB 미접촉)
    s = _session_cache.get(token)
    if s is not None:
        if time.time() - s["created"] > SESSION_TTL:
            _session_cache.pop(token, None)
            return None
        return s
    # 캐시 미스 → DB 조회 (최초 로그인 또는 서버 재시작 직후)
    if _DATABASE_URL:
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT url, key, created FROM vantix_sessions WHERE token=%s", (token,))
                    row = cur.fetchone()
            if not row:
                return None
            url, key, created = row
            if time.time() - created > SESSION_TTL:
                _delete_session(token)
                return None
            s = {"url": url, "key": _decrypt_key(key), "created": created}
            _session_cache[token] = s
            return s
        except Exception as e:
            print(f"[session] DB 조회 실패: {e}")
    # 파일 폴백
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, "r") as f:
                data = json.load(f)
            s = data.get(token)
            if s and time.time() - s["created"] < SESSION_TTL:
                result = {"url": s["url"], "key": _decrypt_key(s["key"]), "created": s["created"]}
                _session_cache[token] = result
                return result
    except Exception:
        pass
    return None

def _require_session(request: Request) -> dict:
    token = request.cookies.get("vx_session") or request.headers.get("X-VX-Session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401, detail="session_expired")
    return s

_init_session_table()
_init_analytics_tables()
_warm_session_cache()
from app.reporter import build_report_data, render_html_report, render_tsv_report, send_report_email
from app.constants import PROGRESS_SET, RESOLVED_SET, CLOSED_SET, HOLD_SET, DEPT_NORMALIZE, dept_name, short_name
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
# ====================================================

# 그룹명 키워드 매핑
DEPT_PLANNING = "기획"
DEPT_SERVER   = "서버"
DEPT_CLIENT   = "클라"

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

app = FastAPI()
if os.path.isdir("etc"):
    app.mount("/devlog", StaticFiles(directory="etc"), name="devlog")

# ==================== 캐시 ====================
_cache = {}

# ==================== AI 캐시 ====================
_ai_cache: dict = {}
AI_CACHE_TTL = 3600  # 1시간

def _get_ai_cache(key: str):
    entry = _ai_cache.get(key)
    if not entry:
        return None
    if (datetime.now() - entry["at"]).total_seconds() > AI_CACHE_TTL:
        del _ai_cache[key]
        return None
    return entry["data"]

def _set_ai_cache(key: str, data):
    _ai_cache[key] = {"data": data, "at": datetime.now()}

def _call_claude(prompt: str, max_tokens: int = 256) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic 패키지 없음. pip install anthropic 후 재시작하세요.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except anthropic.APIStatusError as e:
        if e.status_code == 529:
            raise RuntimeError("AI 서버가 일시적으로 혼잡합니다. 잠시 후 다시 시도해주세요.")
        if e.status_code == 429:
            raise RuntimeError("AI 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요.")
        raise RuntimeError(f"AI 오류 ({e.status_code}): {e.message}")

# ==================== Rate Limiting ====================
_connect_attempts: dict[str, list] = {}  # ip → [timestamp, ...]
RATE_LIMIT_MAX = 10       # 최대 요청 수
RATE_LIMIT_WINDOW = 600   # 10분 윈도우

def _check_rate_limit(ip: str):
    now = time.time()
    attempts = [t for t in _connect_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="요청이 너무 많습니다. 잠시 후 다시 시도해주세요.")
    attempts.append(now)
    _connect_attempts[ip] = attempts

# ==================== 접속자 관리 ====================
_visitors = {}  # ip → last_seen
VISITOR_TTL = 300  # 5분

CACHE_TTL_SECONDS = 900       # 15분 캐시 유효시간 (stale-while-revalidate 적용으로 늘림)
AUTO_REFRESH_INTERVAL = 1800  # 30분마다 백그라운드 자동갱신
_auto_refresh_params = {}     # key → {project_id, updated_after, redmine_url, api_key}
_bg_refresh_lock: set = set() # 중복 백그라운드 갱신 방지


def get_active_visitors():
    now = datetime.now()
    return {ip: t for ip, t in _visitors.items() if (now - t).total_seconds() < VISITOR_TTL}


def cache_key(project_id, updated_after, redmine_url=""):
    # Redmine URL별로 캐시 분리 — 다른 유저의 Redmine 데이터 혼용 방지
    url_tag = (redmine_url or "").rstrip("/")
    return f"{url_tag}|{project_id}|{updated_after}"


def get_cache_entry(project_id, updated_after, redmine_url=""):
    """full entry {data, fetched_at} 반환 — stale 포함. api_data에서만 사용."""
    key = cache_key(project_id, updated_after, redmine_url)
    return _cache.get(key)


def get_cache(project_id, updated_after, redmine_url=""):
    """data만 반환. 데이터 없으면 None, stale이어도 data 반환 (즉시 응답 우선)."""
    entry = get_cache_entry(project_id, updated_after, redmine_url)
    return entry["data"] if entry else None


def is_cache_fresh(entry) -> bool:
    if not entry:
        return False
    age = (datetime.now() - entry["fetched_at"]).total_seconds()
    return age <= CACHE_TTL_SECONDS


def set_cache(project_id, updated_after, data, redmine_url=""):
    key = cache_key(project_id, updated_after, redmine_url)
    _cache[key] = {"data": data, "fetched_at": datetime.now()}
    print(f"  캐시 저장: {key} ({datetime.now().strftime('%H:%M:%S')})")


def cache_age_str(project_id, updated_after, redmine_url=""):
    key = cache_key(project_id, updated_after, redmine_url)
    entry = _cache.get(key)
    if not entry:
        return None
    age = int((datetime.now() - entry["fetched_at"]).total_seconds())
    if age < 60:
        return f"{age}초 전"
    return f"{age // 60}분 전"

def _job_send_monitor_alerts():
    import json as _json
    monitor_path = os.path.join(os.path.dirname(__file__), "monitor_config.json")
    try:
        with open(monitor_path, "r", encoding="utf-8") as f:
            all_cfg = _json.load(f)
    except:
        return

    for project_id, cfg in all_cfg.items():
        try:
            notify_email = cfg.get("notify_email", "")
            if not notify_email:
                continue

            dashboard = get_cache(project_id, DEFAULT_UPDATED_AFTER)
            if not dashboard:
                dashboard = build_dashboard_data(project_id, DEFAULT_UPDATED_AFTER)

            risks = dashboard.get("project_risk", [])
            if not risks:
                continue

            top = risks[0]
            risk_level = top.get("risk_level", "")
            risk_score = min(round(top.get("risk_score", 0) * 100 / 60), 100)
            overdue = top.get("issues_overdue_count", 0)
            urgent = top.get("issues_urgent_count", 0)

            should_send = False
            if cfg.get("overdue") and overdue > 0:
                should_send = True
            if cfg.get("urgent") and urgent > 0:
                should_send = True
            if cfg.get("critical") and risk_level == "Critical":
                should_send = True

            if not should_send:
                continue

            subject = f"[Vantix] {top['name']} 리스크 알림 — {risk_level} ({risk_score}점)"
            body = f"""Vantix AI 리스크 모니터링 알림입니다.

프로젝트: {top['name']}
리스크 레벨: {risk_level} ({risk_score}점)
마감 초과: {overdue}건
마감 임박: {urgent}건

Vantix 대시보드에서 상세 내용을 확인하세요."""

            from app.reporter import send_report_email
            from config import EMAIL_CFG
            import copy
            cfg_override = copy.copy(EMAIL_CFG)
            cfg_override.recipients = [notify_email]
            cfg_override.enabled = True
            html_body = f"""
    <div style="font-family:sans-serif;padding:24px;max-width:600px;">
      <div style="font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:#888;margin-bottom:8px;">VANTIX AI 리스크 알림</div>
      <div style="font-size:24px;font-weight:700;color:#111;margin-bottom:16px;">{top['name']}</div>
      <div style="display:flex;gap:16px;margin-bottom:16px;">
        <div style="padding:12px 20px;background:#f5f3f3;">
          <div style="font-size:10px;color:#888;text-transform:uppercase;">리스크 레벨</div>
          <div style="font-size:18px;font-weight:700;color:#B40023;">{risk_level}</div>
        </div>
        <div style="padding:12px 20px;background:#f5f3f3;">
          <div style="font-size:10px;color:#888;text-transform:uppercase;">점수</div>
          <div style="font-size:18px;font-weight:700;">{risk_score}점</div>
        </div>
        <div style="padding:12px 20px;background:#f5f3f3;">
          <div style="font-size:10px;color:#888;text-transform:uppercase;">마감초과</div>
          <div style="font-size:18px;font-weight:700;color:#B40023;">{overdue}건</div>
        </div>
      </div>
      <div style="font-size:11px;color:#666;border-left:3px solid #111;padding-left:12px;">
        Vantix 대시보드에서 AI Command Panel을 확인하세요.
      </div>
    </div>
    """
            send_report_email(html_body, subject, cfg_override)
            print(f"  모니터링 알림 발송: {notify_email} ({top['name']})")

        except Exception as e:
            print(f"  모니터링 알림 실패 {project_id}: {e}")


def _job_refresh_cache():
    if not _auto_refresh_params:
        return
    for key, params in list(_auto_refresh_params.items()):
        pid   = params["project_id"]
        uaft  = params["updated_after"]
        r_url = params.get("redmine_url", "")
        r_key = params.get("api_key", "")
        print(f"  자동갱신: {key}")
        try:
            data = build_dashboard_data(pid, uaft, redmine_url=r_url, api_key=r_key)
            set_cache(pid, uaft, data, redmine_url=r_url)
        except Exception as e:
            print(f"  자동갱신 실패: {e}")

def _job_weekly_report():
    print("  주간 리포트 생성 중...")
    try:
        dashboard = get_cache(DEFAULT_PROJECT_ID, DEFAULT_UPDATED_AFTER)
        if not dashboard:
            dashboard = build_dashboard_data(DEFAULT_PROJECT_ID, DEFAULT_UPDATED_AFTER)
        report  = build_report_data(dashboard, project_label=DEFAULT_PROJECT_ID or "전체")
        html    = render_html_report(report)
        subject = f"[Vantix] 주간 리포트 {report.period_label}"
        result  = send_report_email(html, subject, EMAIL_CFG)
        print(f"  {result}")
    except Exception as e:
        print(f"  리포트 오류: {e}")

# ── Risk 스냅샷 저장 ──────────────────────────────────────────
RISK_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "risk_history.json")

def save_risk_snapshot():
    """
    매주 지정 요일에 현재 캐시의 risk_score를 스냅샷으로 저장.
    키: "all" 또는 "project_{id}"
    """
    from datetime import date
    today = date.today().isoformat()

    try:
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    # 캐시에서 project_risk가 있는 첫 항목 탐색
    cached = None
    for entry in _cache.values():
        d = entry.get("data", {})
        if d.get("project_risk"):
            cached = d
            break
    if not cached:
        return  # 캐시 없으면 스킵

    projects = cached.get("project_risk", [])
    if not projects:
        return

    # 전체 평균 스냅샷
    avg_score = round(sum(p["risk_score"] for p in projects) / len(projects), 1)
    avg_level = "Critical" if avg_score >= 30 else "High" if avg_score >= 15 else "Medium" if avg_score >= 5 else "Low"
    avg_overdue = sum(p.get("overdue", 0) for p in projects)
    avg_urgent  = sum(p.get("urgent",  0) for p in projects)
    history.setdefault("all", [])
    if not history["all"] or history["all"][-1]["date"] != today:
        history["all"].append({"date": today, "score": avg_score, "level": avg_level,
                               "overdue": avg_overdue, "urgent": avg_urgent})
    history["all"] = history["all"][-52:]  # 최대 52주 보관

    # 캐시 키에서 project_id → project_name 역매핑 구성
    # 캐시 키(identifier)→project_name 역매핑 구성
    pid_to_name = {}
    for cache_key, entry in _cache.items():
        identifier = cache_key.split("|")[0]
        if not identifier:
            continue
        d = entry.get("data", {})
        if isinstance(d, dict):
            pname = d.get("project_name", "")
            if pname:
                pid_to_name[pname] = identifier

    # 프로젝트별 스냅샷 (키: project_{identifier})
    for p in projects:
        pname = p.get("name", "")
        if not pname:
            continue
        identifier = pid_to_name.get(pname, pname)
        key = f"project_{identifier}"

        history.setdefault(key, [])
        score = round(p["risk_score"], 1)
        level = p["risk_level"]
        if not history[key] or history[key][-1]["date"] != today:
            history[key].append({"date": today, "score": score, "level": level,
                                 "overdue": p.get("overdue", 0), "urgent": p.get("urgent", 0)})
        history[key] = history[key][-52:]

    with open(RISK_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def _job_risk_snapshot():
    print("  리스크 스냅샷 저장 중...")
    try:
        save_risk_snapshot()
        print("  리스크 스냅샷 저장 완료")
    except Exception as e:
        print(f"  스냅샷 오류: {e}")

from config import DEFAULT_PROJECT_ID, DEFAULT_UPDATED_AFTER

_scheduler = BackgroundScheduler(timezone="Asia/Seoul")
_scheduler.add_job(_job_refresh_cache, "interval", minutes=30, id="cache_refresh")
_scheduler.add_job(_job_weekly_report, CronTrigger(
    day_of_week=REPORT_DAY, hour=REPORT_HOUR, minute=REPORT_MINUTE
), id="weekly_report")
_scheduler.add_job(save_risk_snapshot, CronTrigger(
    day_of_week='mon', hour=9, minute=0, timezone="Asia/Seoul"
), id="risk_snapshot")
_scheduler.add_job(_job_send_monitor_alerts, CronTrigger(
    hour=9, minute=5, timezone="Asia/Seoul"
), id="monitor_alerts")
save_risk_snapshot()  # 서버 시작 시 즉시 1회 실행
_scheduler.start()
print(f"  스케줄러 시작!")


# ==================== API 유틸 ====================

def get_current_user_email():
    try:
        data = fetch("/users/current.json")
        return data.get("user", {}).get("mail", "")
    except:
        return ""

def fetch(path, params=None, retries=2, redmine_url=None, api_key=None):
    _url = (redmine_url or BASE_URL).rstrip("/") + path
    _key = api_key or API_KEY
    if params:
        _url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(_url, headers={"X-Redmine-API-Key": _key})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < retries - 1:
                print(f"  재시도 ({attempt+1}/{retries-1}): offset {params.get('offset','?') if params else '?'}")
                time.sleep(1)
            else:
                print(f"  요청 실패: {_url}\n     {e}")
    return {}


def fetch_all(path, key, base_params=None, redmine_url=None, api_key=None):
    """첫 페이지로 total_count 파악 후 나머지 페이지를 병렬로 fetch"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    limit = 100

    # 1) 첫 페이지 fetch → total_count 확인
    first_params = {**(base_params or {}), "limit": limit, "offset": 0}
    first_data = fetch(path, first_params, redmine_url=redmine_url, api_key=api_key)
    items = list(first_data.get(key, []))
    total = first_data.get("total_count", len(items))

    if total <= limit:
        return items

    # 2) 나머지 오프셋 계산
    offsets = list(range(limit, total, limit))
    print(f"  병렬 fetch: {len(offsets)+1}페이지 / 총 {total}건")

    def fetch_page(offset):
        params = {**(base_params or {}), "limit": limit, "offset": offset}
        data = fetch(path, params, redmine_url=redmine_url, api_key=api_key)
        return data.get(key, [])

    # 3) 병렬 실행 (max_workers=5)
    pages = [None] * len(offsets)
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(fetch_page, off): i for i, off in enumerate(offsets)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                pages[idx] = future.result()
            except Exception as e:
                print(f"  페이지 fetch 실패 (offset={offsets[idx]}): {e}")
                pages[idx] = []

    for page in pages:
        if page:
            items += page

    return items


def days_diff(due_date_str):
    if not due_date_str:
        return None
    try:
        return (datetime.strptime(due_date_str, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def get_projects(redmine_url=None, api_key=None):
    data = fetch("/projects.json", {"limit": 100}, redmine_url=redmine_url, api_key=api_key)
    return data.get("projects", [])


def get_issues(project_id="", updated_after="2026-03-01", redmine_url=None, api_key=None):
    params = {"status_id": "*"}
    if project_id:
        params["project_id"] = project_id
    if updated_after:
        params["updated_on"] = f">={updated_after}T00:00:00Z"
    return fetch_all("/issues.json", "issues", params, redmine_url=redmine_url, api_key=api_key)


def build_dashboard_data(project_id="", updated_after="2026-03-01", redmine_url=None, api_key=None):
    issues = get_issues(project_id, updated_after, redmine_url=redmine_url, api_key=api_key)

    # 그룹 API 직접 조회로 user → group 매핑 (이름 형식 무관)
    user_group_map = {}
    if project_id:
        try:
            _, user_group_map = _build_group_map(project_id, redmine_url, api_key)
        except Exception:
            pass

    users_data = defaultdict(lambda: {"issues": [], "projects": set(), "group": ""})
    for iss in issues:
        if "assigned_to" not in iss:
            continue
        uname = iss["assigned_to"]["name"]
        if not users_data[uname]["group"]:
            users_data[uname]["group"] = user_group_map.get(uname, "")
        if not users_data[uname].get("short_name"):
            # 그룹명 토큰을 이름에서 제거 (예: "기획_홍길동" → "홍길동", "김주완 클라" → "김주완 클라")
            group = user_group_map.get(uname, "")
            if group and "_" in uname:
                parts = uname.replace("_", " ").split()
                users_data[uname]["short_name"] = " ".join(p for p in parts if p.strip("_") != group.strip("_")) or uname
            else:
                users_data[uname]["short_name"] = uname
        users_data[uname]["issues"].append({
            "id":         iss["id"],
            "subject":    iss["subject"],
            "status":     iss["status"]["name"],
            "priority":   iss["priority"]["name"],
            "project":    iss["project"]["name"],
            "tracker":    iss.get("tracker", {}).get("name", "-"),
            "updated_on": iss.get("updated_on", ""),
            "created_on": iss.get("created_on", ""),
            "due_date":   iss.get("due_date", ""),
            "assignee":   uname,
        })
        users_data[uname]["projects"].add(iss["project"]["name"])

    all_statuses = [i["status"] for ud in users_data.values() for i in ud["issues"]]
    open_issues  = sum(1 for s in all_statuses if s not in CLOSED_SET and s not in HOLD_SET)

    # 오픈 이슈 1건 이상 보유 담당자 수 + 미배정 담당자 이름 목록
    users_active = 0
    users_idle_names = []
    for uname, ud in users_data.items():
        has_open = any(i["status"] not in CLOSED_SET and i["status"] not in HOLD_SET for i in ud["issues"])
        if has_open:
            users_active += 1
        else:
            users_idle_names.append(ud.get("short_name", uname))

    today_str = date.today().strftime("%Y-%m-%d")

    def is_overdue(i):
        return (i["due_date"] and i["due_date"] < today_str
                and i["status"] not in CLOSED_SET
                and i["status"] not in HOLD_SET)

    overdue_total    = 0
    overdue_planning = 0
    overdue_server   = 0
    overdue_client   = 0
    pending_planning = 0
    pending_server   = 0
    pending_client   = 0

    for uname, ud in users_data.items():
        dept = dept_name(uname)
        for i in ud["issues"]:
            if is_overdue(i):
                overdue_total += 1
                if DEPT_PLANNING in dept:
                    overdue_planning += 1
                elif DEPT_SERVER in dept:
                    overdue_server += 1
                elif DEPT_CLIENT in dept:
                    overdue_client += 1
            if i["status"] in {"진행대기", "진행 대기"}:
                if DEPT_PLANNING in dept:
                    pending_planning += 1
                elif DEPT_SERVER in dept:
                    pending_server += 1
                elif DEPT_CLIENT in dept:
                    pending_client += 1

    # ── 프로젝트별 위험도 계산 ──
    project_risk = {}

    # 미할당 이슈가 있는 프로젝트도 color bar에 표시되도록 전체 이슈 기준으로 먼저 등록
    for iss in issues:
        pname = iss["project"]["name"]
        if pname not in project_risk:
            project_risk[pname] = {
                "name": pname,
                "identifier": iss["project"].get("identifier", ""),
                "overdue": 0, "urgent": 0,
                "pending": 0, "open": 0, "total": 0,
                "issues_overdue": [], "issues_urgent": [], "issues_pending": [],
            }

    for uname, ud in users_data.items():
        for i in ud["issues"]:
            pname = i["project"]
            if pname not in project_risk:
                project_risk[pname] = {
                    "name": pname, "overdue": 0, "urgent": 0,
                    "pending": 0, "open": 0, "total": 0,
                    "issues_overdue": [], "issues_urgent": [], "issues_pending": [],
                }
            pr = project_risk[pname]
            pr["total"] += 1
            if i["status"] not in CLOSED_SET:
                pr["open"] += 1
            if is_overdue(i):
                pr["overdue"] += 1
                pr["issues_overdue"].append({"id": i["id"], "subject": i["subject"], "assignee": uname, "due_date": i["due_date"], "status": i["status"], "priority": i["priority"]})
            if i["status"] in {"진행대기", "진행 대기"}:
                pr["pending"] += 1
                pr["issues_pending"].append({"id": i["id"], "subject": i["subject"], "assignee": uname, "due_date": i["due_date"], "status": i["status"], "priority": i["priority"]})
            diff = days_diff(i["due_date"])
            if diff is not None and 0 <= diff <= 3 and i["status"] not in CLOSED_SET and i["status"] not in HOLD_SET:
                pr["urgent"] += 1
                pr["issues_urgent"].append({"id": i["id"], "subject": i["subject"], "assignee": uname, "due_date": i["due_date"], "status": i["status"], "priority": i["priority"]})

    # 위험도 점수 계산
    for pr in project_risk.values():
        total = pr["total"] or 1
        score = round(
            (pr["overdue"] / total * 60) +
            (pr["urgent"]  / total * 30) +
            (pr["pending"] / total * 10),
            1
        )
        if score >= 30:
            pr["risk_level"] = "Critical"
            pr["risk_color"] = "#f87171"
            pr["risk_icon"]  = "🔴"
        elif score >= 15:
            pr["risk_level"] = "High"
            pr["risk_color"] = "#fb923c"
            pr["risk_icon"]  = "🟠"
        elif score >= 5:
            pr["risk_level"] = "Medium"
            pr["risk_color"] = "#fbbf24"
            pr["risk_icon"]  = "⚠️"
        else:
            pr["risk_level"] = "Low"
            pr["risk_color"] = "#34d399"
            pr["risk_icon"]  = "🟢"
        pr["risk_score"] = score

    project_risk_list = sorted(project_risk.values(), key=lambda x: -x["risk_score"])

    # ── 마감 임박 이슈 (오늘 ~ D+3) ──
    future_3 = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    imminent_issues = []
    for uname, ud in users_data.items():
        for i in ud.get("issues", []):
            if (i.get("due_date") and
                today_str <= i["due_date"] <= future_3 and
                i["status"] not in CLOSED_SET and
                i["status"] not in HOLD_SET):
                imminent_issues.append({
                    **i,
                    "assignee": uname,
                    "assignee_short": short_name(uname),
                    "dept": dept_name(uname),
                })
    imminent_issues.sort(key=lambda x: x.get("due_date", ""))

    # ── 7일 트렌드 ──
    today = date.today()
    open_by_day = []
    overdue_by_day = []
    labels = []
    for delta in range(6, -1, -1):  # 6 days ago to today
        d = today - timedelta(days=delta)
        d_str = d.strftime("%Y-%m-%d")
        labels.append(d_str)
        day_open = 0
        day_overdue = 0
        for uname, ud in users_data.items():
            for i in ud.get("issues", []):
                if i["status"] not in CLOSED_SET:
                    day_open += 1
                    if i.get("due_date") and i["due_date"] < d_str and i["status"] not in HOLD_SET:
                        day_overdue += 1
        open_by_day.append(day_open)
        overdue_by_day.append(day_overdue)

    delta_open = open_by_day[-1] - open_by_day[0]
    delta_overdue = overdue_by_day[-1] - overdue_by_day[0]

    trend_7days = {
        "labels": labels,
        "open": open_by_day,
        "overdue": overdue_by_day,
        "delta_open": delta_open,
        "delta_overdue": delta_overdue,
    }

    return {
        "users":            len(users_data),
        "users_active":     users_active,
        "users_idle_names": users_idle_names,
        "total_issues":     len(issues),
        "open_issues":      open_issues,
        "overdue":          overdue_total,
        "overdue_planning": overdue_planning,
        "overdue_server":   overdue_server,
        "overdue_client":   overdue_client,
        "pending_planning": pending_planning,
        "pending_server":   pending_server,
        "pending_client":   pending_client,
        "project_risk":     project_risk_list,
        "redmine_url":      redmine_url or "",
        "redmine_public_url": REDMINE_PUBLIC_URL or redmine_url or "",
        "users_data":       {k: {"issues": v["issues"], "projects": list(v["projects"]), "group": v.get("group", ""), "short_name": v.get("short_name", k)} for k, v in users_data.items()},
        "imminent_count":   len(imminent_issues),
        "imminent_issues":  imminent_issues,
        "trend_7days":      trend_7days,
    }


def _build_group_map(project_id, redmine_url, api_key):
    """
    프로젝트 멤버십 → 그룹 목록 확보 → 그룹별 멤버 API 호출
    반환: (group_map, user_to_group)
      group_map    : {group_name: {id, name, user_count, members}}
      user_to_group: {user_name: group_name}  — 이름 형식 무관
    """
    memberships = fetch(
        f"/projects/{project_id}/memberships.json",
        redmine_url=redmine_url, api_key=api_key
    ).get("memberships", [])

    group_map = {}
    for m in memberships:
        grp = m.get("group")
        if grp and grp["name"] not in group_map:
            group_map[grp["name"]] = {"id": grp["id"], "name": grp["name"], "user_count": 0, "members": []}

    # 각 그룹의 실제 멤버를 Redmine 그룹 API로 가져옴 (이름 형식 무관)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def fetch_members(gname):
        try:
            data = fetch(f"/groups/{group_map[gname]['id']}.json?include=users",
                         redmine_url=redmine_url, api_key=api_key)
            return gname, [u["name"] for u in data.get("group", {}).get("users", []) if u.get("name")]
        except Exception:
            return gname, []

    with ThreadPoolExecutor(max_workers=5) as ex:
        for gname, members in ex.map(lambda g: fetch_members(g), group_map):
            group_map[gname]["members"] = members
            group_map[gname]["user_count"] = len(members)

    user_to_group = {uname: gname for gname, ginfo in group_map.items() for uname in ginfo["members"]}
    return group_map, user_to_group


def get_groups(project_id="", redmine_url=None, api_key=None):
    """
    프로젝트 그룹 카드 목록 + 8주 오버듀 스파크라인
    그룹 멤버는 Redmine 그룹 API 기준 (이름 형식 무관)
    """
    if not project_id:
        return []
    try:
        from datetime import date, timedelta

        # 1. 그룹-멤버 매핑 (API 직접 조회)
        group_map, user_to_group = _build_group_map(project_id, redmine_url, api_key)

        if not group_map:
            return []

        # 2. 이슈 전체 로드
        issues = get_issues(project_id, updated_after="2024-01-01", redmine_url=redmine_url, api_key=api_key)

        # 3. 담당자 → 그룹 (딕셔너리 직접 조회, 이름 규칙 무관)
        def extract_group(assignee_name):
            return user_to_group.get(assignee_name)

        # 4. 8주 역산 계산
        today = date.today()
        days_since_thu = (today.weekday() - 3) % 7  # 목요일 기준
        this_week_start = today - timedelta(days=days_since_thu)
        WEEKS = 8

        today_str = today.strftime("%Y-%m-%d")
        results = []
        unclassified_issues = []

        for gname, ginfo in group_map.items():
            spark = []
            group_total = 0
            for w in range(WEEKS - 1, -1, -1):
                week_end = (this_week_start - timedelta(weeks=w) + timedelta(days=6)).strftime("%Y-%m-%d")
                overdue_count = 0
                for i in issues:
                    assignee = i.get("assigned_to", {}).get("name", "")
                    if extract_group(assignee) != gname:
                        continue
                    status = i.get("status", {}).get("name", "")
                    if status in CLOSED_SET or status in HOLD_SET:
                        continue
                    if w == 0:
                        group_total += 1
                    due = i.get("due_date", "")
                    if due and due < week_end:
                        overdue_count += 1
                spark.append(overdue_count)

            overdue_now = spark[-1] if spark else 0
            overdue_prev = spark[-2] if len(spark) >= 2 else 0
            wow = overdue_now - overdue_prev

            if overdue_now >= 5:
                risk = "Critical"
            elif overdue_now >= 2:
                risk = "High"
            else:
                risk = "Stable"

            results.append({
                "id":           ginfo["id"],
                "name":         gname,
                "user_count":   ginfo["user_count"],
                "members":      ginfo.get("members", []),
                "total_issues": group_total,
                "overdue_now":  overdue_now,
                "overdue_wow":  wow,
                "risk":         risk,
                "spark":        spark,
                "is_extra":     False,
            })

        # 미분류 이슈 수집 (extract_group이 None인 담당자)
        for i in issues:
            assignee = i.get("assigned_to", {}).get("name", "")
            if extract_group(assignee) is None:
                status = i.get("status", {}).get("name", "")
                if status not in CLOSED_SET and status not in HOLD_SET:
                    unclassified_issues.append(i)

        if unclassified_issues:
            uc_overdue = sum(
                1 for i in unclassified_issues
                if i.get("due_date", "") and i.get("due_date", "") < today_str
            )
            uc_members = list({i.get("assigned_to", {}).get("name", "") for i in unclassified_issues if i.get("assigned_to")})
            results.append({
                "id":           None,
                "name":         "기타",
                "user_count":   len(uc_members),
                "members":      uc_members,
                "total_issues": len(unclassified_issues),
                "overdue_now":  uc_overdue,
                "overdue_wow":  0,
                "risk":         "High" if uc_overdue >= 2 else "Stable",
                "spark":        [],
                "is_extra":     True,
            })

        # 오버듀 내림차순 정렬, 기타 카드는 항상 마지막
        results.sort(key=lambda x: (x["is_extra"], -x["overdue_now"]))
        return results

    except Exception as e:
        print(f"[get_groups error] {e}")
        return []


def get_versions(project_id="", redmine_url=None, api_key=None):
    if not project_id:
        projects = get_projects(redmine_url=redmine_url, api_key=api_key)
        versions = []
        for p in projects:
            data = fetch(f"/projects/{p['identifier']}/versions.json", redmine_url=redmine_url, api_key=api_key)
            for v in data.get("versions", []):
                v["project_name"] = p["name"]
                versions.append(v)
        return versions
    else:
        data = fetch(f"/projects/{project_id}/versions.json", redmine_url=redmine_url, api_key=api_key)
        versions = data.get("versions", [])
        for v in versions:
            v["project_name"] = project_id
        return versions


def get_version_issues(version_id, redmine_url=None, api_key=None):
    return fetch_all("/issues.json", "issues", {
        "status_id": "*",
        "fixed_version_id": version_id,
    }, redmine_url=redmine_url, api_key=api_key)


def build_version_data(project_id="", redmine_url=None, api_key=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    versions = get_versions(project_id, redmine_url=redmine_url, api_key=api_key)
    today_str = date.today().strftime("%Y-%m-%d")
    active_versions = [v for v in versions if v.get("status") != "closed"]

    def fetch_version(v):
        issues_raw = get_version_issues(v["id"], redmine_url=redmine_url, api_key=api_key)
        issues = []
        for iss in issues_raw:
            assignee = iss.get("assigned_to", {}).get("name", "") if "assigned_to" in iss else ""
            issues.append({
                "id":          iss["id"],
                "subject":     iss["subject"],
                "status":      iss["status"]["name"],
                "priority":    iss["priority"]["name"],
                "project":     iss["project"]["name"],
                "tracker":     iss.get("tracker", {}).get("name", "-"),
                "due_date":    iss.get("due_date", ""),
                "created_on":  iss.get("created_on", ""),
                "updated_on":  iss.get("updated_on", ""),
                "assignee":    assignee,
                "done_ratio":  iss.get("done_ratio", 0),  # ← Redmine 개별 완료율
            })
        total    = len(issues)
        closed   = sum(1 for i in issues if i["status"] in CLOSED_SET)
        resolved = sum(1 for i in issues if i["status"] in RESOLVED_SET)
        progress = sum(1 for i in issues if i["status"] in PROGRESS_SET)
        overdue  = sum(1 for i in issues
                       if i["due_date"] and i["due_date"] < today_str
                       and i["status"] not in CLOSED_SET
                       and i["status"] not in HOLD_SET)

        # Redmine과 동일한 방식: 각 이슈 done_ratio 평균
        # closed/resolved 이슈는 done_ratio=100으로 간주
        if total:
            total_ratio = sum(
                100 if i["status"] in CLOSED_SET | RESOLVED_SET else i["done_ratio"]
                for i in issues
            )
            done_pct = round(total_ratio / total)
        else:
            done_pct = 0

        return {
            "id":           v["id"],
            "name":         v["name"],
            "project_name": v.get("project_name", ""),
            "due_date":     v.get("due_date", ""),
            "status":       v.get("status", ""),
            "total":        total,
            "closed":       closed,
            "resolved":     resolved,
            "progress":     progress,
            "overdue":      overdue,
            "done_pct":     done_pct,
            "issues":       issues,
        }

    result = []
    print(f"  버전 병렬 로드: {len(active_versions)}개")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_version, v): v for v in active_versions}
        for future in as_completed(futures):
            try:
                r = future.result()
                result.append(r)
            except Exception as e:
                import traceback
                print(f"  버전 로드 실패: {e}")
                traceback.print_exc()

    result = [r for r in result if r is not None]
    result.sort(key=lambda x: x.get("name", "").lower())
    return result


# ==================== HTML PAGE ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    from config import DEFAULT_UPDATED_AFTER, DEFAULT_PROJECT_ID
    token = request.cookies.get("vx_session")
    s = _get_session(token or "")
    if not s:
        return RedirectResponse(url="/connect")
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__REDMINE_BASE_URL__", s["url"])
    html = html.replace("__REDMINE_API_KEY__", s["key"])
    html = html.replace("__DEFAULT_UPDATED_AFTER__", DEFAULT_UPDATED_AFTER)
    html = html.replace("__DEFAULT_PROJECT_ID__", DEFAULT_PROJECT_ID or "")
    html = html.replace("__REDMINE_PUBLIC_URL__", REDMINE_PUBLIC_URL or s["url"])
    return HTMLResponse(content=html)

@app.get("/api/projects")
async def api_projects(request: Request, s: dict = Depends(_require_session)):
    projects = get_projects(redmine_url=s["url"], api_key=s["key"])
    return [{"identifier": p["identifier"], "name": p["name"]} for p in projects]


@app.get("/api/data")
async def api_data(request: Request, project_id: str = "", updated_after: str = "2026-03-01", force: bool = False, s: dict = Depends(_require_session)):
    r_url = s["url"]
    r_key = s["key"]
    key = cache_key(project_id, updated_after, r_url)
    _auto_refresh_params[key] = {"project_id": project_id, "updated_after": updated_after, "redmine_url": r_url, "api_key": r_key}

    if not force:
        entry = get_cache_entry(project_id, updated_after, r_url)
        if entry:
            age = cache_age_str(project_id, updated_after, r_url)
            if is_cache_fresh(entry):
                print(f"  캐시 히트(신선): {key} ({age})")
                return {**entry["data"], "cached": True, "cache_age": age}
            # 만료됐지만 데이터 있음 → 즉시 반환 + 백그라운드 갱신 (stale-while-revalidate)
            if key not in _bg_refresh_lock:
                _bg_refresh_lock.add(key)
                import threading as _t
                def _bg_refresh(pid, uaft, ru, rk, ckey):
                    try:
                        print(f"  백그라운드 갱신 시작: {ckey}")
                        fresh = build_dashboard_data(pid, uaft, redmine_url=ru, api_key=rk)
                        set_cache(pid, uaft, fresh, redmine_url=ru)
                        print(f"  백그라운드 갱신 완료: {ckey}")
                    except Exception as e:
                        print(f"  백그라운드 갱신 실패 {ckey}: {e}")
                    finally:
                        _bg_refresh_lock.discard(ckey)
                _t.Thread(target=_bg_refresh, args=(project_id, updated_after, r_url, r_key, key), daemon=True).start()
            print(f"  캐시 히트(stale): {key} ({age})")
            return {**entry["data"], "cached": True, "cache_age": age, "stale": True}

    print(f"  Redmine fetch: {key}")
    data = build_dashboard_data(project_id, updated_after, redmine_url=r_url, api_key=r_key)
    set_cache(project_id, updated_after, data, redmine_url=r_url)

    import threading as _threading
    def _bg_generate_signals(pid, ru):
        try:
            cache_k = f"action-signals|{ru.rstrip('/')}|{pid}"
            if _get_ai_cache(cache_k):
                return
            risks = data.get("project_risk", [])[:5]
            if not risks:
                return
            risk_lines = "\n".join([
                f"- {r['name']}: score={min(round(r['risk_score']*100/60),100)}, "
                f"level={r['risk_level']}, overdue={r.get('issues_overdue_count',0)}, "
                f"urgent={r.get('issues_urgent_count',0)}"
                for r in risks
            ])
            prompt = (
                "아래 프로젝트 리스크 데이터를 분석해서 JSON 배열만 반환해줘. 설명 없이 JSON만.\n"
                "반드시 3~5개 액션을 생성해야 해.\n"
                "각 항목 형식: {\"project\": \"프로젝트명\", \"priority\": \"P1\"|\"P2\"|\"P3\", "
                "\"timing\": \"IMMEDIATE\"|\"THIS WEEK\"|\"NEXT SPRINT\", "
                "\"action\": \"구체적 액션 한 줄(한국어, ~가 필요합니다 또는 ~을 권장합니다 형태)\", "
                "\"reason\": \"원인 한 줄(한국어, 데이터 근거 포함)\", "
                "\"who\": \"담당자 역할\"}\n"
                f"리스크 데이터:\n{risk_lines}\n"
                "JSON 배열만, 마크다운 코드블록 없이, 반드시 3개 이상"
            )
            import re as _re
            raw = _call_claude(prompt, max_tokens=800)
            match = _re.search(r'\[.*\]', raw, _re.DOTALL)
            signals = json.loads(match.group()) if match else []
            if signals:
                _set_ai_cache(cache_k, json.dumps(signals, ensure_ascii=False))
                print(f"  action-signals 프리생성 완료: {pid}")
        except Exception as e:
            print(f"  action-signals 프리생성 실패 {pid}: {e}")

    _threading.Thread(target=_bg_generate_signals, args=(project_id, r_url), daemon=True).start()

    return {**data, "cached": False, "cache_age": None}


@app.get("/api/groups")
async def api_groups(request: Request, project_id: str = "", s: dict = Depends(_require_session)):
    if not project_id:
        return {"groups": []}
    groups = get_groups(project_id, redmine_url=s["url"], api_key=s["key"])
    return {"groups": groups}


@app.get("/api/versions")
async def api_versions(request: Request, project_id: str = "", force: bool = False, s: dict = Depends(_require_session)):
    vkey = f"versions|{s['url'].rstrip('/')}|{project_id}"
    if not force:
        entry = _cache.get(vkey)
        if entry:
            age = (datetime.now() - entry["fetched_at"]).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return entry["data"]
    data = build_version_data(project_id, redmine_url=s["url"], api_key=s["key"])
    if data:  # 빈 결과는 캐시하지 않음
        _cache[vkey] = {"data": data, "fetched_at": datetime.now()}
    return data


@app.get("/api/forecast")
async def api_forecast(request: Request, project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    # 대시보드 캐시 우선 사용
    dashboard = get_cache(project_id, updated_after, s["url"])
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
        set_cache(project_id, updated_after, dashboard, redmine_url=s["url"])

    # ── 지표 1: 차주 지연 예상 이슈 수 (현재 overdue + D-3 이내 이슈)
    project_risk = dashboard.get("project_risk", [])
    delay_predicted = sum(p.get("overdue", 0) + p.get("urgent", 0) for p in project_risk)

    # ── 지표 2: 완료 위험 마일스톤 수 (마감 14일 이내 & done_pct < 80%)
    at_risk_milestones = 0
    vkey = f"versions|{s['url'].rstrip('/')}|{project_id}"
    version_data = None
    entry = _cache.get(vkey)
    if entry and (datetime.now() - entry["fetched_at"]).total_seconds() < CACHE_TTL_SECONDS:
        version_data = entry["data"]
    if not version_data:
        try:
            version_data = build_version_data(project_id, redmine_url=s["url"], api_key=s["key"])
            if version_data:
                _cache[vkey] = {"data": version_data, "fetched_at": datetime.now()}
        except Exception:
            version_data = []
    for v in (version_data or []):
        due = v.get("due_date", "")
        diff = days_diff(due) if due else None
        if diff is not None and diff <= 14 and v.get("done_pct", 0) < 80:
            at_risk_milestones += 1

    # ── 지표 3: 전주 대비 리스크 변화
    risk_change    = None
    prev_overdue   = None
    prev_urgent    = None
    prev_date      = None
    try:
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
            hist = json.load(f)
        proj_key = f"project_{project_id}" if project_id else "all"
        proj_hist = hist.get(proj_key, hist.get("all", []))
        if len(proj_hist) >= 2:
            prev = proj_hist[-2]
            risk_change  = round(proj_hist[-1]["score"] - prev["score"], 1)
            prev_overdue = prev.get("overdue")
            prev_urgent  = prev.get("urgent")
            prev_date    = prev.get("date")
    except Exception:
        pass

    # ── 담당자 과부하 (오픈 이슈 집중도 기준)
    users_data = dashboard.get("users_data", {})
    overload_raw = []
    for uname, ud in users_data.items():
        issues = ud.get("issues", [])
        open_count = sum(1 for i in issues if i["status"] not in CLOSED_SET and i["status"] not in HOLD_SET)
        if open_count > 0:
            overload_raw.append({"name": ud.get("short_name", uname), "open": open_count})
    overload_raw.sort(key=lambda x: -x["open"])
    max_open = overload_raw[0]["open"] if overload_raw else 1
    overload = []
    for x in overload_raw[:5]:
        pct = round(x["open"] / max_open * 100)
        overload.append({
            "name": x["name"], "open": x["open"], "pct": pct,
            "level": "danger" if pct >= 70 else ("warning" if pct >= 40 else "ok"),
            "label": "과부하" if pct >= 70 else ("주의" if pct >= 40 else "여유"),
        })

    metrics = {
        "delay_predicted":  delay_predicted,
        "at_risk_milestones": at_risk_milestones,
        "risk_change":      risk_change,
        "prev_overdue":     prev_overdue,
        "prev_urgent":      prev_urgent,
        "prev_date":        prev_date,
    }

    if not project_id:
        # ── 전체 프로젝트 뷰: 프로젝트별 차주 위험 예측 ──
        items = []
        for p in project_risk[:6]:
            lvl = p["risk_level"]
            parts = []
            if p.get("overdue", 0) > 0:
                parts.append(f"마감 초과 {p['overdue']}건")
            if p.get("urgent", 0) > 0:
                parts.append(f"D-3 이내 {p['urgent']}건")
            items.append({
                "name":       p["name"],
                "identifier": p.get("identifier", ""),
                "reason":     " · ".join(parts) if parts else "이슈 없음, 안정",
                "badge":      "지연 위험" if lvl in ("Critical", "High") else ("주의 필요" if lvl == "Medium" else "안정"),
                "badge_class": "danger" if lvl in ("Critical", "High") else ("warning" if lvl == "Medium" else "safe"),
                "dot_class":  "high" if lvl in ("Critical", "High") else ("medium" if lvl == "Medium" else "low"),
                "done_pct":   None,
                "overdue":    p.get("overdue", 0),
                "urgent":     p.get("urgent",  0),
                "total":      p.get("total",   0),
                "risk_score": round(p.get("risk_score", 0), 1),
            })
        return {
            "type": "all",
            "metrics": metrics,
            "items_label": "프로젝트별 차주 위험 예측",
            "items": items,
            "overload": overload,
        }
    else:
        # ── 단일 프로젝트 뷰: 마일스톤/버전별 위험 예측 ──
        items = []
        for v in (version_data or [])[:6]:
            overdue = v.get("overdue", 0)
            done = v.get("done_pct", 0)
            due = v.get("due_date", "")
            diff = days_diff(due) if due else None
            total = v.get("total", 0)
            open_count = total - v.get("closed", 0) - v.get("resolved", 0)

            if overdue > 0 or (diff is not None and diff <= 3 and done < 80):
                badge, badge_class, dot_class = "지연 위험", "danger", "high"
            elif diff is not None and diff <= 14 and done < 80:
                badge, badge_class, dot_class = "주의 필요", "warning", "medium"
            else:
                badge, badge_class, dot_class = "안정", "safe", "low"

            parts = []
            if due:
                parts.append(f"마감 {due}")
            parts.append(f"미완료 {open_count}/{total}건")

            items.append({
                "name": v["name"],
                "reason": " · ".join(parts),
                "badge": badge,
                "badge_class": badge_class,
                "dot_class": dot_class,
                "done_pct": done,
            })

        if not items:
            for p in project_risk[:4]:
                lvl = p["risk_level"]
                items.append({
                    "name": p["name"],
                    "reason": f"마감 초과 {p.get('overdue',0)}건 · D-3 {p.get('urgent',0)}건",
                    "badge": "지연 위험" if lvl in ("Critical","High") else ("주의 필요" if lvl=="Medium" else "안정"),
                    "badge_class": "danger" if lvl in ("Critical","High") else ("warning" if lvl=="Medium" else "safe"),
                    "dot_class": "high" if lvl in ("Critical","High") else ("medium" if lvl=="Medium" else "low"),
                    "done_pct": None,
                })

        return {
            "type": "project",
            "metrics": metrics,
            "items_label": "마일스톤 / 버전별 차주 위험 예측",
            "items": items,
            "overload": overload,
        }


@app.get("/api/cache/clear")
async def clear_cache(s: dict = Depends(_require_session)):
    _cache.clear()
    return {"ok": True, "message": "캐시 초기화 완료"}


@app.get("/api/risk-history")
async def api_risk_history(project_id: str = "", weeks: int = 12, s: dict = Depends(_require_session)):
    """
    저장된 스냅샷에서 최근 N주 반환.
    스냅샷 없으면 빈 배열 반환 (역산 제거).
    """
    try:
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"history": []}

    if not project_id:
        key = "all"
    else:
        all_keys = list(history.keys())
        proj_keys = [k for k in all_keys if k.startswith("project_")]
        direct_key = f"project_{project_id}"
        if direct_key in history:
            key = direct_key
        else:
            # identifier 기반 부분 매칭 시도 (예: nd_project → project_nd_project)
            matched = [k for k in history if project_id in k]
            if matched:
                key = matched[0]
            else:
                try:
                    dashboard = build_dashboard_data(project_id, "2020-01-01")
                    pname = dashboard.get("project_name", "")
                    name_key = f"project_{pname}"
                    key = name_key if name_key in history else "all"
                except Exception:
                    key = "all"
    records = history.get(key, [])

    # 프로젝트 데이터가 너무 적으면 "all" 키로 폴백 (레드마인 인스턴스 변경 등으로 키가 달라진 경우 대응)
    if key != "all" and len(records) < 3:
        records = history.get("all", [])

    # 월요일 기준 주별 그룹핑 — 같은 주의 마지막(최신) 스냅샷만 사용
    from datetime import date as _date
    week_map = {}
    for r in records:
        try:
            d = _date.fromisoformat(r["date"])
            # 해당 날짜의 월요일 구하기 (weekday: 0=월)
            monday = d - timedelta(days=d.weekday())
            week_key = monday.isoformat()
            week_map[week_key] = r  # 같은 주면 나중 것(최신)으로 덮어쓰기
        except Exception:
            continue

    # 월요일 날짜 기준 오름차순 정렬 후 최근 N주만 사용
    sorted_weeks = sorted(week_map.items())[-weeks:]

    # All Projects 뷰일 때 주별 프로젝트 breakdown 수집
    proj_week_map = {}  # monday_str → {proj_key: record}
    if key == "all":
        for hkey, hrecords in history.items():
            if not hkey.startswith("project_"):
                continue
            proj_name = hkey[len("project_"):]
            for pr in hrecords:
                try:
                    pd = _date.fromisoformat(pr["date"])
                    pm = pd - timedelta(days=pd.weekday())
                    pm_str = pm.isoformat()
                    proj_week_map.setdefault(pm_str, {})[proj_name] = pr
                except Exception:
                    continue

    result = []
    for idx, (monday_str, r) in enumerate(sorted_weeks, 1):
        item = {
            "week": f"W{idx}",
            "score": r["score"],
            "level": r["level"],
            "date": monday_str
        }
        # All Projects — 해당 주의 프로젝트 목록 추가 (점수 내림차순)
        if key == "all" and monday_str in proj_week_map:
            projs = []
            for pname, pr in proj_week_map[monday_str].items():
                projs.append({
                    "name": pname,
                    "score": pr["score"],
                    "level": pr["level"]
                })
            projs.sort(key=lambda x: x["score"], reverse=True)
            item["projects"] = projs
        result.append(item)

    return {"history": result}



@app.get("/api/visitors")
async def visitors(request: Request):
    ip = request.client.host
    _visitors[ip] = datetime.now()
    active = get_active_visitors()
    return {"count": len(active)}


# ==================== ANALYTICS & FEEDBACK ====================

@app.post("/api/track")
async def api_track(request: Request):
    if not _DATABASE_URL:
        return {"ok": True}
    try:
        body = await request.json()
        session_id = body.get("session_id", "")
        event_type = body.get("event_type", "")
        page       = body.get("page", "")
        element    = body.get("element", "")
        duration   = body.get("duration")
        ts         = time.time()
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analytics_events (session_id, event_type, page, element, duration, ts) VALUES (%s,%s,%s,%s,%s,%s)",
                    (session_id, event_type, page, element, duration, ts)
                )
            conn.commit()
    except Exception as e:
        print(f"[track] 저장 실패: {e}")
    return {"ok": True}


@app.post("/api/feedback")
async def api_feedback(request: Request):
    try:
        body = await request.json()
        fb_type = body.get("type", "")
        message = body.get("message", "").strip()
        name    = body.get("name", "").strip()
        email   = body.get("email", "").strip()
        ip      = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
        if not message:
            raise HTTPException(status_code=400, detail="내용을 입력해 주세요.")
        ts = time.time()
        if _DATABASE_URL:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO vantix_feedback (type, message, name, email, ip, ts) VALUES (%s,%s,%s,%s,%s,%s)",
                        (fb_type, message, name, email, ip, ts)
                    )
                conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        print(f"[feedback] 저장 실패: {e}")
        raise HTTPException(status_code=500, detail="저장 실패")
    return {"ok": True}


def _require_admin(request: Request):
    auth = request.cookies.get("vx_admin")
    if not ADMIN_PASSWORD or auth != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@app.post("/api/admin/login")
async def api_admin_login(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    if not ADMIN_PASSWORD or pw != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="비밀번호가 틀렸습니다.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("vx_admin", ADMIN_PASSWORD, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@app.post("/api/admin/logout")
async def api_admin_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("vx_admin")
    return resp


@app.get("/api/admin/stats")
async def api_admin_stats(request: Request, _=Depends(_require_admin)):
    if not _DATABASE_URL:
        return {"pages": [], "clicks": [], "exits": [], "sessions": 0}
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                # 총 세션 수
                cur.execute("SELECT COUNT(DISTINCT session_id) FROM analytics_events")
                total_sessions = cur.fetchone()[0]

                # 페이지별 평균 체류 시간 (dwell 이벤트)
                cur.execute("""
                    SELECT page, COUNT(*) as views, COALESCE(AVG(duration),0)::int as avg_sec
                    FROM analytics_events
                    WHERE event_type='dwell' AND page IS NOT NULL AND page != ''
                    GROUP BY page ORDER BY avg_sec DESC
                """)
                pages = [{"page": r[0], "views": r[1], "avg_sec": r[2]} for r in cur.fetchall()]

                # 클릭 Top 10
                cur.execute("""
                    SELECT element, COUNT(*) as cnt
                    FROM analytics_events
                    WHERE event_type='click' AND element IS NOT NULL AND element != ''
                    GROUP BY element ORDER BY cnt DESC LIMIT 10
                """)
                clicks = [{"element": r[0], "cnt": r[1]} for r in cur.fetchall()]

                # 이탈 페이지 (exit 이벤트)
                cur.execute("""
                    SELECT page, COUNT(*) as cnt
                    FROM analytics_events
                    WHERE event_type='exit' AND page IS NOT NULL AND page != ''
                    GROUP BY page ORDER BY cnt DESC
                """)
                exits = [{"page": r[0], "cnt": r[1]} for r in cur.fetchall()]

                # 최근 7일 일별 방문자 수
                cur.execute("""
                    SELECT TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul', 'MM/DD') as day,
                           COUNT(DISTINCT session_id) as visitors
                    FROM analytics_events
                    WHERE ts > EXTRACT(EPOCH FROM NOW()) - 604800
                    GROUP BY day ORDER BY day
                """)
                daily = [{"day": r[0], "visitors": r[1]} for r in cur.fetchall()]

                # 오늘 방문자 수
                cur.execute("""
                    SELECT COUNT(DISTINCT session_id)
                    FROM analytics_events
                    WHERE ts > EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date)
                """)
                today_visitors = cur.fetchone()[0]

        return {"pages": pages, "clicks": clicks, "exits": exits, "sessions": total_sessions, "daily": daily, "today_visitors": today_visitors}
    except Exception as e:
        print(f"[admin/stats] 오류: {e}")
        return {"pages": [], "clicks": [], "exits": [], "sessions": 0, "daily": []}


@app.get("/api/admin/feedback")
async def api_admin_feedback(request: Request, _=Depends(_require_admin)):
    if not _DATABASE_URL:
        return {"items": []}
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, type, message, name, email, ip, ts
                    FROM vantix_feedback ORDER BY ts DESC LIMIT 200
                """)
                rows = cur.fetchall()
        items = [{"id": r[0], "type": r[1], "message": r[2], "name": r[3], "email": r[4],
                  "ip": r[5], "ts": datetime.fromtimestamp(r[6]).strftime("%Y-%m-%d %H:%M")} for r in rows]
        return {"items": items}
    except Exception as e:
        print(f"[admin/feedback] 오류: {e}")
        return {"items": []}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    template_path = os.path.join(os.path.dirname(__file__), "templates", "admin.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/color-preview", response_class=HTMLResponse)
async def color_preview():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "color-preview.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/report-mockup", response_class=HTMLResponse)
async def report_mockup():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "report-mockup.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/notice-mockup", response_class=HTMLResponse)
async def notice_mockup():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "notice-mockup.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/insights-mockup", response_class=HTMLResponse)
async def insights_mockup():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "insights-mockup.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    token = request.cookies.get("vx_session")
    if _get_session(token or ""):
        return RedirectResponse(url="/")
    template_path = os.path.join(os.path.dirname(__file__), "templates", "connect.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html)

@app.post("/api/connect")
async def api_connect(request: Request):
    """베타 온보딩: Redmine URL + API Key 검증 후 세션 발급"""
    client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    _check_rate_limit(client_ip)
    body = await request.json()
    rm_url = (body.get("url") or "").strip().rstrip("/")
    rm_key = (body.get("api_key") or "").strip()
    if not rm_url or not rm_key:
        raise HTTPException(status_code=400, detail="url과 api_key 필수")

    # Redmine 연결 검증
    try:
        test_url = rm_url + "/users/current.json"
        req = urllib.request.Request(test_url, headers={"X-Redmine-API-Key": rm_key})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            user_data = json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Redmine 연결 실패: {str(e)}")

    token = str(_uuid.uuid4())
    _save_session(token, rm_url, rm_key, time.time())
    response = JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", ""), "token": token})
    response.set_cookie("vx_session", token, httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
    return response

@app.post("/api/disconnect")
async def api_disconnect(request: Request):
    """세션 종료 + 쿠키 만료"""
    token = request.cookies.get("vx_session")
    if token:
        _delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("vx_session")
    return response

@app.post("/api/update-connection")
async def api_update_connection(request: Request):
    """설정에서 연결 정보 변경 — 기존 세션 토큰 재사용"""
    token = request.cookies.get("vx_session")
    if not token or not _get_session(token):
        raise HTTPException(status_code=401, detail="session_expired")
    body = await request.json()
    rm_url = (body.get("url") or "").strip().rstrip("/")
    rm_key = (body.get("api_key") or "").strip()
    existing = _get_session(token)
    if not rm_key and existing:
        rm_key = existing.get("key", "")
    if not rm_url or not rm_key:
        raise HTTPException(status_code=400, detail="url과 api_key 필수")
    try:
        test_url = rm_url + "/users/current.json"
        req = urllib.request.Request(test_url, headers={"X-Redmine-API-Key": rm_key})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            user_data = json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Redmine 연결 실패: {str(e)}")
    _save_session(token, rm_url, rm_key, time.time())
    return JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", "")})

@app.get("/api/issue/{issue_id}")
def api_get_issue(issue_id: int, request: Request):
    from concurrent.futures import ThreadPoolExecutor
    s = _require_session(request)
    issue_data = fetch(f"/issues/{issue_id}.json", {"include": "journals,attachments"}, redmine_url=s["url"], api_key=s["key"])
    issue = issue_data.get("issue", {})
    pid = issue.get("project", {}).get("identifier") or str(issue.get("project", {}).get("id", ""))

    def get_statuses(): return fetch("/issue_statuses.json", redmine_url=s["url"], api_key=s["key"]).get("issue_statuses", [])
    def get_members():
        items, offset = [], 0
        while True:
            data = fetch(f"/projects/{pid}/memberships.json", {"limit": 100, "offset": offset}, redmine_url=s["url"], api_key=s["key"])
            batch = data.get("memberships", [])
            items += batch
            total = data.get("total_count", len(batch))
            if offset + 100 >= total:
                break
            offset += 100
        return items
    def get_versions_fn(): return fetch(f"/projects/{pid}/versions.json", redmine_url=s["url"], api_key=s["key"]).get("versions", [])

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_statuses = ex.submit(get_statuses)
        f_members  = ex.submit(get_members)
        f_versions = ex.submit(get_versions_fn)
        statuses = f_statuses.result()
        members  = f_members.result()
        versions = f_versions.result()

    assignees = sorted(
        [{"id": m["user"]["id"], "name": m["user"]["name"]} for m in members if "user" in m],
        key=lambda x: x["name"]
    )
    ver_list = [{"id": v["id"], "name": v["name"]} for v in versions if v.get("status") != "closed"]
    return {"issue": issue, "statuses": statuses, "assignees": assignees, "versions": ver_list}


@app.put("/api/issue/{issue_id}")
async def api_update_issue(issue_id: int, request: Request, s: dict = Depends(_require_session)):
    body = await request.json()
    payload = {"issue": {}}
    if "status_id"        in body: payload["issue"]["status_id"]         = body["status_id"]
    if "assigned_to_id"   in body: payload["issue"]["assigned_to_id"]    = body["assigned_to_id"]
    if "fixed_version_id" in body: payload["issue"]["fixed_version_id"]  = body["fixed_version_id"]
    if "due_date"         in body: payload["issue"]["due_date"]           = body["due_date"]
    if "notes"            in body and body["notes"].strip():
        payload["issue"]["notes"] = body["notes"]

    url = s["url"].rstrip("/") + f"/issues/{issue_id}.json"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"X-Redmine-API-Key": s["key"], "Content-Type": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/attachment-proxy")
async def api_attachment_proxy(url: str, request: Request):
    s = _require_session(request)
    try:
        req = urllib.request.Request(
            url,
            headers={"X-Redmine-API-Key": s["key"]},
        )
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            data = resp.read()
        from fastapi.responses import Response
        return Response(content=data, media_type=content_type)
    except Exception as e:
        from fastapi.responses import Response
        return Response(status_code=404)


@app.post("/api/action/redmine-update")
async def api_redmine_update(payload: dict, request: Request, s: dict = Depends(_require_session)):
    issue_ids = payload.get("issue_ids", [])
    field = payload.get("field", "")
    value = payload.get("value", "")

    field_map = {
        "assignee": "assigned_to_id",
        "due": "due_date",
        "version": "fixed_version_id",
        "priority": "priority_id",
        "status": "status_id",
    }
    rm_field = field_map.get(field)
    if not rm_field:
        return {"error": "invalid field", "success": [], "failed": []}

    if field == "assignee":
        users = fetch("/users.json?limit=100", redmine_url=s["url"], api_key=s["key"]).get("users", [])
        matched = next((u for u in users if u.get("login") == value or
                       (u.get("firstname","") + " " + u.get("lastname","")).strip() == value), None)
        if matched:
            value = str(matched["id"])

    success, failed = [], []
    for issue_id in issue_ids:
        body = {"issue": {rm_field: value}}
        url = s["url"].rstrip("/") + f"/issues/{issue_id}.json"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"X-Redmine-API-Key": s["key"], "Content-Type": "application/json"},
            method="PUT"
        )
        try:
            with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
                success.append(issue_id)
        except Exception:
            failed.append(issue_id)

    return {"success": success, "failed": failed}


@app.post("/api/action/monitor")
async def api_action_monitor(request: Request, s: dict = Depends(_require_session)):
    body = await request.json()
    project_id = body.get("project_id", "")
    # 프론트가 { project_id, config: {overdue, urgent, critical, hour} } 구조로 전송
    cfg = body.get("config", {})
    config = {
        "overdue":  cfg.get("overdue", False),
        "urgent":   cfg.get("urgent", False),
        "critical": cfg.get("critical", False),
        "hour":     cfg.get("hour", "9"),
        "notify_email": cfg.get("notify_email", ""),
    }

    monitor_path = os.path.join(os.path.dirname(__file__), "monitor_config.json")
    try:
        with open(monitor_path, "r", encoding="utf-8") as f:
            all_cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_cfg = {}

    all_cfg[project_id] = config

    # Redmine API로 본인 이메일 자동 조회
    email = get_current_user_email()
    if email:
        all_cfg[project_id]["notify_email"] = email

    with open(monitor_path, "w", encoding="utf-8") as f:
        json.dump(all_cfg, f, ensure_ascii=False, indent=2)

    return {"ok": True, "notify_email": all_cfg[project_id]["notify_email"]}


@app.get("/api/action/monitor")
async def api_get_monitor(project_id: str = "", s: dict = Depends(_require_session)):
    monitor_path = os.path.join(os.path.dirname(__file__), "monitor_config.json")
    try:
        with open(monitor_path, "r", encoding="utf-8") as f:
            all_cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_cfg = {}
    return all_cfg.get(project_id, {})


@app.get("/api/callouts")
async def api_callouts_get(request: Request):
    global _callout_cache, _callout_cache_ts
    token = request.cookies.get("vx_session")
    s = _get_session(token or "")
    if not s or not _DATABASE_URL:
        return {"items": []}
    # 메모리 캐시 히트 → 즉시 반환
    if _callout_cache is not None and time.time() - _callout_cache_ts < _CALLOUT_CACHE_TTL:
        return {"items": _callout_cache}
    def _query():
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, from_name, date, text, color, done, seen FROM vantix_callouts ORDER BY created DESC")
                return cur.fetchall()
    try:
        rows = await _adb(_query)
        items = [{"id": r[0], "from": r[1] or "", "date": r[2] or "", "text": r[3], "color": r[4] or "#ff6b6b", "done": bool(r[5]), "seen": bool(r[6])} for r in rows]
        _callout_cache = items
        _callout_cache_ts = time.time()
        return {"items": items}
    except Exception as e:
        print(f"[callouts/get] {e}")
        return {"items": []}


@app.post("/api/callouts")
async def api_callouts_post(request: Request):
    token = request.cookies.get("vx_session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    body = await request.json()
    cid  = body.get("id", "")
    text = body.get("text", "").strip()
    if not cid or not text:
        raise HTTPException(status_code=400, detail="id and text required")
    if not _DATABASE_URL:
        return {"ok": True}
    vals = (cid, body.get("from", "나"), body.get("date", ""), text, body.get("color", "#ff6b6b"), False, False, time.time())
    def _insert():
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO vantix_callouts (id, from_name, date, text, color, done, seen, created) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    vals
                )
            conn.commit()
    try:
        await _adb(_insert)
        global _callout_cache; _callout_cache = None  # 캐시 무효화
    except Exception as e:
        print(f"[callouts/post] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}


@app.patch("/api/callouts/{cid}")
async def api_callouts_patch(cid: str, request: Request):
    token = request.cookies.get("vx_session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    body = await request.json()
    if not _DATABASE_URL:
        return {"ok": True}
    has_done = "done" in body
    has_seen = "seen" in body
    done_val = bool(body.get("done"))
    seen_val = bool(body.get("seen"))
    def _update():
        with _db_conn() as conn:
            with conn.cursor() as cur:
                if has_done:
                    cur.execute("UPDATE vantix_callouts SET done=%s WHERE id=%s", (done_val, cid))
                if has_seen:
                    cur.execute("UPDATE vantix_callouts SET seen=%s WHERE id=%s", (seen_val, cid))
            conn.commit()
    try:
        await _adb(_update)
        global _callout_cache; _callout_cache = None  # 캐시 무효화
    except Exception as e:
        print(f"[callouts/patch] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}


@app.delete("/api/callouts/{cid}")
async def api_callouts_delete(cid: str, request: Request):
    token = request.cookies.get("vx_session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    if not _DATABASE_URL:
        return {"ok": True}
    def _delete():
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM vantix_callouts WHERE id=%s", (cid,))
            conn.commit()
    try:
        await _adb(_delete)
        global _callout_cache; _callout_cache = None  # 캐시 무효화
    except Exception as e:
        print(f"[callouts/delete] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}


@app.get("/api/report/preview")
async def api_report_preview(
    request: Request,
    project_id:    str = "",
    updated_after: str = "2026-03-01",
    sections:      str = "",
    memo:          str = "",
    s: dict = Depends(_require_session),
):
    from fastapi.responses import HTMLResponse

    redmine_url = s.get("url")
    api_key     = s.get("key")

    dashboard = get_cache(project_id, updated_after, redmine_url)
    if not dashboard:
        dashboard = build_dashboard_data(
            project_id, updated_after,
            redmine_url=redmine_url, api_key=api_key
        )

    # identifier → display name 변환
    proj_name = project_id
    try:
        projects_raw = fetch(
            "/projects.json?limit=100",
            redmine_url=redmine_url,
            api_key=api_key
        )
        for p in projects_raw.get("projects", []):
            if p.get("identifier") == project_id:
                proj_name = p.get("name", project_id)
                break
    except Exception:
        pass

    # 버전 데이터 + 이슈 카운트 계산해서 dashboard에 주입
    if "versions" not in dashboard or not dashboard.get("versions"):
        try:
            today_str = date.today().isoformat()
            raw_versions = get_versions(
                project_id,
                redmine_url=redmine_url,
                api_key=api_key
            )
            enriched = []
            for v in raw_versions:
                vid = v.get("id")
                try:
                    v_issues = get_version_issues(
                        vid,
                        redmine_url=redmine_url,
                        api_key=api_key
                    )
                except Exception:
                    v_issues = []
                total   = len(v_issues)
                closed  = len([i for i in v_issues
                                if i.get("status", {}).get("name") in CLOSED_SET])
                overdue = len([i for i in v_issues
                                if i.get("due_date") and i["due_date"] < today_str
                                and i.get("status", {}).get("name") not in CLOSED_SET])
                v["_total"]   = total
                v["_closed"]  = closed
                v["_overdue"] = overdue
                enriched.append(v)
            dashboard["versions"] = enriched
        except Exception:
            dashboard["versions"] = []

    section_list = [sec.strip() for sec in sections.split(",") if sec.strip()] if sections else None

    # AI 요약 주입 — report-summary 캐시 우선, 없으면 직접 생성
    if not dashboard.get("ai_summary"):
        try:
            cache_k = f"report-summary|{redmine_url.rstrip('/')}|{project_id}|{updated_after}"
            cached_summary = _get_ai_cache(cache_k)
            if cached_summary:
                dashboard["ai_summary"] = cached_summary
            else:
                ai_resp = await api_ai_report_summary(
                    project_id=project_id,
                    updated_after=updated_after,
                    s=s
                )
                summary_val = ai_resp.get("summary", "") if isinstance(ai_resp, dict) else ""
                dashboard["ai_summary"] = summary_val
        except Exception:
            dashboard["ai_summary"] = ""

    # 인사이트 생성 — 버전 데이터(issues 포함) 필요
    from app.insights import run_all_insights
    r_url = s["url"].rstrip("/")
    vkey  = f"versions|{r_url}|{project_id}"
    ins_versions = None
    v_entry = _cache.get(vkey)
    if v_entry and (datetime.now() - v_entry["fetched_at"]).total_seconds() < CACHE_TTL_SECONDS:
        ins_versions = v_entry["data"]
    if not ins_versions:
        try:
            ins_versions = build_version_data(project_id, redmine_url=s["url"], api_key=s["key"])
            if ins_versions:
                _cache[vkey] = {"data": ins_versions, "fetched_at": datetime.now()}
        except Exception:
            ins_versions = []
    insight_dash = dict(dashboard)
    insight_dash["versions"] = ins_versions or []
    try:
        raw_insights = run_all_insights(insight_dash)
        insights_data = [
            {"rule": i.rule, "level": i.level, "title": i.title, "body": i.body, "target": i.target}
            for i in raw_insights
        ]
    except Exception:
        insights_data = []

    report = build_report_data(dashboard, project_label=proj_name, insights=insights_data)
    html   = render_html_report(report, sections=section_list, memo=memo)
    return HTMLResponse(content=html)


@app.get("/api/report/tsv")
async def api_report_tsv(
    request: Request,
    project_id:    str = "",
    updated_after: str = "2026-03-01",
    sections:      str = "",
    s: dict = Depends(_require_session),
):
    from fastapi.responses import PlainTextResponse
    redmine_url = s.get("url")
    api_key     = s.get("key")
    dashboard = get_cache(project_id, updated_after, redmine_url)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=redmine_url, api_key=api_key)
    section_list = [sec.strip() for sec in sections.split(",") if sec.strip()] if sections else None
    report = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    tsv    = render_tsv_report(report, sections=section_list)
    return PlainTextResponse(content=tsv, media_type="text/plain; charset=utf-8")


@app.post("/api/report/send")
async def api_report_send(project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    dashboard = get_cache(project_id, updated_after, s["url"])
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
    report  = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    html    = render_html_report(report)
    subject = f"[Vantix] 주간 리포트 {report.period_label}"
    return send_report_email(html, subject, EMAIL_CFG)


@app.get("/api/redmine-users")
async def api_redmine_users(s: dict = Depends(_require_session)):
    redmine_url = s["url"]
    api_key     = s["key"]
    try:
        data  = fetch("/users.json?limit=100", redmine_url=redmine_url, api_key=api_key)
        users = []
        for u in data.get("users", []):
            name = f"{u.get('lastname', '')} {u.get('firstname', '')}".strip() or u.get("login", "")
            users.append({"id": u.get("id"), "name": name, "email": u.get("mail", ""), "login": u.get("login", "")})
        return {"users": users}
    except Exception as e:
        return {"users": [], "error": str(e)}


@app.post("/api/report/send-html")
async def api_report_send_html(request: Request, s: dict = Depends(_require_session)):
    body       = await request.json()
    html       = body.get("html", "")
    recipients = body.get("recipients", [])
    subject    = body.get("subject", f"[Vantix] 주간 리포트 {date.today().strftime('%Y-%m-%d')}")
    if not html:
        return {"ok": False, "error": "리포트 내용이 없습니다"}
    if not recipients:
        return {"ok": False, "error": "수신자를 선택해주세요"}
    from dataclasses import replace
    cfg = replace(EMAIL_CFG, recipients=recipients,
                  enabled=bool(EMAIL_CFG.host and EMAIL_CFG.user and EMAIL_CFG.password))
    return send_report_email(html, subject, cfg)


# ==================== AI 엔드포인트 ====================

@app.get("/api/ai/risk-comment")
async def api_ai_risk_comment(s: dict = Depends(_require_session),
    name: str = "", score: float = 0,
    overdue: int = 0, urgent: int = 0, open_issues: int = 0,
    top_issues: str = ""
):
    cache_k = f"risk-comment|{name}|{score}|{overdue}|{urgent}|{open_issues}|{top_issues}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return {"comment": cached}
    issues_context = f"\n주요 초과 이슈: {top_issues}" if top_issues else ""
    prompt = (
        f"다음 프로젝트 리스크 데이터를 보고 아래 순서대로 '항목 · 항목 · 항목 · 항목' 형태로 딱 한 줄만 답해줘.\n"
        f"순서: ①마감초과 건수 ②마감임박 건수 ③주요 리스크 한 가지(이슈 제목에서 기능명/그룹 추출, 없으면 오픈 이슈 현황으로 대체) ④리스크 점수\n"
        f"프로젝트명: {name}, 리스크점수: {score}, 마감초과: {overdue}건, 임박: {urgent}건, 오픈: {open_issues}건"
        f"{issues_context}\n"
        f"예시1(초과있을때): 마감 초과 {overdue}건 · 임박 {urgent}건 · 로그인 기능 지연(클라) · Risk Score {score}\n"
        f"예시2(초과없을때): 마감 초과 0건 · 임박 {urgent}건 · 오픈 {open_issues}건 진행 중 · Risk Score {score}\n"
        f"형식: 예시 형태 그대로 딱 한 줄, '정보 미제공' '미지정' 같은 표현 절대 쓰지 말 것, 앞뒤 설명 없이"
    )
    try:
        comment = _call_claude(prompt, max_tokens=150)
        _set_ai_cache(cache_k, comment)
        return {"comment": comment}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/action-signals")
async def api_ai_action_signals(s: dict = Depends(_require_session),
    project_id: str = "",
    updated_after: str = "2026-01-01",
    force: bool = False
):
    cache_k = f"action-signals|{s['url'].rstrip('/')}|{project_id}"

    # force=True면 캐시 삭제
    if force and cache_k in _ai_cache:
        del _ai_cache[cache_k]

    # 캐시 있으면 바로 반환
    cached = _get_ai_cache(cache_k)
    if cached:
        parsed = json.loads(cached)
        if parsed:
            return {"signals": parsed}

    # 캐시에서 해당 유저+프로젝트 항목 찾기 (캐시 키: url|project_id|updated_after)
    r_url = s["url"].rstrip("/")
    dashboard = get_cache(project_id, updated_after, r_url)
    if not dashboard:
        dashboard = get_cache(project_id, DEFAULT_UPDATED_AFTER, r_url)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, DEFAULT_UPDATED_AFTER, redmine_url=s["url"], api_key=s["key"])
        set_cache(project_id, DEFAULT_UPDATED_AFTER, dashboard, redmine_url=s["url"])

    risks = dashboard.get("project_risk", [])[:5]
    if not risks:
        return {"signals": []}

    # 담당자별 이슈 수 및 편중도
    users_data = dashboard.get("users_data", {})
    assignee_counts = {u: len(v["issues"]) for u, v in users_data.items() if v["issues"]}
    total_issues = sum(assignee_counts.values()) or 1
    top_assignee = max(assignee_counts, key=assignee_counts.get) if assignee_counts else None
    top_count = assignee_counts.get(top_assignee, 0)
    top_pct = round(top_count / total_issues * 100)

    # 담당자별 상세 (상위 5명)
    assignee_lines = "\n".join([
        f"  - {u}: {len(v['issues'])}건 (overdue={sum(1 for i in v['issues'] if i.get('_elapsed') is not None and i.get('_elapsed',0)<0)})"
        for u, v in sorted(users_data.items(), key=lambda x: len(x[1]["issues"]), reverse=True)[:5]
        if v["issues"]
    ])

    # 지연 이슈 상위 5개 추출
    all_issues = []
    for v in users_data.values():
        all_issues.extend(v.get("issues", []))
    overdue_issues = [i for i in all_issues if i.get("_elapsed") is not None and i.get("_elapsed", 0) < 0]
    overdue_issues.sort(key=lambda i: i.get("_elapsed", 0))
    overdue_lines = "\n".join([
        f"  - [{i.get('status','?')}] {i.get('subject','?')} (D+{abs(i.get('_elapsed',0))}, 담당:{i.get('assigned_to','?')})"
        for i in overdue_issues[:5]
    ]) or "  없음"

    # 마감 임박 이슈 상위 3개
    urgent_issues = [i for i in all_issues if i.get("_elapsed") is not None and 0 <= i.get("_elapsed", 99) <= 7]
    urgent_lines = "\n".join([
        f"  - [{i.get('status','?')}] {i.get('subject','?')} (D-{i.get('_elapsed',0)}, 담당:{i.get('assigned_to','?')})"
        for i in urgent_issues[:3]
    ]) or "  없음"

    # 전주 대비 리스크 변화
    try:
        import json as _json
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
            hist = _json.load(f)
        proj_key = f"project_{project_id}" if project_id else "all"
        proj_hist = hist.get(proj_key, hist.get("all", []))
        if len(proj_hist) >= 2:
            delta = round(proj_hist[-1]["score"] - proj_hist[-2]["score"], 1)
            delta_str = f"+{delta}" if delta > 0 else str(delta)
        else:
            delta_str = "데이터 부족"
    except:
        delta_str = "알 수 없음"

    risk_lines = "\n".join([
        f"- {r['name']}: score={min(round(r['risk_score']*100/60),100)}, "
        f"level={r['risk_level']}, overdue={r.get('issues_overdue_count',0)}, "
        f"urgent={r.get('issues_urgent_count',0)}"
        for r in risks
    ])

    prompt = (
        "아래 실제 프로젝트 데이터를 보고 JSON 배열만 반환해줘. 설명 없이 JSON만.\n"
        "반드시 4~5개 액션을 생성해야 해.\n"
        "각 항목 형식:\n"
        "{\"priority\": \"P1\"|\"P2\"|\"P3\", "
        "\"timing\": \"IMMEDIATE\"|\"THIS WEEK\"|\"NEXT SPRINT\", "
        "\"action_type\": \"REASSIGN\"|\"ESCALATE\"|\"SCHEDULE\"|\"MONITOR\", "
        "\"action\": \"구체적 처방 한 줄 (이슈명/담당자명/수치를 직접 언급)\", "
        "\"reason\": \"원인 한 줄 (데이터 수치 근거 포함)\", "
        "\"who\": \"담당자 역할\"}\n\n"
        f"[프로젝트 리스크 현황]\n{risk_lines}\n\n"
        f"[마감 초과 이슈 목록]\n{overdue_lines}\n\n"
        f"[마감 임박 이슈 목록 (7일 이내)]\n{urgent_lines}\n\n"
        f"[담당자별 이슈 현황]\n{assignee_lines}\n\n"
        f"[전주 대비 리스크 변화]\n{delta_str}점\n\n"
        "규칙:\n"
        "1. 각 액션은 서로 다른 관점이어야 함 (중복 금지)\n"
        "   - 하나는 특정 지연 이슈 처리, 하나는 담당자 재배분, 하나는 임박 이슈 대응, 하나는 리스크 모니터링, 하나는 예방 조치\n"
        "2. action 필드에 반드시 이슈명 또는 담당자명을 직접 언급할 것\n"
        "3. '분석이 필요합니다', '회의를 권장합니다' 같은 추상적 표현 금지\n"
        "4. 실행 가능한 구체적 동사로 시작할 것 (예: '[이슈명]을 오늘 중 [담당자]와 해결 방안 확정', '[담당자]의 [N]건 중 [N]건을 [담당자2]에게 이전')\n"
        "우선순위 기준:\n"
        "P1/IMMEDIATE: overdue > 0 이거나 담당자 집중도 70% 이상\n"
        "P2/THIS WEEK: HIGH 리스크이거나 urgent > 0\n"
        "P3/NEXT SPRINT: 예방 조치\n"
        "JSON 배열만, 마크다운 코드블록 없이"
    )

    try:
        raw = _call_claude(prompt, max_tokens=800)
        import re as _re
        match = _re.search(r'\[.*\]', raw, _re.DOTALL)
        signals = json.loads(match.group()) if match else []
        if signals:
            _set_ai_cache(cache_k, json.dumps(signals, ensure_ascii=False))
        return {"signals": signals}
    except Exception as e:
        return {"error": str(e), "signals": []}


@app.get("/api/ai/report-summary")
async def api_ai_report_summary(project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    cache_k = f"report-summary|{s['url'].rstrip('/')}|{project_id}|{updated_after}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return {"summary": cached}
    dashboard = get_cache(project_id, updated_after, s["url"])
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
    top_risk  = dashboard.get("project_risk", [])[:3]
    top_names = ", ".join(f"{p['name']}({p['risk_level']}, score {min(round(p['risk_score'] * 100 / 60), 100)})" for p in top_risk) or "없음"
    users_data = dashboard.get("users_data", {})
    top_user = max(
        users_data.items(),
        key=lambda kv: sum(1 for i in kv[1]["issues"]
                           if i.get("due_date") and i["due_date"] < date.today().strftime("%Y-%m-%d")
                           and i["status"] not in CLOSED_SET),
        default=(None, None)
    )
    top_user_name = short_name(top_user[0]) if top_user[0] else "없음"
    all_risk_list = dashboard.get("project_risk", [])
    highest_risk = all_risk_list[0] if all_risk_list else None
    lowest_risk  = all_risk_list[-1] if len(all_risk_list) > 1 else None
    def norm(s): return min(round(s * 100 / 60), 100)
    highest_str  = f"{highest_risk['name']}(score {norm(highest_risk['risk_score'])}, {highest_risk['risk_level']})" if highest_risk else "없음"
    lowest_str   = f"{lowest_risk['name']}(score {norm(lowest_risk['risk_score'])}, {lowest_risk['risk_level']})" if lowest_risk else "없음"

    if not project_id:
        prompt = (
            f"마크다운 문법 사용 금지. **굵게**, *기울임*, # 헤더 등 일절 사용하지 말것.\n"
            f"첫 줄: 15자 이내 헤드라인. 숫자+상태 조합. 예: \"복합 위험 즉각 대응 필요\", \"Critical 1건 High 1건 점검\"\n"
            f"줄바꿈 후 본문 시작. 서두 문장 금지.\n"
            f"\n"
            f"아래 4줄 형식 엄수:\n"
            f"ST1: [위험도 최고 프로젝트명 + 수치 + 상태]\n"
            f"ST2: [위험도 최저 프로젝트명 + 수치 또는 전체 특이사항]\n"
            f"AC: [PM이 지금 당장 해야 할 조치]\n"
            f"\n"
            f"각 줄은 ~함. ~있음. ~필요. ~권고. 로 끝낼 것. 존댓말 금지. 4줄 이상 절대 금지.\n"
            f"\n"
            f"데이터:\n"
            f"전체 프로젝트 수: {len(all_risk_list)}개 (이 중 위험군(Critical/High): {sum(1 for p in all_risk_list if p['risk_level'] in ['Critical','High'])}개, 정상(Medium/Low): {sum(1 for p in all_risk_list if p['risk_level'] in ['Medium','Low'])}개)\n"
            f"전체이슈: {dashboard.get('total_issues', 0)}, 오픈: {dashboard.get('open_issues', 0)}, 마감초과: {dashboard.get('overdue', 0)}건\n"
            f"위험도 최고: {highest_str}\n"
            f"위험도 최저: {lowest_str}\n"
            f"프로젝트별 위험도 전체: {top_names}\n"
            f"규칙: 정상(Low/Medium) 프로젝트는 위험하다고 쓰지 말 것. ST/AC 포함 본문 최대 3줄 절대 엄수.\n"
            f"가장 초과 많은 담당자: {top_user_name if top_user_name else '없음'}\n"
            f"규칙: 마감초과가 0건이면 절대 위험하다고 쓰지 말 것.\n"
            f"점수는 모두 100점 만점 기준임. PM, 매니저, 관리자 등 직군명 언급 금지. 담당자로만 표현할 것.\n"
        )
    else:
        prompt = (
            f"마크다운 문법 사용 금지. **굵게**, *기울임*, # 헤더 등 일절 사용하지 말것.\n"
            f"첫 줄: 숫자+상태 조합 15자 이내 헤드라인. 프로젝트명 금지. 예: \"마감 초과 7건 Critical\", \"복합 위험 즉각 조치 필요\"\n"
            f"줄바꿈 후 본문 시작. 서두 문장 금지.\n"
            f"\n"
            f"위험 항목이 1개일 때 → 아래 3줄 형식 엄수:\n"
            f"ST: [수치 포함 현재 상태]\n"
            f"CA: [담당자 또는 원인]\n"
            f"AC: [구체적 액션]\n"
            f"\n"
            f"위험 항목이 2개 이상일 때 → 아래 3줄 형식 엄수:\n"
            f"ST1: [가장 심각한 위험 수치 포함]\n"
            f"ST2: [두 번째 위험 수치 포함]\n"
            f"AC: [통합 액션 권고]\n"
            f"\n"
            f"각 줄은 ~함. ~있음. ~필요. ~권고. 로 끝낼 것. 존댓말 금지. 4줄 이상 절대 금지.\n"
            f"\n"
            f"데이터:\n"
            f"전체이슈: {dashboard.get('total_issues', 0)}, 오픈: {dashboard.get('open_issues', 0)}, "
            f"마감초과: {dashboard.get('overdue', 0)}건\n"
            f"프로젝트별 위험도: {top_names}\n"
            f"가장 초과 많은 담당자: {top_user_name if top_user_name else '없음'}\n"
            f"규칙: 마감초과가 0건이면 절대 위험하다고 쓰지 말 것. 오픈이슈가 0이면 모든 이슈가 완료된 상태임.\n"
            f"점수는 모두 100점 만점 기준임. PM, 매니저, 관리자 등 직군명 언급 금지. 담당자로만 표현할 것.\n"
            f"\n"
            f"마감초과 0건(정상 상태)일 때 → 아래 3줄 형식 엄수:\n"
            f"ST: [오픈 이슈 수 및 현재 상태 요약]\n"
            f"CA: [없음 또는 주요 담당자 현황]\n"
            f"AC: [현행 유지 또는 모니터링 권고]\n"
            f"본문에서도 프로젝트명 절대 언급 금지. 담당자명, 수치, 액션만 사용할 것.\n"
        )
    try:
        summary = _call_claude(prompt, max_tokens=300)
        _set_ai_cache(cache_k, summary)
        return {"summary": summary}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/delay-prediction")
async def api_ai_delay_prediction(s: dict = Depends(_require_session),
    project_id: str = "", updated_after: str = "2026-03-01", project_name: str = ""
):
    cache_k = f"delay-pred|{s['url'].rstrip('/')}|{project_id}|{project_name}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return cached
    dashboard = get_cache(project_id, updated_after, s["url"])
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
    risk_list  = dashboard.get("project_risk", [])
    p = next((x for x in risk_list if x["name"] == project_name), None)
    if not p:
        p = risk_list[0] if risk_list else {}
    users_data = dashboard.get("users_data", {})
    today_str  = date.today().strftime("%Y-%m-%d")
    user_counts: dict = {}
    resolved_cnt = 0
    for uname, ud in users_data.items():
        proj_issues = [i for i in ud["issues"] if i.get("project") == project_name] if project_name else ud["issues"]
        if proj_issues:
            user_counts[uname] = len(proj_issues)
        resolved_cnt += sum(1 for i in proj_issues if i["status"] in RESOLVED_SET)
    total = p.get("total", 1) or 1
    top_cnt = max(user_counts.values(), default=0)
    concentration_pct = round(top_cnt / sum(user_counts.values()) * 100) if user_counts else 0
    overdue_pct = round(p.get("overdue", 0) / total * 100)
    resolve_rate = round(resolved_cnt / total * 100)
    prompt = (
        f"다음 프로젝트 데이터를 보고 지연 위험도(높음/중간/낮음)와 이유를 2~3문장으로 예측해줘.\n"
        f"프로젝트명: {project_name or p.get('name', '전체')}\n"
        f"오픈이슈: {p.get('open', 0)}건, 마감초과비율: {overdue_pct}%, 해결율: {resolve_rate}%\n"
        f"담당자집중도: 상위 1명이 전체의 {concentration_pct}% 담당\n"
        f"형식: 첫 줄에 위험도만 한 단어(높음/중간/낮음), 둘째 줄부터 이유"
    )
    try:
        result = _call_claude(prompt, max_tokens=200)
        lines  = result.strip().split("\n", 1)
        level_raw = lines[0].strip()
        reason    = lines[1].strip() if len(lines) > 1 else result
        if "높음" in level_raw:
            level = "높음"
        elif "중간" in level_raw:
            level = "중간"
        else:
            level = "낮음"
        data = {"level": level, "reason": reason}
        _set_ai_cache(cache_k, data)
        return data
    except Exception as e:
        return {"error": str(e)}


# ── Rule-based Insights ─────────────────────────────────────────
@app.get("/api/insights")
async def api_insights(
    project_id: str = "",
    updated_after: str = "2026-03-01",
    s: dict = Depends(_require_session),
):
    from app.insights import run_all_insights

    r_url = s["url"].rstrip("/")

    # 대시보드 데이터 (캐시 우선)
    dashboard = get_cache(project_id, updated_after, r_url)
    if not dashboard:
        dashboard = build_dashboard_data(
            project_id, updated_after, redmine_url=s["url"], api_key=s["key"]
        )
        set_cache(project_id, updated_after, dashboard, redmine_url=s["url"])

    # 버전 데이터 (issues 포함) — 인사이트 룰에 필요
    vkey = f"versions|{r_url}|{project_id}"
    versions = None
    entry = _cache.get(vkey)
    if entry and (datetime.now() - entry["fetched_at"]).total_seconds() < CACHE_TTL_SECONDS:
        versions = entry["data"]
    if not versions:
        try:
            versions = build_version_data(
                project_id, redmine_url=s["url"], api_key=s["key"]
            )
            if versions:
                _cache[vkey] = {"data": versions, "fetched_at": datetime.now()}
        except Exception:
            versions = []

    # 인사이트용 dashboard에 버전(issues 포함) + 히스토리 주입
    insight_dash = dict(dashboard)
    insight_dash["versions"] = versions or []

    try:
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as _hf:
            _hist_all = json.load(_hf)
        if project_id:
            _hist_key = f"project_{project_id}"
            _hist_records = _hist_all.get(_hist_key) or _hist_all.get("all", [])
        else:
            _hist_records = _hist_all.get("all", [])
        insight_dash["history"] = sorted(_hist_records, key=lambda x: x.get("date", ""))
    except Exception:
        insight_dash["history"] = []

    try:
        insights = run_all_insights(insight_dash)
    except Exception as e:
        return {"insights": [], "error": str(e)}

    return {
        "insights": [
            {
                "rule":   ins.rule,
                "level":  ins.level,
                "title":  ins.title,
                "body":   ins.body,
                "target": ins.target,
            }
            for ins in insights
        ]
    }


# ==================== 실행 ====================

def warmup_cache():
    """서버 시작 직후 백그라운드에서 캐시 미리 채우기"""
    time.sleep(2)
    print("  캐시 워밍 시작 (백그라운드)...")
    targets = [
        ("", DEFAULT_UPDATED_AFTER),
        *([(DEFAULT_PROJECT_ID, DEFAULT_UPDATED_AFTER)] if DEFAULT_PROJECT_ID else []),
    ]
    for project_id, updated_after in targets:
        try:
            label = project_id if project_id else "전체"
            print(f"  워밍 중: {label}")
            data = build_dashboard_data(project_id, updated_after)
            set_cache(project_id, updated_after, data)
            print(f"  워밍 완료: {label}")
        except Exception as e:
            print(f"  캐시 워밍 실패 ({label}): {e}")
    print("  캐시 워밍 전체 완료 — 첫 방문자 즉시 응답 가능")


def _preload_all_projects():
    try:
        projects = get_projects()
        for p in projects:
            pid = str(p.get("id", ""))
            if not pid:
                continue
            try:
                data = build_dashboard_data(pid, DEFAULT_UPDATED_AFTER)
                set_cache(pid, DEFAULT_UPDATED_AFTER, data)
                print(f"  프리로드 완료: {pid}")
            except Exception as e:
                print(f"  프리로드 실패 {pid}: {e}")
    except Exception as e:
        print(f"  프리로드 전체 실패: {e}")


def _warmup_then_preload():
    warmup_cache()          # ① 전체 + 기본 프로젝트 먼저 (유저 첫 화면)
    _preload_all_projects() # ② 완료 후 나머지 순차 워밍

threading.Thread(target=_warmup_then_preload, daemon=True).start()


if __name__ == "__main__":
    print("=" * 50)
    print("  Vantix 시작")
    print("=" * 50)
    print(f"  브라우저에서 열기: http://localhost:8000")
    print(f"  종료: Ctrl+C")
    print(f"  자동갱신 주기: 30분")
    print("=" * 50)

    # warmup은 _warmup_then_preload 에서 순차 실행됨 (모듈 로드 시 이미 시작)

    uvicorn.run(app, host="0.0.0.0", port=8000)
