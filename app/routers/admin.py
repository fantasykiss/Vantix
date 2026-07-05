"""
분석/피드백/관리자 라우트: /api/track, /api/feedback, /api/admin/*, /admin

REFACTOR_PLAN.md Phase A — main.py에서 기계적으로 이전 (로직 변경 없음).
원본 파일의 "ANALYTICS & FEEDBACK" 블록이 관리자 대시보드 라우트와 물리적으로
붙어 있고 _build_filters/_require_admin을 공유하므로 하나의 라우터로 묶었다.
"""
import hmac
import os
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import ADMIN_PASSWORD, PLAN_PRICES
import main as _m

router = APIRouter()

ADMIN_SESSION_TTL = 86400 * 7  # 7일 — 로그인 쿠키 max_age와 동일
_admin_sessions: dict[str, float] = {}  # token → 발급 시각 (쿠키엔 비밀번호 대신 이 토큰만 실림)


@router.post("/api/track")
async def api_track(request: Request):
    if not _m._DATABASE_URL:
        return {"ok": True}
    try:
        body = await request.json()
        session_id = body.get("session_id", "")
        event_type = body.get("event_type", "")
        page       = body.get("page", "")
        element    = body.get("element", "")
        duration   = body.get("duration")
        ts         = time.time()
        ip         = request.headers.get("X-Forwarded-For", request.client.host or "").split(",")[0].strip()
        user_agent = request.headers.get("User-Agent", "")
        host       = request.headers.get("host", "").split(":")[0]
        env        = "로컬" if host in ("localhost", "127.0.0.1") else "프로덕션"
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analytics_events (session_id, event_type, page, element, duration, ts, ip, user_agent, env) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (session_id, event_type, page, element, duration, ts, ip, user_agent, env)
                )
            conn.commit()
    except Exception as e:
        print(f"[track] 저장 실패: {e}")
    return {"ok": True}


@router.post("/api/feedback")
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
        if _m._DATABASE_URL:
            with _m._db_conn() as conn:
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


def _build_filters(period: str, env: str) -> tuple[str, tuple]:
    parts: list[str] = []
    params: list = []
    if period == "today":
        parts.append("ts > EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date)")
    elif period == "30d":
        parts.append("ts > EXTRACT(EPOCH FROM NOW()) - 2592000")
    elif period != "all":  # default 7d
        parts.append("ts > EXTRACT(EPOCH FROM NOW()) - 604800")
    if env:
        parts.append("env = %s")
        params.append(env)
    clause = (" AND " + " AND ".join(parts)) if parts else ""
    return clause, tuple(params)


def _require_admin(request: Request):
    token = request.cookies.get("vx_admin")
    created = _admin_sessions.get(token or "")
    if not token or created is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if time.time() - created > ADMIN_SESSION_TTL:
        _admin_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@router.post("/api/admin/login")
async def api_admin_login(request: Request):
    client_ip = request.headers.get("X-Forwarded-For", request.client.host or "").split(",")[0].strip()
    _m._check_rate_limit(f"admin_login:{client_ip}")

    body = await request.json()
    pw = body.get("password", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="비밀번호가 틀렸습니다.")
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("vx_admin", token, httponly=True, samesite="lax", max_age=ADMIN_SESSION_TTL, secure=True)
    return resp


@router.post("/api/admin/logout")
async def api_admin_logout(request: Request):
    token = request.cookies.get("vx_admin")
    if token:
        _admin_sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("vx_admin")
    return resp


