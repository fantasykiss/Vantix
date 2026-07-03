#!/usr/bin/env python3
"""
Redmine 실시간 웹 대시보드
실행: python main.py
접속: http://localhost:8000
"""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
import uvicorn

# ==================== 서버 설정 ====================
from config import BASE_URL, API_KEY, ANTHROPIC_API_KEY, AI_MODEL, EMAIL_CFG, REPORT_DAY, REPORT_HOUR, REPORT_MINUTE, REDMINE_PUBLIC_URL, FERNET_KEY, ADMIN_PASSWORD, DEMO_URL, DEMO_KEY, OWNER_IPS
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
DEMO_SESSION_TTL = 30 * 60   # 30분
_demo_tokens: set = set()    # 데모 세션 토큰 추적 (메모리 전용)
_SESSION_FILE = os.getenv("SESSIONS_PATH", os.path.join(os.path.dirname(__file__), "sessions.json"))
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
                cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS ip TEXT")
                cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS user_agent TEXT")
                cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS env TEXT")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vantix_callouts (
                        id      TEXT PRIMARY KEY,
                        from_name TEXT,
                        date    TEXT,
                        text    TEXT NOT NULL,
                        color   TEXT,
                        done    BOOLEAN DEFAULT FALSE,
                        seen    BOOLEAN DEFAULT FALSE,
                        created DOUBLE PRECISION NOT NULL,
                        expires_at DOUBLE PRECISION
                    )
                """)
                cur.execute("ALTER TABLE vantix_callouts ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS risk_history (
                        hist_key TEXT NOT NULL,
                        date     TEXT NOT NULL,
                        score    REAL NOT NULL,
                        level    TEXT NOT NULL,
                        overdue  INTEGER DEFAULT 0,
                        urgent   INTEGER DEFAULT 0,
                        PRIMARY KEY (hist_key, date)
                    )
                """)
            conn.commit()
    except Exception as e:
        print(f"[analytics] DB 테이블 초기화 실패: {e}")

def _db_load_history(key: str) -> list:
    """DB에서 hist_key에 해당하는 히스토리 엔트리 반환 (날짜 오름차순)."""
    if not _DATABASE_URL:
        return []
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT date, score, level, overdue, urgent FROM risk_history WHERE hist_key=%s ORDER BY date ASC",
                    (key,)
                )
                return [{"date": r[0], "score": r[1], "level": r[2], "overdue": r[3], "urgent": r[4]} for r in cur.fetchall()]
    except Exception:
        return []

def _db_save_history_entry(key: str, date: str, score: float, level: str, overdue: int, urgent: int):
    """DB에 히스토리 엔트리 upsert. 52개 초과분은 오래된 것부터 삭제."""
    if not _DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO risk_history (hist_key, date, score, level, overdue, urgent)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (hist_key, date) DO UPDATE
                        SET score=EXCLUDED.score, level=EXCLUDED.level,
                            overdue=EXCLUDED.overdue, urgent=EXCLUDED.urgent
                """, (key, date, score, level, overdue, urgent))
                # 52주 초과분 정리
                cur.execute("""
                    DELETE FROM risk_history WHERE hist_key=%s AND date NOT IN (
                        SELECT date FROM risk_history WHERE hist_key=%s ORDER BY date DESC LIMIT 52
                    )
                """, (key, key))
            conn.commit()
    except Exception as e:
        print(f"[risk_history] DB 저장 실패: {e}")

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
        ttl = DEMO_SESSION_TTL if token in _demo_tokens else SESSION_TTL
        if time.time() - s["created"] > ttl:
            _demo_tokens.discard(token)
            _session_cache.pop(token, None)
            _delete_session(token)
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
            ttl = DEMO_SESSION_TTL if token in _demo_tokens else SESSION_TTL
            if time.time() - created > ttl:
                _demo_tokens.discard(token)
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
    except Exception as e:
        print(f"[session/get] {e}")
    return None

def _require_session(request: Request) -> dict:
    token = request.cookies.get("vx_session") or request.headers.get("X-VX-Session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401, detail="session_expired")
    return s

def _require_login(request: Request) -> int:
    """vx_user_id 쿠키 또는 Redmine 세션 중 하나만 있으면 통과. 결제/구독 API용."""
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return uid

_init_session_table()
_init_analytics_tables()
_warm_session_cache()

# ==================== 유저 DB (SQLite) ====================
from app.constants import (
    DEFAULT_PLAN, plan_info, plan_allows, plan_project_limit, plan_member_limit,
    PLAN_ORDER,
)
from config import RESEND_API_KEY, RESEND_FROM, SUPPORT_EMAIL, PORTONE_STORE_ID, PORTONE_CHANNEL_KEY, PORTONE_CHANNEL_KEY_INICIS, PORTONE_CHANNEL_KEY_TOSSPAY, PORTONE_API_SECRET, PLAN_PRICES
import resend as _resend
_resend.api_key = RESEND_API_KEY
import httpx as _httpx

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 회원/결제/팀 데이터는 PostgreSQL(DATABASE_URL)에 저장 → 로컬·Railway 공유, 재배포 시 보존.
# 기존 SQLite 호출 패턴(conn.execute(...).fetchone(), dict(row))을 그대로 쓰도록 감싸는 호환 래퍼.
from psycopg2.extras import RealDictCursor as _RealDictCursor

class _PgUsersConn:
    """SQLite 호환 인터페이스로 PostgreSQL 커넥션을 감싼다."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=_RealDictCursor)
        cur.execute(sql, params)
        return cur
    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)
        cur.close()
    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)
    def commit(self):
        self._conn.commit()

class _users_db:
    """`with _users_db() as conn:` — 풀에서 꺼내 쓰고 정상 종료 시 commit, 예외 시 rollback."""
    def __enter__(self):
        self._raw = _get_pool().getconn()
        return _PgUsersConn(self._raw)
    def __exit__(self, exc_type, *_):
        try:
            if exc_type:
                self._raw.rollback()
            else:
                self._raw.commit()
        finally:
            _get_pool().putconn(self._raw)

def _init_users_db():
    if not _DATABASE_URL:
        print("[users-db] DATABASE_URL 없음 — 회원 DB 초기화 건너뜀")
        return
    with _users_db() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS vantix_users (
                id               SERIAL PRIMARY KEY,
                email            TEXT    UNIQUE NOT NULL,
                hashed_password  TEXT    NOT NULL,
                created_at       DOUBLE PRECISION NOT NULL,
                is_active        INTEGER DEFAULT 1,
                plan             TEXT    DEFAULT '{DEFAULT_PLAN}',
                email_verified   INTEGER DEFAULT 0,
                email_verify_token TEXT
            );
            CREATE TABLE IF NOT EXISTS vantix_redmine_connections (
                id                SERIAL PRIMARY KEY,
                user_id           INTEGER NOT NULL REFERENCES vantix_users(id),
                redmine_url       TEXT    NOT NULL,
                api_key           TEXT    NOT NULL,
                default_project_id TEXT   DEFAULT '',
                updated_after     TEXT    DEFAULT '',
                created_at        DOUBLE PRECISION NOT NULL
            );
            -- Phase 4: 유저가 모니터링하기로 선택한 프로젝트 (플랜별 개수 제한 적용 대상)
            CREATE TABLE IF NOT EXISTS vantix_user_projects (
                id           SERIAL PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES vantix_users(id),
                project_id   TEXT    NOT NULL,
                project_name TEXT    DEFAULT '',
                created_at   DOUBLE PRECISION NOT NULL,
                UNIQUE(user_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS vantix_billing_keys (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES vantix_users(id),
                billing_key TEXT    NOT NULL,
                plan        TEXT    NOT NULL,
                status      TEXT    DEFAULT 'active',
                created_at  DOUBLE PRECISION NOT NULL,
                expires_at  DOUBLE PRECISION
            );
            CREATE TABLE IF NOT EXISTS vantix_payment_history (
                id             SERIAL PRIMARY KEY,
                user_id        INTEGER NOT NULL,
                payment_id     TEXT    NOT NULL UNIQUE,
                billing_key_id INTEGER,
                plan           TEXT    NOT NULL,
                amount         INTEGER NOT NULL,
                status         TEXT    NOT NULL,
                paid_at        DOUBLE PRECISION
            );
            -- Phase 4: 팀(워크스페이스) — 오너가 결제, 팀원은 자기 Redmine 키로 합류
            CREATE TABLE IF NOT EXISTS vantix_workspaces (
                id            SERIAL PRIMARY KEY,
                owner_user_id INTEGER NOT NULL UNIQUE REFERENCES vantix_users(id),
                created_at    DOUBLE PRECISION NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vantix_workspace_members (
                id           SERIAL PRIMARY KEY,
                workspace_id INTEGER NOT NULL REFERENCES vantix_workspaces(id),
                user_id      INTEGER NOT NULL REFERENCES vantix_users(id),
                role         TEXT    NOT NULL DEFAULT 'viewer',
                created_at   DOUBLE PRECISION NOT NULL,
                UNIQUE(workspace_id, user_id),
                UNIQUE(user_id)
            );
            CREATE TABLE IF NOT EXISTS vantix_invitations (
                id           SERIAL PRIMARY KEY,
                workspace_id INTEGER NOT NULL REFERENCES vantix_workspaces(id),
                email        TEXT    NOT NULL,
                role         TEXT    NOT NULL DEFAULT 'viewer',
                token        TEXT    NOT NULL UNIQUE,
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   DOUBLE PRECISION NOT NULL
            );
            -- 기존 배포에서 누락 가능한 컬럼 보강 (idempotent)
            ALTER TABLE vantix_users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT '{DEFAULT_PLAN}';
            ALTER TABLE vantix_users ADD COLUMN IF NOT EXISTS email_verified INTEGER DEFAULT 0;
            ALTER TABLE vantix_users ADD COLUMN IF NOT EXISTS email_verify_token TEXT;
            ALTER TABLE vantix_users ADD COLUMN IF NOT EXISTS projects_changed_at DOUBLE PRECISION DEFAULT NULL;
            ALTER TABLE vantix_users ADD COLUMN IF NOT EXISTS pending_plan TEXT DEFAULT NULL;
            ALTER TABLE vantix_billing_keys ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;
            ALTER TABLE vantix_billing_keys ADD COLUMN IF NOT EXISTS next_retry_at DOUBLE PRECISION DEFAULT NULL;
            ALTER TABLE vantix_billing_keys ADD COLUMN IF NOT EXISTS grace_until DOUBLE PRECISION DEFAULT NULL;
        """)

_init_users_db()

def _create_user(email: str, password: str) -> tuple[int, str]:
    hashed = _pwd_ctx.hash(password)
    token = str(_uuid.uuid4()).replace("-", "")
    with _users_db() as conn:
        cur = conn.execute(
            "INSERT INTO vantix_users (email, hashed_password, created_at, email_verified, email_verify_token) VALUES (?,?,?,0,?) RETURNING id",
            (email.lower().strip(), hashed, time.time(), token)
        )
        return cur.fetchone()["id"], token

def _verify_email_token(token: str) -> dict | None:
    with _users_db() as conn:
        row = conn.execute(
            "SELECT * FROM vantix_users WHERE email_verify_token=? AND is_active=1", (token,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE vantix_users SET email_verified=1, email_verify_token=NULL WHERE id=?", (row["id"],)
        )
        return dict(row)

def _send_verification_email(email: str, token: str, base_url: str):
    verify_url = f"{base_url}/api/auth/verify-email?token={token}"
    try:
        _resend.Emails.send({
            "from": f"Vantix <{RESEND_FROM}>",
            "to": [email],
            "subject": "[Vantix] 이메일 인증을 완료해주세요",
            "html": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#0F766E;margin-bottom:16px;">VANTIX — EMAIL VERIFICATION</div>
  <h2 style="font-family:sans-serif;font-weight:700;font-size:22px;color:#17181A;margin:0 0 12px;">이메일 인증</h2>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 28px;">아래 버튼을 클릭하면 가입이 완료되고 Redmine 연결 단계로 이동합니다. 링크는 24시간 동안 유효합니다.</p>
  <a href="{verify_url}" style="display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:#0F766E;text-decoration:none;padding:14px 28px;">이메일 인증하기 ↗</a>
  <p style="margin:24px 0 0;font-size:11px;color:#8a8d91;">버튼이 작동하지 않으면 아래 링크를 복사해 브라우저에 붙여넣으세요.<br><a href="{verify_url}" style="color:#0F766E;word-break:break-all;">{verify_url}</a></p>
</div>""",
        })
    except Exception as e:
        print(f"[resend] 이메일 발송 실패: {e}")

def _send_invite_email(email: str, inviter_email: str, role: str, base_url: str, is_existing: bool):
    role_label = {"admin": "관리자", "viewer": "뷰어"}.get(role, role)
    action_url = f"{base_url}/connect"
    guide = ("이미 Vantix 계정이 있으시네요. 로그인하면 자동으로 팀에 합류됩니다."
             if is_existing else
             "아래 버튼에서 이 이메일로 가입하시면 자동으로 팀에 합류됩니다.")
    btn_label = "로그인하기 ↗" if is_existing else "가입하고 합류하기 ↗"
    try:
        _resend.Emails.send({
            "from": f"Vantix <{RESEND_FROM}>",
            "to": [email],
            "subject": f"[Vantix] {inviter_email}님이 팀에 초대했습니다",
            "html": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#0F766E;margin-bottom:16px;">VANTIX — TEAM INVITATION</div>
  <h2 style="font-family:sans-serif;font-weight:700;font-size:22px;color:#17181A;margin:0 0 12px;">팀 초대</h2>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 8px;"><b>{inviter_email}</b>님이 당신을 Vantix 팀에 <b>{role_label}</b> 역할로 초대했습니다.</p>
  <p style="font-size:13px;line-height:1.7;color:#46494d;margin:0 0 28px;">{guide}</p>
  <a href="{action_url}" style="display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:#0F766E;text-decoration:none;padding:14px 28px;">{btn_label}</a>
  <p style="margin:24px 0 0;font-size:11px;color:#8a8d91;">합류 후에는 본인의 Redmine API 키로 대시보드를 연결하게 됩니다. 당신의 Redmine 권한 범위 내 프로젝트만 표시됩니다.</p>
</div>""",
        })
    except Exception as e:
        print(f"[resend] 초대 이메일 발송 실패: {e}")

def _send_billing_email(email: str, kind: str, plan: str):
    """자동 갱신 결제 관련 이메일 발송. kind: 'fail1' | 'fail2' | 'downgraded'"""
    plan_label = {"pro": "Pro", "business": "Business"}.get(plan, plan.capitalize())
    subjects = {
        "fail1":      f"[Vantix] {plan_label} 구독 결제에 실패했습니다",
        "fail2":      f"[Vantix] 결제 재시도 실패 — 내일 Free로 변경됩니다",
        "downgraded": f"[Vantix] 플랜이 Free로 변경되었습니다",
    }
    bodies = {
        "fail1": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#DC2626;margin-bottom:16px;">VANTIX — 결제 실패</div>
  <h2 style="font-family:sans-serif;font-weight:700;font-size:22px;color:#17181A;margin:0 0 12px;">{plan_label} 구독 결제 실패</h2>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 12px;">정기결제 처리 중 오류가 발생했습니다. 등록하신 카드 정보를 확인해주세요.</p>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 28px;"><b>3일간 유예기간</b>이 적용되며, 이 기간 동안 결제 재시도가 이루어집니다. 유예기간 내 결제가 완료되면 구독이 정상 유지됩니다.</p>
  <a href="https://vantix.app/connect" style="display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:#0F766E;text-decoration:none;padding:14px 28px;">결제 수단 변경하기 ↗</a>
  <p style="margin:24px 0 0;font-size:11px;color:#8a8d91;">문의: support@vantix.app</p>
</div>""",
        "fail2": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#DC2626;margin-bottom:16px;">VANTIX — 결제 재시도 실패</div>
  <h2 style="font-family:sans-serif;font-weight:700;font-size:22px;color:#17181A;margin:0 0 12px;">결제 재시도에 실패했습니다</h2>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 12px;">{plan_label} 구독 결제 재시도에 실패했습니다. <b>내일까지 결제가 완료되지 않으면 Free 플랜으로 자동 변경됩니다.</b></p>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 28px;">지금 바로 결제 수단을 변경하여 서비스를 유지하세요.</p>
  <a href="https://vantix.app/connect" style="display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:#DC2626;text-decoration:none;padding:14px 28px;">지금 결제 수단 변경하기 ↗</a>
  <p style="margin:24px 0 0;font-size:11px;color:#8a8d91;">문의: support@vantix.app</p>
</div>""",
        "downgraded": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#6A6E73;margin-bottom:16px;">VANTIX — 플랜 변경</div>
  <h2 style="font-family:sans-serif;font-weight:700;font-size:22px;color:#17181A;margin:0 0 12px;">Free 플랜으로 변경되었습니다</h2>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 12px;">{plan_label} 구독 결제가 최종 실패하여 Free 플랜으로 변경되었습니다.</p>
  <p style="font-size:14px;line-height:1.7;color:#46494d;margin:0 0 28px;">언제든지 재구독하여 유료 기능을 다시 이용하실 수 있습니다.</p>
  <a href="https://vantix.app/connect" style="display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#fff;background:#0F766E;text-decoration:none;padding:14px 28px;">다시 구독하기 ↗</a>
  <p style="margin:24px 0 0;font-size:11px;color:#8a8d91;">문의: support@vantix.app</p>
</div>""",
    }
    try:
        _resend.Emails.send({
            "from": f"Vantix <{RESEND_FROM}>",
            "to": [email],
            "subject": subjects[kind],
            "html": bodies[kind],
        })
    except Exception as e:
        print(f"[resend] 결제 이메일 발송 실패({kind}): {e}")


def _get_user_by_email(email: str) -> dict | None:
    with _users_db() as conn:
        row = conn.execute("SELECT * FROM vantix_users WHERE email=? AND is_active=1", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None

def _get_user_by_id(user_id: int) -> dict | None:
    with _users_db() as conn:
        row = conn.execute("SELECT * FROM vantix_users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        return dict(row) if row else None

def _verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)

# ---------- Phase 4: 플랜 ----------
def _get_user_plan(user_id: int | None) -> str:
    """유저의 유효 플랜. 워크스페이스 멤버면 오너 플랜을 상속. 익명/데모 → free."""
    if not user_id:
        return DEFAULT_PLAN
    owner_id = _plan_owner_id(user_id)
    with _users_db() as conn:
        row = conn.execute("SELECT plan FROM vantix_users WHERE id=? AND is_active=1", (owner_id,)).fetchone()
        return (row["plan"] if row and row["plan"] else DEFAULT_PLAN)

def _set_user_plan(user_id: int, plan: str):
    with _users_db() as conn:
        conn.execute("UPDATE vantix_users SET plan=? WHERE id=?", (plan, user_id))

# ---------- Phase 4: 선택 프로젝트 ----------
def _get_user_projects(user_id: int) -> list[dict]:
    with _users_db() as conn:
        rows = conn.execute(
            "SELECT project_id, project_name FROM vantix_user_projects WHERE user_id=? ORDER BY created_at",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def _get_user_project_ids(user_id: int | None) -> set[str]:
    if not user_id:
        return set()
    return {str(p["project_id"]) for p in _get_user_projects(user_id)}

def _set_user_projects(user_id: int, projects: list[dict]) -> list[dict]:
    """선택 프로젝트 일괄 교체. 플랜 개수 제한 초과 시 ValueError.
    projects: [{"project_id": "..", "project_name": ".."}, ...]"""
    plan = _get_user_plan(user_id)
    limit = plan_project_limit(plan)
    # 중복 project_id 제거 (입력 순서 유지)
    seen, cleaned = set(), []
    for p in projects:
        pid = str(p.get("project_id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        cleaned.append({"project_id": pid, "project_name": (p.get("project_name") or "").strip()})
    if limit != -1 and len(cleaned) > limit:
        raise ValueError(f"{plan} 플랜은 프로젝트 {limit}개까지 선택할 수 있습니다 (요청: {len(cleaned)}개)")
    with _users_db() as conn:
        conn.execute("DELETE FROM vantix_user_projects WHERE user_id=?", (user_id,))
        now = time.time()
        for i, p in enumerate(cleaned):
            conn.execute(
                "INSERT INTO vantix_user_projects (user_id, project_id, project_name, created_at) VALUES (?,?,?,?)",
                (user_id, p["project_id"], p["project_name"], now + i * 1e-6)
            )
    return cleaned

def _save_redmine_connection(user_id: int, redmine_url: str, api_key: str,
                              default_project_id: str = "", updated_after: str = ""):
    encrypted_key = _encrypt_key(api_key)
    with _users_db() as conn:
        existing = conn.execute("SELECT id FROM vantix_redmine_connections WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE vantix_redmine_connections SET redmine_url=?, api_key=?, default_project_id=?, updated_after=? WHERE user_id=?",
                (redmine_url, encrypted_key, default_project_id, updated_after, user_id)
            )
        else:
            conn.execute(
                "INSERT INTO vantix_redmine_connections (user_id, redmine_url, api_key, default_project_id, updated_after, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, redmine_url, encrypted_key, default_project_id, updated_after, time.time())
            )

def _get_redmine_connection(user_id: int) -> dict | None:
    with _users_db() as conn:
        row = conn.execute("SELECT * FROM vantix_redmine_connections WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["api_key"] = _decrypt_key(d["api_key"])
        return d

# ---------- Phase 4: 팀(워크스페이스) ----------
def _ensure_workspace(owner_user_id: int) -> int:
    """오너의 워크스페이스 id 반환. 없으면 생성하고 오너를 owner 멤버로 등록."""
    with _users_db() as conn:
        row = conn.execute("SELECT id FROM vantix_workspaces WHERE owner_user_id=?", (owner_user_id,)).fetchone()
        if row:
            return row["id"]
        now = time.time()
        cur = conn.execute(
            "INSERT INTO vantix_workspaces (owner_user_id, created_at) VALUES (?,?) RETURNING id", (owner_user_id, now)
        )
        ws_id = cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO vantix_workspace_members (workspace_id, user_id, role, created_at) VALUES (?,?,'owner',?) ON CONFLICT DO NOTHING",
            (ws_id, owner_user_id, now)
        )
        return ws_id

def _get_membership(user_id: int | None) -> dict | None:
    """유저가 속한 워크스페이스 멤버십. {workspace_id, role, owner_user_id} 또는 None."""
    if not user_id:
        return None
    with _users_db() as conn:
        row = conn.execute(
            "SELECT m.workspace_id, m.role, w.owner_user_id "
            "FROM vantix_workspace_members m JOIN vantix_workspaces w ON w.id=m.workspace_id "
            "WHERE m.user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

def _get_workspace_role(user_id: int | None) -> str:
    """유저의 역할. 워크스페이스 미소속이면 'owner'(자기 자신이 곧 오너)."""
    m = _get_membership(user_id)
    return m["role"] if m else "owner"

def _can_edit(user_id: int | None) -> bool:
    """이슈 수정·리포트 발송 권한. owner/admin만 True. viewer는 False."""
    return _get_workspace_role(user_id) in ("owner", "admin")

def _plan_owner_id(user_id: int | None) -> int | None:
    """플랜·결제 기준이 되는 유저 id. 멤버면 워크스페이스 오너, 아니면 자기 자신."""
    if not user_id:
        return None
    m = _get_membership(user_id)
    return m["owner_user_id"] if m else user_id

_PROJECT_CHANGE_COOLDOWN = 7 * 24 * 3600  # 7일(초)

def _get_projects_changed_at(user_id: int | None) -> float | None:
    if not user_id:
        return None
    with _users_db() as conn:
        row = conn.execute("SELECT projects_changed_at FROM vantix_users WHERE id=?", (user_id,)).fetchone()
    return row["projects_changed_at"] if row else None

def _projects_cooldown_days_left(user_id: int | None) -> int:
    """남은 쿨다운 일수. 0이면 변경 가능."""
    ts = _get_projects_changed_at(user_id)
    if not ts:
        return 0
    import time
    elapsed = time.time() - ts
    remaining = _PROJECT_CHANGE_COOLDOWN - elapsed
    return max(0, int(remaining / 86400) + (1 if remaining % 86400 > 0 else 0))

def _get_workspace_members(workspace_id: int) -> list[dict]:
    """워크스페이스 멤버 목록 (이메일·역할 포함)."""
    with _users_db() as conn:
        rows = conn.execute(
            "SELECT m.user_id, m.role, m.created_at, u.email "
            "FROM vantix_workspace_members m JOIN vantix_users u ON u.id=m.user_id "
            "WHERE m.workspace_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, m.created_at",
            (workspace_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def _count_workspace_seats(workspace_id: int) -> int:
    """현재 멤버 수 + 대기중 초대 수 (좌석 점유 기준)."""
    with _users_db() as conn:
        m = conn.execute("SELECT COUNT(*) c FROM vantix_workspace_members WHERE workspace_id=?", (workspace_id,)).fetchone()["c"]
        i = conn.execute("SELECT COUNT(*) c FROM vantix_invitations WHERE workspace_id=? AND status='pending'", (workspace_id,)).fetchone()["c"]
        return m + i

def _add_member(workspace_id: int, user_id: int, role: str):
    """유저를 워크스페이스 멤버로 추가 (이미 있으면 무시)."""
    with _users_db() as conn:
        conn.execute(
            "INSERT INTO vantix_workspace_members (workspace_id, user_id, role, created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (workspace_id, user_id, role, time.time())
        )

def _claim_pending_invites(user_id: int, email: str):
    """이메일로 온 대기중 초대를 가입/로그인 시 자동 합류 처리."""
    m = _get_membership(user_id)
    if m:  # 이미 어떤 워크스페이스 소속이면 스킵
        return
    with _users_db() as conn:
        inv = conn.execute(
            "SELECT * FROM vantix_invitations WHERE email=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (email.lower().strip(),)
        ).fetchone()
        if not inv:
            return
    _add_member(inv["workspace_id"], user_id, inv["role"])
    with _users_db() as conn:
        conn.execute("UPDATE vantix_invitations SET status='accepted' WHERE id=?", (inv["id"],))

# 세션에 user_id를 태그하기 위한 확장 저장소 (메모리)
_session_user_map: dict[str, int] = {}  # token → user_id

def _tag_session_user(token: str, user_id: int):
    _session_user_map[token] = user_id

def _get_session_user_id(token: str) -> int | None:
    return _session_user_map.get(token)

# ---------- Phase 4: 요청 → user_id, 플랜 게이팅 ----------
def _current_user_id(request: Request) -> int | None:
    """요청에서 로그인 유저 id 추출. vx_user_id 쿠키 우선, 세션맵 폴백.
    익명/데모 세션은 None (= free 취급)."""
    uid_str = request.cookies.get("vx_user_id")
    if uid_str and uid_str.isdigit():
        return int(uid_str)
    token = request.cookies.get("vx_session") or request.headers.get("X-VX-Session")
    return _get_session_user_id(token or "") if token else None



def _check_project_access(request: Request, project_id: str):
    """선택 프로젝트 게이트. 로그인 유저가 프로젝트를 선택해 둔 경우,
    그 목록 밖의 project_id 접근을 403으로 막는다.
    - 익명/데모(user_id 없음) 또는 아직 선택 안 한 유저: 통과 (선택 UI는 3단계).
    - project_id 미지정(전체 보기): 통과."""
    pid = str(project_id or "").strip()
    if not pid:
        return
    uid = _current_user_id(request)
    if not uid:
        return
    selected = _get_user_project_ids(uid)
    if not selected:
        return
    if pid not in selected:
        raise HTTPException(
            status_code=403,
            detail={"error": "project_not_allowed", "project_id": pid,
                    "plan": _get_user_plan(uid),
                    "message": "선택한 프로젝트가 아닙니다. 요금제에서 모니터링 프로젝트를 변경하세요."},
        )

# ============================================================

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
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ==================== 라우터 등록 (REFACTOR_PLAN.md Phase A) ====================
# connect/callouts/team/admin은 app/routers/*.py로 이전됨.
# 각 라우터는 `import main as _m`으로 아래에 정의된 세션/DB/캐시 등 공용 인프라를 참조한다.
def _register_routers():
    from app.routers import connect as _connect_router
    from app.routers import callouts as _callouts_router
    from app.routers import team as _team_router
    from app.routers import admin as _admin_router
    app.include_router(_connect_router.router)
    app.include_router(_callouts_router.router)
    app.include_router(_team_router.router)
    app.include_router(_admin_router.router)

# ==================== 캐시 ====================
_cache = {}

# ==================== 리포트 공유 저장소 ====================
_report_store: dict = {}   # token → {html, created_at}
REPORT_TTL = 86400         # 24시간

# ==================== AI 캐시 ====================
_ai_cache: dict = {}
AI_CACHE_TTL = 3600  # 1시간

# ==================== 이슈 모달 메타 캐시 (상태/멤버/버전) ====================
_modal_meta_cache: dict = {}
MODAL_META_TTL = 300  # 5분

def _get_modal_meta_cache(key: str):
    entry = _modal_meta_cache.get(key)
    if not entry:
        return None
    if (datetime.now() - entry["at"]).total_seconds() > MODAL_META_TTL:
        del _modal_meta_cache[key]
        return None
    return entry["data"]

def _set_modal_meta_cache(key: str, data):
    _modal_meta_cache[key] = {"data": data, "at": datetime.now()}

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
            model=AI_MODEL,
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
def _parse_ua(ua: str) -> dict:
    u = ua.lower()
    if "ipad" in u or ("android" in u and "mobile" not in u):
        device = "태블릿"
    elif "mobile" in u or "android" in u or "iphone" in u:
        device = "모바일"
    else:
        device = "데스크탑"
    if "edg/" in u or "edge/" in u:
        browser = "Edge"
    elif "opr/" in u or "opera" in u:
        browser = "Opera"
    elif "chrome/" in u:
        browser = "Chrome"
    elif "firefox/" in u:
        browser = "Firefox"
    elif "safari/" in u:
        browser = "Safari"
    else:
        browser = "기타"
    if "windows" in u:
        os_name = "Windows"
    elif "iphone" in u or "ipad" in u:
        os_name = "iOS"
    elif "android" in u:
        os_name = "Android"
    elif "macintosh" in u or "mac os" in u:
        os_name = "macOS"
    elif "linux" in u:
        os_name = "Linux"
    else:
        os_name = "기타"
    return {"device": device, "browser": browser, "os": os_name}

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
    monitor_path = os.path.join(os.path.dirname(__file__), "monitor_config.json")
    try:
        with open(monitor_path, "r", encoding="utf-8") as f:
            all_cfg = json.load(f)
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
        report  = build_report_data(dashboard, project_label=DEFAULT_PROJECT_ID or "전체", project_id=DEFAULT_PROJECT_ID or "")
        html    = render_html_report(report)
        subject = f"[Vantix] 주간 리포트 {report.period_label}"
        result  = send_report_email(html, subject, EMAIL_CFG)
        print(f"  {result}")
    except Exception as e:
        print(f"  리포트 오류: {e}")

# ── Risk 스냅샷 저장 ──────────────────────────────────────────
RISK_HISTORY_PATH = os.getenv("RISK_HISTORY_PATH", os.path.join(os.path.dirname(__file__), "risk_history.json"))

def save_risk_snapshot():
    """
    매주 지정 요일에 현재 캐시의 risk_score를 스냅샷으로 저장.
    키: "all" 또는 "project_{id}"
    """
    from datetime import date
    today = date.today().isoformat()

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

    if _DATABASE_URL:
        # DB 저장
        _db_save_history_entry("all", today, avg_score, avg_level, avg_overdue, avg_urgent)
        for p in projects:
            pname = p.get("name", "")
            if not pname:
                continue
            identifier = pid_to_name.get(pname, pname)
            _db_save_history_entry(
                f"project_{identifier}", today,
                round(p["risk_score"], 1), p["risk_level"],
                p.get("overdue", 0), p.get("urgent", 0)
            )
    else:
        # JSON 폴백 (로컬)
        try:
            with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            history = {}

        history.setdefault("all", [])
        if not history["all"] or history["all"][-1]["date"] != today:
            history["all"].append({"date": today, "score": avg_score, "level": avg_level,
                                   "overdue": avg_overdue, "urgent": avg_urgent})
        history["all"] = history["all"][-52:]

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

def _job_billing_renewal():
    """매일 새벽 2시 — 만료/재시도 대상 구독 자동 갱신. 실패 시 3일 유예 + 2회 재시도."""
    if not _DATABASE_URL:
        return
    now = time.time()
    try:
        with _users_db() as conn:
            rows = conn.execute("""
                SELECT bk.id, bk.user_id, bk.billing_key, bk.plan,
                       bk.retry_count, bk.grace_until,
                       u.email
                FROM vantix_billing_keys bk
                JOIN vantix_users u ON u.id = bk.user_id AND u.is_active = 1
                WHERE bk.status = 'active'
                  AND (
                    (bk.retry_count = 0 AND bk.expires_at <= ?)
                    OR (bk.retry_count > 0 AND bk.next_retry_at IS NOT NULL AND bk.next_retry_at <= ?)
                  )
            """, (now, now)).fetchall()
    except Exception as e:
        print(f"[billing renewal] DB 조회 실패: {e}")
        return

    for row in rows:
        bk_id     = row["id"]
        uid       = row["user_id"]
        bk_enc    = row["billing_key"]
        plan      = row["plan"]
        retry     = row["retry_count"] or 0
        email     = row["email"]

        billing_key = _decrypt_key(bk_enc)
        payment_id  = f"vantix-renew-{plan}-{uid}-{int(now)}-r{retry}"

        print(f"[billing renewal] uid={uid} plan={plan} retry={retry}")
        result      = _charge_billing_key(billing_key, payment_id, plan, uid, email)
        http_status = result.get("_http_status", 0)
        payment     = result.get("payment") or {}
        paid        = http_status == 200 and bool(payment) and payment.get("status", "PAID") == "PAID"

        if paid:
            new_expires = now + 31 * 86400
            with _users_db() as conn:
                conn.execute("""
                    UPDATE vantix_billing_keys
                    SET expires_at=?, retry_count=0, next_retry_at=NULL, grace_until=NULL
                    WHERE id=?
                """, (new_expires, bk_id))
                conn.execute("""
                    INSERT INTO vantix_payment_history
                      (user_id, payment_id, billing_key_id, plan, amount, status, paid_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (uid, payment_id, bk_id, plan, PLAN_PRICES[plan], "paid", now))
                # pending_plan 있으면 이 갱신 시점에 적용
                prow = conn.execute("SELECT pending_plan FROM vantix_users WHERE id=?", (uid,)).fetchone()
                if prow and prow["pending_plan"]:
                    _set_user_plan(uid, prow["pending_plan"])
                    conn.execute("UPDATE vantix_users SET pending_plan=NULL WHERE id=?", (uid,))
            print(f"[billing renewal] 갱신 성공 uid={uid}")
        else:
            if retry == 0:
                # 1차 실패 — 유예기간 3일, 내일 재시도
                with _users_db() as conn:
                    conn.execute("""
                        UPDATE vantix_billing_keys
                        SET retry_count=1, next_retry_at=?, grace_until=?
                        WHERE id=?
                    """, (now + 86400, now + 3 * 86400, bk_id))
                _send_billing_email(email, "fail1", plan)
                print(f"[billing renewal] 1차 실패 uid={uid}, 유예 3일")
            elif retry == 1:
                # 2차 실패 — 모레(day 3) 마지막 재시도
                with _users_db() as conn:
                    conn.execute("""
                        UPDATE vantix_billing_keys
                        SET retry_count=2, next_retry_at=?
                        WHERE id=?
                    """, (now + 2 * 86400, bk_id))
                _send_billing_email(email, "fail2", plan)
                print(f"[billing renewal] 2차 실패 uid={uid}, 마지막 재시도 예정")
            else:
                # 3차 실패 — 최종 다운그레이드
                with _users_db() as conn:
                    conn.execute("""
                        UPDATE vantix_billing_keys
                        SET status='failed', retry_count=3, next_retry_at=NULL
                        WHERE id=?
                    """, (bk_id,))
                _set_user_plan(uid, "free")
                _send_billing_email(email, "downgraded", plan)
                print(f"[billing renewal] 최종 실패 → free uid={uid}")


def _job_cleanup_callouts():
    if not _DATABASE_URL:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM vantix_callouts WHERE expires_at IS NOT NULL AND expires_at < %s", (time.time(),))
            conn.commit()
        global _callout_cache; _callout_cache = None
    except Exception as e:
        print(f"[callout cleanup] {e}")

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
_scheduler.add_job(_job_cleanup_callouts, CronTrigger(
    hour=3, minute=0, timezone="Asia/Seoul"
), id="callout_cleanup")
_scheduler.add_job(_job_billing_renewal, CronTrigger(
    hour=2, minute=0, timezone="Asia/Seoul"
), id="billing_renewal")
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

# ==================== Redmine 클라이언트 (REFACTOR_PLAN.md Phase B) ====================
# 실제 fetch 로직은 app/redmine/client.py의 RedmineClient로 이전됨.
# 아래 4개 함수는 main.py 전역에 78곳 넘게 퍼져있는 기존 호출부
# (fetch(..., redmine_url=, api_key=) 형태)를 건드리지 않기 위한 호환 래퍼다.
# 신규 코드는 이 래퍼 대신 app.redmine.factory.build_client()로 RedmineClient를
# 직접 만들어 client.fetch()/get_issues() 등을 쓰는 걸 권장한다.
from app.redmine.factory import build_client as _build_redmine_client


def fetch(path, params=None, retries=2, redmine_url=None, api_key=None):
    return _build_redmine_client(redmine_url, api_key).fetch(path, params, retries)


def fetch_all(path, key, base_params=None, redmine_url=None, api_key=None):
    return _build_redmine_client(redmine_url, api_key).fetch_all(path, key, base_params)


def days_diff(due_date_str):
    if not due_date_str:
        return None
    try:
        return (datetime.strptime(due_date_str, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def get_projects(redmine_url=None, api_key=None):
    return _build_redmine_client(redmine_url, api_key).get_projects()


def get_issues(project_id="", updated_after="2026-03-01", redmine_url=None, api_key=None):
    return _build_redmine_client(redmine_url, api_key).get_issues(project_id, updated_after)


def build_dashboard_data(project_id="", updated_after="2026-03-01", redmine_url=None, api_key=None):
    issues = get_issues(project_id, updated_after, redmine_url=redmine_url, api_key=api_key)

    # 그룹 API 직접 조회로 user → group 매핑 (이름 형식 무관)
    user_group_map = {}
    if project_id:
        try:
            _, user_group_map = _build_group_map(project_id, redmine_url, api_key)
        except Exception as e:
            print(f"[group_map] {e}")

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
    future_7 = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    imminent_issues = []
    for uname, ud in users_data.items():
        for i in ud.get("issues", []):
            if (i.get("due_date") and
                today_str <= i["due_date"] <= future_7 and
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
        "users_data":       {k: {"issues": v["issues"], "projects": list(v["projects"]), "group": v.get("group", ""), "short_name": v.get("short_name", k), "dept": dept_name(k)} for k, v in users_data.items()},
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
    try:
        from datetime import date, timedelta

        # 1. 그룹-멤버 매핑
        if project_id:
            group_map, user_to_group = _build_group_map(project_id, redmine_url, api_key)
        else:
            # 전체 프로젝트: /groups.json으로 Redmine 전체 그룹 직접 조회
            all_groups = fetch("/groups.json", redmine_url=redmine_url, api_key=api_key).get("groups", [])
            group_map = {g["name"]: {"id": g["id"], "name": g["name"], "user_count": 0, "members": []} for g in all_groups}
            from concurrent.futures import ThreadPoolExecutor
            def _fetch_members_all(gname):
                try:
                    data = fetch(f"/groups/{group_map[gname]['id']}.json?include=users", redmine_url=redmine_url, api_key=api_key)
                    return gname, [u["name"] for u in data.get("group", {}).get("users", []) if u.get("name")]
                except Exception:
                    return gname, []
            with ThreadPoolExecutor(max_workers=5) as ex:
                for gname, members in ex.map(_fetch_members_all, list(group_map.keys())):
                    group_map[gname]["members"] = members
                    group_map[gname]["user_count"] = len(members)
            user_to_group = {uname: gname for gname, ginfo in group_map.items() for uname in ginfo["members"]}

        if not group_map:
            return []

        # 2. 이슈 전체 로드
        eight_weeks_ago = (date.today() - timedelta(days=56)).strftime("%Y-%m-%d")
        issues = get_issues(project_id, updated_after=eight_weeks_ago, redmine_url=redmine_url, api_key=api_key)

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
            "created_on":   v.get("created_on", ""),
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
    is_demo = "true" if token in _demo_tokens else "false"
    html = html.replace("__IS_DEMO__", is_demo)
    html = html.replace("__PORTONE_STORE_ID__", PORTONE_STORE_ID)
    html = html.replace("__PORTONE_CHANNEL_KEY__", PORTONE_CHANNEL_KEY)
    html = html.replace("__PORTONE_CHANNEL_KEY_INICIS__", PORTONE_CHANNEL_KEY_INICIS)
    html = html.replace("__PORTONE_CHANNEL_KEY_TOSSPAY__", PORTONE_CHANNEL_KEY_TOSSPAY)
    return HTMLResponse(content=html)

@app.get("/api/projects")
async def api_projects(request: Request, s: dict = Depends(_require_session)):
    projects = get_projects(redmine_url=s["url"], api_key=s["key"])
    return [{"identifier": p["identifier"], "name": p["name"]} for p in projects]


@app.get("/api/data")
async def api_data(request: Request, project_id: str = "", updated_after: str = "2026-03-01", force: bool = False, s: dict = Depends(_require_session)):
    _check_project_access(request, project_id)
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
                threading.Thread(target=_bg_refresh, args=(project_id, updated_after, r_url, r_key, key), daemon=True).start()
            print(f"  캐시 히트(stale): {key} ({age})")
            return {**entry["data"], "cached": True, "cache_age": age, "stale": True}

    print(f"  Redmine fetch: {key}")
    data = build_dashboard_data(project_id, updated_after, redmine_url=r_url, api_key=r_key)
    set_cache(project_id, updated_after, data, redmine_url=r_url)

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
            raw = _call_claude(prompt, max_tokens=800)
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            signals = json.loads(match.group()) if match else []
            if signals:
                _set_ai_cache(cache_k, json.dumps(signals, ensure_ascii=False))
                print(f"  action-signals 프리생성 완료: {pid}")
        except Exception as e:
            print(f"  action-signals 프리생성 실패 {pid}: {e}")

    threading.Thread(target=_bg_generate_signals, args=(project_id, r_url), daemon=True).start()

    return {**data, "cached": False, "cache_age": None}


@app.get("/api/groups")
async def api_groups(request: Request, project_id: str = "", s: dict = Depends(_require_session)):
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
        proj_key = f"project_{project_id}" if project_id else "all"
        if _DATABASE_URL:
            proj_hist = _db_load_history(proj_key)
            if len(proj_hist) < 2:
                proj_hist = _db_load_history("all")
        else:
            with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
                hist = json.load(f)
            proj_hist = hist.get(proj_key, hist.get("all", []))
        if len(proj_hist) >= 2:
            prev = proj_hist[-2]
            risk_change  = round(proj_hist[-1]["score"] - prev["score"], 1)
            prev_overdue = prev.get("overdue")
            prev_urgent  = prev.get("urgent")
            prev_date    = prev.get("date")
    except Exception as e:
        print(f"[snapshot/prev] {e}")

    # ── 담당자 위험도 랭킹
    users_data = dashboard.get("users_data", {})
    overload_raw = []
    for uname, ud in users_data.items():
        issues = ud.get("issues", [])
        open_issues = [i for i in issues if i["status"] not in CLOSED_SET and i["status"] not in HOLD_SET]
        if not open_issues:
            continue
        total = len(open_issues)
        overdue_issues = [i for i in open_issues if i.get("due_date") and i["due_date"] < today_str]
        urgent_issues  = [i for i in open_issues if i.get("due_date") and today_str <= i["due_date"] <= (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")]
        overdue_count = len(overdue_issues)
        urgent_count  = len(urgent_issues)
        delays = []
        for i in overdue_issues:
            try:
                d = (date.today() - date.fromisoformat(i["due_date"])).days
                if d > 0:
                    delays.append(d)
            except Exception:
                pass
        avg_delay = round(sum(delays) / len(delays)) if delays else None
        max_delay = max(delays) if delays else None
        score = round((overdue_count / total * 60) + (urgent_count / total * 30), 1)
        norm_score = min(round(score * 100 / 60), 100)
        overload_raw.append({
            "name":      ud.get("short_name", uname),
            "open":      total,
            "overdue":   overdue_count,
            "urgent":    urgent_count,
            "avg_delay": avg_delay,
            "max_delay": max_delay,
            "risk_score": norm_score,
            "delta":     None,
        })
    overload_raw.sort(key=lambda x: -x["risk_score"])
    max_open = overload_raw[0]["open"] if overload_raw else 1
    overload = []
    for x in overload_raw[:5]:
        pct = round(x["open"] / max_open * 100)
        score = x["risk_score"]
        overload.append({
            **x,
            "pct":   pct,
            "level": "danger"  if score >= 60 else ("warning" if score >= 30 else "ok"),
            "label": "위험"    if score >= 60 else ("주의"    if score >= 30 else "안정"),
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


@app.delete("/api/cache/{project_id}")
async def clear_project_cache(project_id: str, s: dict = Depends(_require_session)):
    # 캐시 키 형식: "{redmine_url}|{project_id}|{updated_after}"
    # project_id가 두 번째 세그먼트인 것만 삭제
    removed = []
    for k in list(_cache.keys()):
        parts = k.split("|", 2)
        if len(parts) >= 2 and parts[1] == project_id:
            removed.append(k)
    for k in removed:
        _cache.pop(k, None)
    return {"ok": True, "removed": len(removed)}


@app.get("/api/risk-history")
async def api_risk_history(project_id: str = "", weeks: int = 12, s: dict = Depends(_require_session)):
    """
    저장된 스냅샷에서 최근 N주 반환.
    DB 우선, 없으면 JSON 폴백.
    """
    from datetime import date as _date

    # ── 히스토리 로드 (DB or JSON)
    if _DATABASE_URL:
        # DB 모드: key 결정
        if not project_id:
            key = "all"
        else:
            direct_key = f"project_{project_id}"
            records_try = _db_load_history(direct_key)
            if records_try:
                key = direct_key
            else:
                # 부분 매칭: DB에서 project_id 포함 키 탐색
                try:
                    with _db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT DISTINCT hist_key FROM risk_history WHERE hist_key LIKE %s LIMIT 1",
                                (f"%{project_id}%",)
                            )
                            row = cur.fetchone()
                            key = row[0] if row else "all"
                except Exception:
                    key = "all"

        records = _db_load_history(key)
        if key != "all" and len(records) < 3:
            records = _db_load_history("all")
            key = "all"

        # All Projects 뷰 — 프로젝트별 breakdown
        history = {}
        if key == "all":
            try:
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT hist_key, date, score, level FROM risk_history WHERE hist_key LIKE 'project_%' ORDER BY date ASC"
                        )
                        for row in cur.fetchall():
                            history.setdefault(row[0], []).append({"date": row[1], "score": row[2], "level": row[3]})
            except Exception as e:
                print(f"[history/db] {e}")
    else:
        # JSON 폴백
        try:
            with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"history": []}

        if not project_id:
            key = "all"
        else:
            direct_key = f"project_{project_id}"
            if direct_key in history:
                key = direct_key
            else:
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
        if key != "all" and len(records) < 3:
            records = history.get("all", [])
            key = "all"

    # ── 월요일 기준 주별 그룹핑
    week_map = {}
    for r in records:
        try:
            d = _date.fromisoformat(r["date"])
            monday = d - timedelta(days=d.weekday())
            week_map[monday.isoformat()] = r
        except Exception:
            continue

    sorted_weeks = sorted(week_map.items())[-weeks:]

    # All Projects 뷰 — 주별 프로젝트 breakdown
    proj_week_map = {}
    if key == "all":
        for hkey, hrecords in history.items():
            if not hkey.startswith("project_"):
                continue
            proj_name = hkey[len("project_"):]
            for pr in hrecords:
                try:
                    pd = _date.fromisoformat(pr["date"])
                    pm_str = (pd - timedelta(days=pd.weekday())).isoformat()
                    proj_week_map.setdefault(pm_str, {})[proj_name] = pr
                except Exception:
                    continue

    result = []
    for idx, (monday_str, r) in enumerate(sorted_weeks, 1):
        item = {"week": f"W{idx}", "score": r["score"], "level": r["level"], "date": monday_str}
        if key == "all" and monday_str in proj_week_map:
            projs = [{"name": pn, "score": pr["score"], "level": pr["level"]}
                     for pn, pr in proj_week_map[monday_str].items()]
            projs.sort(key=lambda x: x["score"], reverse=True)
            item["projects"] = projs
        result.append(item)

    return {"history": result}



@app.get("/api/visitors")
async def visitors(request: Request, sid: str = ""):
    key = sid.strip() if sid.strip() else request.client.host
    _visitors[key] = datetime.now()
    active = get_active_visitors()
    return {"count": len(active)}


# ==================== ANALYTICS & FEEDBACK ====================
# /api/track, /api/feedback, _build_filters, _require_admin, /api/admin/*, /admin 라우트는
# app/routers/admin.py로 이전됨 (REFACTOR_PLAN Phase A)

# api_admin_stats ~ admin_page 함수 본문은 app/routers/admin.py로 이전됨 (REFACTOR_PLAN Phase A)




# ==================== AUTH 엔드포인트 ====================

@app.post("/api/auth/signup")
async def api_auth_signup(request: Request):
    """이메일 + 비밀번호로 계정 생성 → 인증 이메일 발송"""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        raise HTTPException(status_code=400, detail="유효한 이메일을 입력하세요")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다")

    existing = _get_user_by_email(email)
    if existing:
        if not existing.get("email_verified"):
            # 미인증 계정 재가입 시도 → 인증 이메일 재발송
            token = str(_uuid.uuid4()).replace("-", "")
            with _users_db() as conn:
                conn.execute("UPDATE vantix_users SET email_verify_token=? WHERE id=?", (token, existing["id"]))
            base_url = str(request.base_url).rstrip("/")
            _send_verification_email(email, token, base_url)
            return JSONResponse({"ok": True, "email_sent": True, "email": email})
        raise HTTPException(status_code=409, detail="이미 사용 중인 이메일입니다")

    user_id, token = _create_user(email, password)
    base_url = str(request.base_url).rstrip("/")
    _send_verification_email(email, token, base_url)
    return JSONResponse({"ok": True, "email_sent": True, "email": email})


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    """로그인 → 저장된 Redmine 연결이 있으면 세션 자동 발급"""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    user = _get_user_by_email(email)
    if not user or not _verify_password(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")

    if not user.get("email_verified"):
        raise HTTPException(status_code=403, detail="email_not_verified")

    _claim_pending_invites(user["id"], user["email"])

    conn_info = _get_redmine_connection(user["id"])
    if conn_info:
        # Redmine 연결이 저장돼 있으면 세션 자동 발급
        token = str(_uuid.uuid4())
        _save_session(token, conn_info["redmine_url"], conn_info["api_key"], time.time())
        _tag_session_user(token, user["id"])
        response = JSONResponse({"ok": True, "has_connection": True, "email": email})
        response.set_cookie("vx_session", token, httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
        response.set_cookie("vx_user_id", str(user["id"]), httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
        return response
    else:
        # Redmine 연결 없음 — 자동 로그인 상태로 두되(vx_user_id), 대시보드는 연결 후 진입
        response = JSONResponse({"ok": True, "has_connection": False, "email": email})
        response.set_cookie("vx_user_id", str(user["id"]), httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
        response.set_cookie("vx_pending_uid", str(user["id"]), httponly=True, max_age=600, samesite="lax", secure=True)
        return response


@app.get("/api/auth/verify-email")
async def api_auth_verify_email(token: str, request: Request):
    """이메일 인증 링크 클릭 → 인증 완료 후 Redmine 연결 페이지로 이동"""
    user = _verify_email_token(token)
    if not user:
        return HTMLResponse("<h3>인증 링크가 유효하지 않거나 이미 사용되었습니다.</h3><a href='/connect'>홈으로</a>", status_code=400)

    conn_info = _get_redmine_connection(user["id"])
    response = RedirectResponse(url="/connect?verified=1", status_code=302)
    if conn_info:
        # Redmine 연결이 이미 있으면 세션 발급 후 대시보드로
        sess_token = str(_uuid.uuid4())
        _save_session(sess_token, conn_info["redmine_url"], conn_info["api_key"], time.time())
        _tag_session_user(sess_token, user["id"])
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie("vx_session", sess_token, httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
        response.set_cookie("vx_user_id", str(user["id"]), httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
    else:
        # 자동 로그인 상태로 connect 랜딩에서 대기 (Redmine 연결은 사용자가 원할 때)
        response.set_cookie("vx_user_id", str(user["id"]), httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
        response.set_cookie("vx_pending_uid", str(user["id"]), httponly=True, max_age=600, samesite="lax", secure=True)
    return response


@app.post("/api/auth/resend-verification")
async def api_auth_resend_verification(request: Request):
    """인증 이메일 재발송"""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    user = _get_user_by_email(email)
    if not user or user.get("email_verified"):
        return JSONResponse({"ok": True})  # 보안상 항상 ok 반환
    token = str(_uuid.uuid4()).replace("-", "")
    with _users_db() as conn:
        conn.execute("UPDATE vantix_users SET email_verify_token=? WHERE id=?", (token, user["id"]))
    base_url = str(request.base_url).rstrip("/")
    _send_verification_email(email, token, base_url)
    return JSONResponse({"ok": True})


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    """로그아웃 — 세션 + 유저 쿠키 삭제"""
    token = request.cookies.get("vx_session")
    if token:
        _delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("vx_session")
    response.delete_cookie("vx_user_id")
    response.delete_cookie("vx_pending_uid")
    return response


@app.post("/api/auth/delete-account")
async def api_auth_delete_account(request: Request):
    """회원 탈퇴 (소프트 삭제). 비밀번호 재확인 후:
    - 팀: 오너면 멤버 분리·워크스페이스 해체, 멤버면 본인만 탈퇴
    - 구독 해지, Redmine 연결·선택 프로젝트 삭제, 개인정보 익명화
    - 결제 이력은 법령상 보관 (vantix_payment_history 유지)"""
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    body = await request.json()
    password = body.get("password") or ""

    user = _get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없습니다")
    if not _verify_password(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다")

    # 1) 팀 처리
    m = _get_membership(uid)
    with _users_db() as conn:
        if m and m["role"] == "owner":
            ws_id = m["workspace_id"]
            # 팀원 전원 분리 + 초대 삭제 + 워크스페이스 해체 (FK 순서: members·invitations → workspace)
            conn.execute("DELETE FROM vantix_workspace_members WHERE workspace_id=?", (ws_id,))
            conn.execute("DELETE FROM vantix_invitations WHERE workspace_id=?", (ws_id,))
            conn.execute("DELETE FROM vantix_workspaces WHERE id=?", (ws_id,))
        elif m:
            # 멤버 — 본인만 탈퇴
            conn.execute("DELETE FROM vantix_workspace_members WHERE user_id=?", (uid,))
        # 2) 구독 해지
        conn.execute("UPDATE vantix_billing_keys SET status='cancelled' WHERE user_id=? AND status='active'", (uid,))
        # 3) Redmine 연결·선택 프로젝트 삭제 (API 키 즉시 제거)
        conn.execute("DELETE FROM vantix_redmine_connections WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM vantix_user_projects WHERE user_id=?", (uid,))
        # 4) 개인정보 익명화 + 비활성화 (결제이력 보존을 위해 행은 유지)
        anon_email = f"deleted_{uid}_{int(time.time())}@deleted.local"
        conn.execute(
            "UPDATE vantix_users SET is_active=0, email=?, hashed_password='', email_verify_token=NULL, plan='free' WHERE id=?",
            (anon_email, uid)
        )

    # 5) 세션·쿠키 정리
    token = request.cookies.get("vx_session")
    if token:
        _delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("vx_session")
    response.delete_cookie("vx_user_id")
    response.delete_cookie("vx_pending_uid")
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    """현재 로그인 유저 정보"""
    token = request.cookies.get("vx_session")
    user_id_str = request.cookies.get("vx_user_id")
    if not user_id_str:
        uid = _get_session_user_id(token or "") if token else None
    else:
        uid = int(user_id_str) if user_id_str.isdigit() else None

    if not uid:
        raise HTTPException(status_code=401, detail="not_logged_in")

    user = _get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=401, detail="not_logged_in")

    conn_info = _get_redmine_connection(uid)

    # Redmine에서 현재 유저 이름 + admin 여부 가져오기
    display_name = ""
    is_admin = False
    s = _get_session(token or "")
    if s:
        try:
            rm_user = fetch("/users/current.json", redmine_url=s["url"], api_key=s["key"])
            u = rm_user.get("user", {})
            display_name = f"{u.get('lastname', '')} {u.get('firstname', '')}".strip() or u.get("login", "")
            is_admin = bool(u.get("admin", False))
        except Exception:
            pass

    return JSONResponse({
        "email": user["email"],
        "has_connection": conn_info is not None,
        "redmine_url": conn_info["redmine_url"] if conn_info else None,
        "display_name": display_name,
        "is_admin": is_admin,
    })


@app.post("/api/auth/connect-redmine")
async def api_auth_connect_redmine(request: Request):
    """로그인 후 Redmine 연결 저장 (회원가입 Step 2)"""
    # pending_uid 쿠키 또는 vx_user_id로 유저 확인
    pending_uid_str = request.cookies.get("vx_pending_uid")
    user_id_str = request.cookies.get("vx_user_id")
    uid_str = pending_uid_str or user_id_str

    if not uid_str or not uid_str.isdigit():
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    user_id = int(uid_str)
    user = _get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="유저를 찾을 수 없습니다")

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

    _save_redmine_connection(user_id, rm_url, rm_key)
    _claim_pending_invites(user_id, user["email"])

    # 세션 발급
    token = str(_uuid.uuid4())
    _save_session(token, rm_url, rm_key, time.time())
    _tag_session_user(token, user_id)

    response = JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", "")})
    response.set_cookie("vx_session", token, httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
    response.set_cookie("vx_user_id", str(user_id), httponly=True, max_age=SESSION_TTL, samesite="lax", secure=True)
    response.delete_cookie("vx_pending_uid")
    return response


# ---------- Phase 4: 플랜 / 선택 프로젝트 ----------
@app.get("/api/account/plan")
async def api_account_plan(request: Request):
    """현재 유저의 플랜·한도·선택 프로젝트. 익명/데모는 free."""
    uid = _current_user_id(request)
    plan = _get_user_plan(uid)
    info = plan_info(plan)
    role = _get_workspace_role(uid) if uid else "owner"
    return JSONResponse({
        "plan": plan,
        "label": info["label"],
        "project_limit": info["project_limit"],
        "member_limit": info.get("member_limit", 3),
        "ai": info["ai"],
        "report": info["report"],
        "csv": info.get("csv", False),
        "role": role,
        "can_edit": role in ("owner", "admin"),
        "selected_projects": _get_user_projects(uid) if uid else [],
        "is_authenticated": uid is not None,
        "projects_changed_at": _get_projects_changed_at(uid),
        "email": (_get_user_by_id(uid) or {}).get("email", "") if uid else "",
    })


# ==================== 문의하기 ====================

@app.post("/api/support")
async def api_support(request: Request):
    body = await request.json()
    name    = str(body.get("name", "")).strip()[:100]
    email   = str(body.get("email", "")).strip()[:200]
    message = str(body.get("message", "")).strip()[:2000]
    if not email or not message:
        raise HTTPException(status_code=400, detail="이메일과 내용을 입력해주세요")
    uid = _current_user_id(request)
    plan = _get_user_plan(uid) if uid else "비로그인"
    try:
        _resend.Emails.send({
            "from": f"Vantix <{RESEND_FROM}>",
            "to": [SUPPORT_EMAIL],
            "reply_to": [email],
            "subject": f"[Vantix 문의] {name or email}",
            "html": f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:520px;margin:0 auto;padding:40px 32px;background:#F5F4EF;border:1px solid rgba(23,24,26,.1);">
  <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#0F766E;margin-bottom:16px;">VANTIX — 문의</div>
  <table style="width:100%;font-size:13px;margin-bottom:24px;border-collapse:collapse;">
    <tr><td style="padding:6px 0;color:#6A6E73;width:80px;">이름</td><td style="padding:6px 0;">{name or "미입력"}</td></tr>
    <tr><td style="padding:6px 0;color:#6A6E73;">이메일</td><td style="padding:6px 0;"><a href="mailto:{email}" style="color:#0F766E;">{email}</a></td></tr>
    <tr><td style="padding:6px 0;color:#6A6E73;">플랜</td><td style="padding:6px 0;">{plan}</td></tr>
  </table>
  <div style="background:#fff;border:1px solid rgba(23,24,26,.1);padding:16px 20px;font-size:14px;line-height:1.8;color:#17181A;white-space:pre-wrap;">{message}</div>
</div>""",
        })
    except Exception as e:
        print(f"[support] 메일 발송 실패: {e}")
        raise HTTPException(status_code=500, detail="메일 발송에 실패했습니다")
    return JSONResponse({"ok": True})


# ==================== 결제 (포트원 V2) ====================

def _charge_billing_key(billing_key: str, payment_id: str, plan: str, user_id: int, email: str) -> dict:
    """포트원 V2 서버사이드 빌링키 결제"""
    amount = PLAN_PRICES.get(plan, 0)
    order_name = f"Vantix {plan.capitalize()} 월간 구독"
    with _httpx.Client(timeout=15) as client:
        resp = client.post(
            f"https://api.portone.io/payments/{payment_id}/billing-key",
            headers={"Authorization": f"PortOne {PORTONE_API_SECRET}"},
            json={
                "billingKey": billing_key,
                "orderName": order_name,
                "amount": {"total": amount},
                "currency": "KRW",
                "customer": {"id": f"user-{user_id}", "email": email},
            },
        )
    try:
        data = resp.json()
    except Exception:
        data = {}
    # 4xx/5xx인데 메시지가 없으면 raw 응답을 보존해 디버깅 가능하게
    if resp.status_code >= 400 and "message" not in data:
        data = {"type": f"HTTP_{resp.status_code}", "message": (resp.text or "")[:300]}
    if isinstance(data, dict):
        data["_http_status"] = resp.status_code
    print(f"[billing] charge resp status={resp.status_code} body={str(data)[:300]}")
    return data


@app.post("/api/billing/issue")
async def api_billing_issue(request: Request, uid: int = Depends(_require_login)):
    """프론트에서 발급받은 빌링키로 즉시 첫 결제 후 플랜 업그레이드"""
    # 멤버는 결제 불가 — 오너만 결제 (멤버는 오너 플랜 상속)
    if _get_workspace_role(uid) != "owner":
        raise HTTPException(status_code=403, detail="결제는 워크스페이스 오너만 가능합니다")
    body = await request.json()
    billing_key = body.get("billingKey", "").strip()
    plan = body.get("plan", "").strip().lower()
    if not billing_key:
        raise HTTPException(status_code=400, detail="billingKey가 없습니다")
    if plan not in ("pro", "business"):
        raise HTTPException(status_code=400, detail="플랜이 올바르지 않습니다")

    # 중복 결제 방지 — 현재 플랜보다 상위로의 업그레이드만 허용 (같거나 하위는 거부)
    current_plan = _get_user_plan(uid)
    try:
        cur_rank = PLAN_ORDER.index(current_plan)
    except ValueError:
        cur_rank = 0
    if PLAN_ORDER.index(plan) <= cur_rank:
        raise HTTPException(status_code=409, detail=f"이미 {plan_info(current_plan)['label']} 플랜을 이용 중입니다")

    # 유저 이메일
    with _users_db() as conn:
        row = conn.execute("SELECT email FROM vantix_users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없습니다")
    email = row["email"]

    # 결제 실행
    import uuid as _uuid_mod
    payment_id = f"vantix-{plan}-{uid}-{int(time.time())}"
    result = _charge_billing_key(billing_key, payment_id, plan, uid, email)
    # 포트원 V2 빌링키 결제: 성공 시 HTTP 200 + { "payment": { pgTxId, paidAt } }.
    # status 필드를 안 주므로 HTTP 200 + payment 존재로 성공 판정. 실패는 4xx + type/message.
    http_status = result.get("_http_status", 0)
    payment = result.get("payment") or {}
    paid = http_status == 200 and bool(payment) and payment.get("status", "PAID") == "PAID"

    if not paid:
        code = result.get("type", "UNKNOWN")
        msg = result.get("message") or "결제에 실패했습니다"
        raise HTTPException(status_code=402, detail=f"결제 실패: {msg} ({code})")

    # 빌링키 저장 + 플랜 업데이트
    import calendar
    now = time.time()
    expires_at = now + 31 * 86400  # 약 1개월
    with _users_db() as conn:
        conn.execute(
            "UPDATE vantix_billing_keys SET status='cancelled' WHERE user_id=? AND status='active'",
            (uid,)
        )
        cur = conn.execute(
            "INSERT INTO vantix_billing_keys (user_id, billing_key, plan, status, created_at, expires_at) VALUES (?,?,?,'active',?,?) RETURNING id",
            (uid, _encrypt_key(billing_key), plan, now, expires_at)
        )
        bk_id = cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO vantix_payment_history (user_id, payment_id, billing_key_id, plan, amount, status, paid_at) VALUES (?,?,?,?,?,?,?)",
            (uid, payment_id, bk_id, plan, PLAN_PRICES[plan], "paid", now)
        )
    _set_user_plan(uid, plan)
    return JSONResponse({"ok": True, "plan": plan, "payment_id": payment_id})


@app.post("/api/payment/card-complete")
async def api_payment_card_complete(request: Request, uid: int = Depends(_require_login)):
    """KG이니시스 카드 일반결제 검증 후 플랜 업그레이드"""
    body = await request.json()
    payment_id = body.get("paymentId", "").strip()
    plan = body.get("plan", "").strip()
    if not payment_id or plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="잘못된 요청")

    resp = requests.get(
        f"https://api.portone.io/payments/{payment_id}",
        headers={"Authorization": f"PortOne {PORTONE_API_SECRET}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="결제 검증 실패")
    payment = resp.json()
    if payment.get("status") != "PAID":
        raise HTTPException(status_code=400, detail="결제가 완료되지 않았습니다")

    now = time.time()
    expires_at = now + 31 * 86400
    with _users_db() as conn:
        conn.execute("UPDATE vantix_billing_keys SET status='cancelled' WHERE user_id=? AND status='active'", (uid,))
        row = conn.execute(
            "INSERT INTO vantix_billing_keys (user_id, billing_key, plan, status, created_at, expires_at) VALUES (?,?,?,'active',?,?) RETURNING id",
            (uid, "CARD_ONE_TIME", plan, now, expires_at)
        ).fetchone()
        bk_id = row["id"] if row else None
        conn.execute(
            "INSERT INTO vantix_payment_history (user_id, payment_id, billing_key_id, plan, amount, status, paid_at) VALUES (?,?,?,?,?,?,?)",
            (uid, payment_id, bk_id, plan, PLAN_PRICES[plan], "paid", now)
        )
    return JSONResponse({"ok": True, "plan": plan, "payment_id": payment_id})


@app.post("/api/billing/cancel")
async def api_billing_cancel(request: Request, uid: int = Depends(_require_login)):
    """구독 취소 — 빌링키 비활성화 후 플랜을 free로"""
    with _users_db() as conn:
        conn.execute(
            "UPDATE vantix_billing_keys SET status='cancelled' WHERE user_id=? AND status='active'",
            (uid,)
        )
    _set_user_plan(uid, "free")
    return JSONResponse({"ok": True, "plan": "free"})


@app.post("/api/billing/refund")
async def api_billing_refund(request: Request, uid: int = Depends(_require_login)):
    """최근 결제 환불 + 구독 취소 — PortOne V2 결제 취소 API 호출"""

    with _users_db() as conn:
        ph = conn.execute(
            "SELECT * FROM vantix_payment_history WHERE user_id=? AND status='paid' ORDER BY paid_at DESC LIMIT 1",
            (uid,)
        ).fetchone()

    if not ph:
        raise HTTPException(status_code=404, detail="환불할 결제 내역이 없습니다")

    payment_id = ph["payment_id"]

    with _httpx.Client(timeout=15) as client:
        resp = client.post(
            f"https://api.portone.io/payments/{payment_id}/cancel",
            headers={"Authorization": f"PortOne {PORTONE_API_SECRET}"},
            json={"reason": "고객 요청"},
        )

    if resp.status_code not in (200, 201):
        try:
            msg = resp.json().get("message", "환불 처리에 실패했습니다")
        except Exception:
            msg = "환불 처리에 실패했습니다"
        print(f"[refund] failed status={resp.status_code} body={resp.text[:300]}")
        raise HTTPException(status_code=502, detail=f"환불 실패: {msg}")

    with _users_db() as conn:
        conn.execute(
            "UPDATE vantix_payment_history SET status='refunded' WHERE payment_id=?",
            (payment_id,)
        )
        conn.execute(
            "UPDATE vantix_billing_keys SET status='cancelled' WHERE user_id=? AND status='active'",
            (uid,)
        )
    _set_user_plan(uid, "free")
    print(f"[refund] 환불 완료 uid={uid} payment_id={payment_id}")
    return JSONResponse({"ok": True, "plan": "free", "refunded_payment_id": payment_id})


@app.get("/api/billing/status")
async def api_billing_status(request: Request, uid: int = Depends(_require_login)):
    """현재 구독 상태"""
    with _users_db() as conn:
        bk = conn.execute(
            "SELECT * FROM vantix_billing_keys WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (uid,)
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM vantix_payment_history WHERE user_id=? ORDER BY paid_at DESC LIMIT 10",
            (uid,)
        ).fetchall()
    plan = _get_user_plan(uid)
    # 빌링키는 민감정보 — 프론트로 절대 노출하지 않음
    sub = None
    if bk:
        sub = dict(bk)
        sub.pop("billing_key", None)
    return JSONResponse({
        "plan": plan,
        "user_id": str(uid),
        "active_subscription": sub,
        "payment_history": [dict(h) for h in history],
    })


# ==================== 팀(워크스페이스) ====================
# /api/team*, /api/account/projects 라우트는 app/routers/team.py로 이전됨 (REFACTOR_PLAN Phase A)


# ============================================================

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "privacy.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "terms.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# connect/disconnect/update-connection 라우트는 app/routers/connect.py로 이전됨 (REFACTOR_PLAN Phase A)

@app.get("/api/issue/{issue_id}")
def api_get_issue(issue_id: int, request: Request):
    from concurrent.futures import ThreadPoolExecutor
    s = _require_session(request)

    # 이슈 본문만 항상 새로 fetch
    issue_data = fetch(f"/issues/{issue_id}.json", {"include": "journals,attachments"}, redmine_url=s["url"], api_key=s["key"])
    issue = issue_data.get("issue", {})
    pid = issue.get("project", {}).get("identifier") or str(issue.get("project", {}).get("id", ""))

    # 상태/멤버/버전은 프로젝트 단위 캐시 활용 (5분 TTL)
    meta_key = f"{s['url']}|{pid}"
    cached_meta = _get_modal_meta_cache(meta_key)

    if cached_meta:
        statuses  = cached_meta["statuses"]
        assignees = cached_meta["assignees"]
        ver_list  = cached_meta["versions"]
    else:
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
        _set_modal_meta_cache(meta_key, {"statuses": statuses, "assignees": assignees, "versions": ver_list})

    return {"issue": issue, "statuses": statuses, "assignees": assignees, "versions": ver_list}


@app.put("/api/issue/{issue_id}")
async def api_update_issue(issue_id: int, request: Request, s: dict = Depends(_require_session)):
    if not _can_edit(_current_user_id(request)):
        raise HTTPException(status_code=403, detail="이슈 수정 권한이 없습니다 (뷰어 권한)")
    body = await request.json()
    payload = {"issue": {}}
    if "status_id"        in body: payload["issue"]["status_id"]         = body["status_id"]
    if "assigned_to_id"   in body: payload["issue"]["assigned_to_id"]    = body["assigned_to_id"]
    if "fixed_version_id" in body: payload["issue"]["fixed_version_id"]  = body["fixed_version_id"]
    if "due_date"         in body: payload["issue"]["due_date"]           = body["due_date"]
    if "notes"            in body and body["notes"].strip():
        payload["issue"]["notes"] = body["notes"]
    if "description"      in body and body["description"] is not None:
        payload["issue"]["description"] = body["description"]

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


@app.get("/api/statuses")
async def api_statuses(s: dict = Depends(_require_session)):
    statuses = fetch("/issue_statuses.json", redmine_url=s["url"], api_key=s["key"]).get("issue_statuses", [])
    return {"statuses": statuses}


@app.get("/api/assignees")
async def api_assignees(project_id: str = "", s: dict = Depends(_require_session)):
    memberships = fetch(
        f"/projects/{project_id}/memberships.json",
        redmine_url=s["url"], api_key=s["key"]
    ).get("memberships", [])
    users = []
    seen = set()
    for m in memberships:
        u = m.get("user")
        if u and u.get("id") not in seen:
            seen.add(u["id"])
            users.append({"id": u["id"], "name": u["name"]})
    users.sort(key=lambda x: x["name"])
    return {"assignees": users}


@app.post("/api/bulk-update")
async def api_bulk_update(request: Request, s: dict = Depends(_require_session)):
    body = await request.json()
    issue_ids = body.get("issue_ids", [])
    fields = body.get("fields", {})
    if not issue_ids or not fields:
        return {"ok": False, "error": "issue_ids 또는 fields 없음"}

    ALLOWED = {"status_id", "assigned_to_id", "fixed_version_id"}
    payload_fields = {k: v for k, v in fields.items() if k in ALLOWED}
    if not payload_fields:
        return {"ok": False, "error": "변경 가능한 필드 없음"}

    def _update_one(issue_id):
        payload = json.dumps({"issue": payload_fields}).encode()
        for attempt in range(2):
            req = urllib.request.Request(
                s["url"].rstrip("/") + f"/issues/{issue_id}.json",
                data=payload,
                headers={"X-Redmine-API-Key": s["key"], "Content-Type": "application/json"},
                method="PUT"
            )
            try:
                with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT):
                    return True
            except Exception:
                if attempt == 1:
                    return False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    BATCH_SIZE = 10
    ok_count, fail_count = 0, 0
    batches = [issue_ids[i:i+BATCH_SIZE] for i in range(0, len(issue_ids), BATCH_SIZE)]
    for batch in batches:
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futures = {ex.submit(_update_one, iid): iid for iid in batch}
            for f in as_completed(futures):
                if f.result():
                    ok_count += 1
                else:
                    fail_count += 1

    return {"ok": True, "updated": ok_count, "failed": fail_count}


@app.post("/api/bulk-complete")
async def api_bulk_complete(request: Request, s: dict = Depends(_require_session)):
    body = await request.json()
    issue_ids = body.get("issue_ids", [])
    status_id = body.get("status_id")
    if not issue_ids:
        return {"ok": False, "error": "issue_ids 없음"}
    if not status_id:
        return {"ok": False, "error": "status_id 없음"}

    done_status = {"id": status_id}

    def _update_one(issue_id):
        payload = json.dumps({"issue": {"status_id": done_status["id"]}}).encode()
        for attempt in range(2):  # 실패 시 1회 재시도
            req = urllib.request.Request(
                s["url"].rstrip("/") + f"/issues/{issue_id}.json",
                data=payload,
                headers={"X-Redmine-API-Key": s["key"], "Content-Type": "application/json"},
                method="PUT"
            )
            try:
                with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT):
                    return True
            except Exception:
                if attempt == 1:
                    return False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import math
    BATCH_SIZE = 10
    ok_count, fail_count = 0, 0
    batches = [issue_ids[i:i+BATCH_SIZE] for i in range(0, len(issue_ids), BATCH_SIZE)]
    for batch in batches:
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futures = {ex.submit(_update_one, iid): iid for iid in batch}
            for f in as_completed(futures):
                if f.result():
                    ok_count += 1
                else:
                    fail_count += 1

    return {"ok": True, "updated": ok_count, "failed": fail_count}


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
    if not _can_edit(_current_user_id(request)):
        raise HTTPException(status_code=403, detail="이슈 수정 권한이 없습니다 (뷰어 권한)")
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


# /api/callouts* 라우트는 app/routers/callouts.py로 이전됨 (REFACTOR_PLAN Phase A)


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
                v["total"]   = total
                v["closed"]  = closed
                v["overdue"] = overdue
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
        except Exception as e:
            print(f"[report] AI 요약 생성 실패: {e}")
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

    report = build_report_data(dashboard, project_label=proj_name, insights=insights_data, project_id=project_id or "")
    html   = render_html_report(report, sections=section_list, memo=memo)
    return HTMLResponse(content=html)


@app.post("/api/report/share")
async def api_report_share(
    request: Request,
    s: dict = Depends(_require_session),

):
    body = await request.json()
    html = body.get("html", "")
    if not html:
        return JSONResponse({"ok": False, "error": "html이 없습니다"}, status_code=400)
    token = uuid.uuid4().hex
    now   = time.time()
    # 만료된 리포트 정리
    expired = [k for k, v in _report_store.items() if now - v["created_at"] > REPORT_TTL]
    for k in expired:
        del _report_store[k]
    _report_store[token] = {"html": html, "created_at": now}
    return {"ok": True, "token": token}


@app.get("/report/{token}")
async def view_shared_report(token: str):
    entry = _report_store.get(token)
    if not entry:
        return HTMLResponse("<h2>리포트를 찾을 수 없거나 만료되었습니다.</h2>", status_code=404)
    if time.time() - entry["created_at"] > REPORT_TTL:
        del _report_store[token]
        return HTMLResponse("<h2>리포트가 만료되었습니다 (24시간).</h2>", status_code=410)
    return HTMLResponse(content=entry["html"])


@app.get("/api/report/tsv")
async def api_report_tsv(
    request: Request,
    project_id:    str = "",
    updated_after: str = "2026-03-01",
    sections:      str = "",
    s: dict = Depends(_require_session),
):
    uid = _current_user_id(request)
    if not plan_info(_get_user_plan(uid)).get("csv", False):
        raise HTTPException(status_code=403, detail="CSV 내보내기는 Business 플랜에서 사용 가능합니다")
    from fastapi.responses import PlainTextResponse
    redmine_url = s.get("url")
    api_key     = s.get("key")
    dashboard = get_cache(project_id, updated_after, redmine_url)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=redmine_url, api_key=api_key)
    section_list = [sec.strip() for sec in sections.split(",") if sec.strip()] if sections else None
    report = build_report_data(dashboard, project_label=project_id or "전체 프로젝트", project_id=project_id or "")
    tsv    = render_tsv_report(report, sections=section_list)
    return PlainTextResponse(content=tsv, media_type="text/plain; charset=utf-8")


@app.post("/api/report/send")
async def api_report_send(request: Request, project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    uid = _current_user_id(request)
    if not plan_allows(_get_user_plan(uid), "report"):
        raise HTTPException(status_code=403, detail="이메일 리포트는 Pro 이상 플랜에서 사용 가능합니다")
    dashboard = get_cache(project_id, updated_after, s["url"])
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
    report  = build_report_data(dashboard, project_label=project_id or "전체 프로젝트", project_id=project_id or "")
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
    uid = _current_user_id(request)
    if not plan_allows(_get_user_plan(uid), "report"):
        raise HTTPException(status_code=403, detail="이메일 리포트는 Pro 이상 플랜에서 사용 가능합니다")
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
        proj_key = f"project_{project_id}" if project_id else "all"
        if _DATABASE_URL:
            proj_hist = _db_load_history(proj_key)
            if len(proj_hist) < 2:
                proj_hist = _db_load_history("all")
        else:
            with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
                hist = json.load(f)
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
        match = re.search(r'\[.*\]', raw, re.DOTALL)
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

# 로컬 `python main.py` 실행 시 이 파일은 모듈명 `__main__`으로 실행된다.
# app/routers/*.py는 전부 `import main as _m`으로 공용 인프라를 참조하는데,
# sys.modules에 "main"이 없으면 그 import가 이 파일을 처음부터 다시 실행시켜
# _register_routers()가 재귀적으로 다시 호출되며 순환 임포트로 죽는다
# (Railway는 `uvicorn main:app`으로 기동해 모듈명이 처음부터 "main"이라 이 문제가 없다).
# 이미 정의가 끝난 현재 모듈을 "main"으로도 등록해 재실행을 막는다.
sys.modules.setdefault("main", sys.modules[__name__])

# 라우터 등록 — 모듈 레벨에서 무조건 실행돼야 함.
# Railway는 `uvicorn main:app`으로 기동하므로 아래 `if __name__ == "__main__":` 블록을
# 절대 타지 않는다. 라우터 등록을 그 블록 안에 두면 로컬(python main.py)에서는 되고
# 배포에서는 라우트가 통째로 사라지는 사고가 나므로 반드시 여기(모듈 레벨)에 둔다.
_register_routers()


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