@router.get("/api/admin/stats")
async def api_admin_stats(request: Request, env: str = "", period: str = "7d", _=Depends(_require_admin)):
    if not _m._DATABASE_URL:
        return {"pages": [], "clicks": [], "exits": [], "sessions": 0}
    tf, tp = _build_filters(period, env)
    ef, ep = (" AND env = %s", (env,)) if env else ("", ())
    try:
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(DISTINCT session_id) FROM analytics_events WHERE 1=1 {tf}", tp)
                total_sessions = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT page, COUNT(*) as views, COALESCE(AVG(duration),0)::int as avg_sec
                    FROM analytics_events
                    WHERE event_type='dwell' AND page IS NOT NULL AND page != '' {tf}
                    GROUP BY page ORDER BY avg_sec DESC
                """, tp)
                pages = [{"page": r[0], "views": r[1], "avg_sec": r[2]} for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT element, COUNT(*) as cnt
                    FROM analytics_events
                    WHERE event_type='click' AND element IS NOT NULL AND element != '' {tf}
                    GROUP BY element ORDER BY cnt DESC LIMIT 10
                """, tp)
                clicks = [{"element": r[0], "cnt": r[1]} for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT element, COUNT(*) as cnt
                    FROM analytics_events
                    WHERE event_type='click' AND element IS NOT NULL AND element != '' {tf}
                    GROUP BY element ORDER BY cnt ASC LIMIT 5
                """, tp)
                bottom_clicks = [{"element": r[0], "cnt": r[1]} for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT page, COUNT(*) as cnt
                    FROM analytics_events
                    WHERE event_type='exit' AND page IS NOT NULL AND page != '' {tf}
                    GROUP BY page ORDER BY cnt DESC
                """, tp)
                exits = [{"page": r[0], "cnt": r[1]} for r in cur.fetchall()]

                if period == "today":
                    cur.execute(f"""
                        SELECT LPAD(TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul','HH24'),2,'0') || 'h',
                               COUNT(DISTINCT session_id)
                        FROM analytics_events
                        WHERE ts > EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date) {ef}
                        GROUP BY 1 ORDER BY 1
                    """, ep)
                else:
                    cur.execute(f"""
                        SELECT TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul','MM/DD'),
                               COUNT(DISTINCT session_id)
                        FROM analytics_events WHERE 1=1 {tf}
                        GROUP BY 1 ORDER BY 1
                    """, tp)
                daily = [{"day": r[0], "visitors": r[1]} for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT COUNT(DISTINCT session_id) FROM analytics_events
                    WHERE ts > EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date) {ef}
                """, ep)
                today_visitors = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT COALESCE(AVG(cnt),0) FROM (
                        SELECT COUNT(DISTINCT session_id) as cnt
                        FROM analytics_events
                        WHERE ts > EXTRACT(EPOCH FROM NOW()) - 604800
                          AND ts < EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date) {ef}
                        GROUP BY TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul','YYYY-MM-DD')
                    ) sub
                """, ep)
                weekly_avg = float(cur.fetchone()[0] or 0)
                anomaly = None
                if weekly_avg > 0:
                    ratio = today_visitors / weekly_avg
                    if ratio >= 2.0:   anomaly = "spike"
                    elif ratio <= 0.3 and today_visitors < weekly_avg - 1: anomaly = "drop"

                cur.execute(f"SELECT COUNT(DISTINCT ip) FROM analytics_events WHERE ip IS NOT NULL AND ip != '' {tf}", tp)
                unique_ips = cur.fetchone()[0]

                cur.execute("""
                    SELECT COALESCE(env,'미분류'), COUNT(DISTINCT session_id)
                    FROM analytics_events WHERE env IS NOT NULL
                    GROUP BY env ORDER BY COUNT(DISTINCT session_id) DESC
                """)
                env_stats = [{"env": r[0], "sessions": r[1]} for r in cur.fetchall()]

                cur.execute(f"""
                    SELECT user_agent, session_id FROM analytics_events
                    WHERE user_agent IS NOT NULL AND user_agent != '' {tf}
                    GROUP BY user_agent, session_id
                """, tp)
                ua_rows = cur.fetchall()
                device_map: dict = {}; browser_map: dict = {}; os_map: dict = {}; seen: dict = {}
                for ua_str, sid in ua_rows:
                    if sid in seen: continue
                    seen[sid] = True
                    p = _m._parse_ua(ua_str)
                    device_map[p["device"]]   = device_map.get(p["device"], 0) + 1
                    browser_map[p["browser"]] = browser_map.get(p["browser"], 0) + 1
                    os_map[p["os"]]           = os_map.get(p["os"], 0) + 1
                device_stats  = sorted([{"name": k, "cnt": v} for k,v in device_map.items()],  key=lambda x: -x["cnt"])
                browser_stats = sorted([{"name": k, "cnt": v} for k,v in browser_map.items()], key=lambda x: -x["cnt"])
                os_stats      = sorted([{"name": k, "cnt": v} for k,v in os_map.items()],      key=lambda x: -x["cnt"])

                cur.execute(f"SELECT COUNT(DISTINCT session_id) FROM analytics_events WHERE event_type='click' {tf}", tp)
                funnel_clicked = cur.fetchone()[0]
                cur.execute(f"""
                    SELECT COUNT(DISTINCT session_id) FROM analytics_events
                    WHERE event_type='dwell' AND page IS NOT NULL AND page NOT ILIKE '%%connect%%' {tf}
                """, tp)
                funnel_dashboard = cur.fetchone()[0]
                funnel = [
                    {"step": "방문", "cnt": total_sessions},
                    {"step": "인터랙션", "cnt": funnel_clicked},
                    {"step": "대시보드 진입", "cnt": funnel_dashboard},
                ]

                # 신규 방문자 — 이 기간 내 처음 등장한 IP
                if period == "today":
                    ps = "EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date)"
                elif period == "30d":
                    ps = "EXTRACT(EPOCH FROM NOW()) - 2592000"
                elif period == "all":
                    ps = "0"
                else:
                    ps = "EXTRACT(EPOCH FROM NOW()) - 604800"
                cur.execute(f"""
                    SELECT COUNT(*) FROM (
                        SELECT ip FROM analytics_events
                        WHERE ip IS NOT NULL AND ip != '' {ef}
                        GROUP BY ip HAVING MIN(ts) >= {ps}
                    ) sub
                """, ep)
                new_visitors = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT ip, COUNT(DISTINCT session_id) as sessions, MIN(ts) as first_seen
                    FROM analytics_events
                    WHERE ip IS NOT NULL AND ip != ''
                    GROUP BY ip ORDER BY sessions DESC LIMIT 8
                """)
                top_ips = [{"ip": r[0], "sessions": r[1], "first_seen": r[2]} for r in cur.fetchall()]

        return {
            "pages": pages, "clicks": clicks, "exits": exits, "bottom_clicks": bottom_clicks,
            "top_ips": top_ips, "owner_ips": _m.OWNER_IPS,
            "sessions": total_sessions, "daily": daily, "today_visitors": today_visitors,
            "unique_ips": unique_ips, "env_stats": env_stats,
            "device_stats": device_stats, "browser_stats": browser_stats, "os_stats": os_stats,
            "funnel": funnel, "anomaly": anomaly, "weekly_avg": round(weekly_avg, 1),
            "new_visitors": new_visitors,
        }
    except Exception as e:
        print(f"[admin/stats] 오류: {e}")
        return {"pages": [], "clicks": [], "exits": [], "sessions": 0, "daily": []}


@router.get("/api/admin/feedback")
async def api_admin_feedback(request: Request, _=Depends(_require_admin)):
    if not _m._DATABASE_URL:
        return {"items": []}
    try:
        with _m._db_conn() as conn:
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


@router.get("/api/admin/connections")
async def api_admin_connections(_=Depends(_require_admin)):
    now = time.time()
    url_map: dict = {}
    for token, s in list(_m._session_cache.items()):
        if now - s.get("created", 0) < _m.SESSION_TTL:
            url = s.get("url", "")
            if url:
                if url not in url_map:
                    url_map[url] = {"url": url, "sessions": 0, "last_seen": s.get("created", 0)}
                url_map[url]["sessions"] += 1
                url_map[url]["last_seen"] = max(url_map[url]["last_seen"], s.get("created", 0))
    connections = sorted(url_map.values(), key=lambda x: -x["last_seen"])
    for c in connections:
        age = int((now - c["last_seen"]) / 60)
        c["age_str"] = f"{age}분 전" if age < 60 else f"{age // 60}시간 전"
    return {"connections": connections, "total": len(connections)}


@router.get("/api/admin/events")
async def api_admin_events(request: Request, env: str = "", period: str = "7d", limit: int = 200, _=Depends(_require_admin)):
    if not _m._DATABASE_URL:
        return {"items": []}
    tf, tp = _build_filters(period, env)
    now = time.time()
    period_start = (
        now - 86400 * 365 * 10 if period == "all" else
        now - 2592000           if period == "30d" else
        now - 604800            if period != "today" else
        None  # today는 DB에서 계산
    )
    try:
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT session_id, event_type, page, element, ip, user_agent, env,
                           TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul','MM/DD HH24:MI:SS') as ts_str
                    FROM analytics_events WHERE 1=1 {tf}
                    ORDER BY ts DESC LIMIT %s
                """, tp + (limit,))
                rows = cur.fetchall()

                # IP별 최초 등장 시각
                result_ips = list({r[4] for r in rows if r[4]})
                ip_first: dict = {}
                if result_ips:
                    cur.execute("SELECT ip, MIN(ts) FROM analytics_events WHERE ip = ANY(%s) GROUP BY ip", (result_ips,))
                    ip_first = {r[0]: r[1] for r in cur.fetchall()}

                if period == "today":
                    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Asia/Seoul')::date)")
                    period_start = cur.fetchone()[0]

        items = []
        for r in rows:
            ua_p = _m._parse_ua(r[5] or "")
            ip = r[4] or ""
            first_ts = ip_first.get(ip, 0)
            is_new = bool(ip and period_start is not None and first_ts >= period_start)
            items.append({
                "session": (r[0] or "")[:8],
                "event": r[1] or "",
                "page": r[2] or "",
                "element": (r[3] or "")[:40],
                "ip": ip,
                "device": ua_p["device"],
                "browser": ua_p["browser"],
                "env": r[6] or "",
                "ts": r[7] or "",
                "is_new": is_new,
                "is_owner": ip in _m.OWNER_IPS,
            })
        return {"items": items}
    except Exception as e:
        print(f"[admin/events] 오류: {e}")
        return {"items": []}


@router.get("/api/admin/saas")
async def api_admin_saas(_=Depends(_require_admin)):
    """회원·결제·팀 운영 대시보드. 민감정보(비번해시·빌링키·API키)는 제외."""
    if not _m._DATABASE_URL:
        return {"summary": {}, "members": [], "payments": [], "teams": []}
    with _m._users_db() as conn:
        # 회원 + Redmine 연결여부
        members = [dict(r) for r in conn.execute("""
            SELECT u.id, u.email, u.plan, u.created_at, u.email_verified,
                   (rc.id IS NOT NULL) AS has_connection
            FROM vantix_users u
            LEFT JOIN vantix_redmine_connections rc ON rc.user_id = u.id
            WHERE u.is_active = 1
            ORDER BY u.created_at DESC
        """).fetchall()]
        # 결제 이력
        payments = [dict(r) for r in conn.execute("""
            SELECT ph.payment_id, ph.plan, ph.amount, ph.status, ph.paid_at, u.email
            FROM vantix_payment_history ph
            JOIN vantix_users u ON u.id = ph.user_id
            ORDER BY ph.paid_at DESC LIMIT 200
        """).fetchall()]
        # 활성 구독 (플랜별)
        active_subs = {r["plan"]: r["c"] for r in conn.execute(
            "SELECT plan, COUNT(*) c FROM vantix_billing_keys WHERE status='active' GROUP BY plan"
        ).fetchall()}
        # 플랜 분포
        plan_dist = {r["plan"]: r["c"] for r in conn.execute(
            "SELECT plan, COUNT(*) c FROM vantix_users WHERE is_active=1 GROUP BY plan"
        ).fetchall()}
        # 팀 현황
        teams = [dict(r) for r in conn.execute("""
            SELECT w.id AS workspace_id, ou.email AS owner_email, ou.plan AS owner_plan,
                   (SELECT COUNT(*) FROM vantix_workspace_members wm WHERE wm.workspace_id = w.id) AS member_count,
                   (SELECT COUNT(*) FROM vantix_invitations iv WHERE iv.workspace_id = w.id AND iv.status='pending') AS pending_count
            FROM vantix_workspaces w
            JOIN vantix_users ou ON ou.id = w.owner_user_id
            ORDER BY member_count DESC
        """).fetchall()]

    # MRR = 활성 구독 × 플랜 가격
    mrr = sum(active_subs.get(p, 0) * price for p, price in PLAN_PRICES.items())
    summary = {
        "total_members": len(members),
        "plan_dist": plan_dist,
        "active_subscriptions": sum(active_subs.values()),
        "mrr": mrr,
    }
    return {"summary": summary, "members": members, "payments": payments, "teams": teams}


@router.get("/api/admin/backups")
async def api_admin_backups(_=Depends(_require_admin)):
    """최근 DB 백업 실행 이력 (성공/실패, 크기, 에러)"""
    if not _m._DATABASE_URL:
        return {"items": []}
    try:
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'Asia/Seoul','YYYY-MM-DD HH24:MI') as ts_str,
                           status, size_bytes, error
                    FROM vantix_backup_log ORDER BY ts DESC LIMIT 30
                """)
                rows = cur.fetchall()
        items = [{"ts_str": r[0], "status": r[1], "size_bytes": r[2], "error": r[3]} for r in rows]
        return {"items": items}
    except Exception as e:
        print(f"[admin/backups] 오류: {e}")
        return {"items": []}


@router.get("/api/admin/backup/download")
async def api_admin_backup_download(_=Depends(_require_admin)):
    """지금 즉시 DB를 덤프해 gzip 압축된 .sql.gz로 다운로드 (관리자 인증 필요, 암호화 없이 그대로 받아서 바로 열람 가능)"""
    if not _m._DATABASE_URL:
        raise HTTPException(status_code=400, detail="DB가 설정되어 있지 않습니다")
    try:
        compressed = _m._dump_db_gzip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"백업 생성 실패: {e}")
    today = datetime.now().strftime("%Y%m%d")
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="vantix_backup_{today}.sql.gz"'},
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    template_path = os.path.join(os.path.dirname(_m.__file__), "templates", "admin.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
