"""
콜아웃(담당자 메모/알림) 라우트: /api/callouts*

REFACTOR_PLAN.md Phase A — main.py에서 기계적으로 이전 (로직 변경 없음).

주의: 원본 코드는 `global _callout_cache`로 main.py 모듈 전역 캐시를 무효화했다.
이 라우터는 main.py 밖에 있으므로 `global`이 아니라 `_m._callout_cache = ...`
형태로 main 모듈 객체의 속성을 직접 재할당해야 동일하게 동작한다.
(main.py의 _job_cleanup_callouts 스케줄러 잡은 그대로 main.py에 남아 같은 변수를 공유한다.)
"""
import time

from fastapi import APIRouter, HTTPException, Request

import main as _m

router = APIRouter()


@router.get("/api/callouts")
async def api_callouts_get(request: Request):
    token = request.cookies.get("vx_session")
    s = _m._get_session(token or "")
    if not s or not _m._DATABASE_URL:
        return {"items": []}
    # 메모리 캐시 히트 → 즉시 반환
    if _m._callout_cache is not None and time.time() - _m._callout_cache_ts < _m._CALLOUT_CACHE_TTL:
        return {"items": _m._callout_cache}

    def _query():
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, from_name, date, text, color, done, seen, expires_at FROM vantix_callouts "
                    "WHERE expires_at IS NULL OR expires_at > %s ORDER BY created DESC",
                    (time.time(),)
                )
                return cur.fetchall()
    try:
        rows = await _m._adb(_query)
        items = [{"id": r[0], "from": r[1] or "", "date": r[2] or "", "text": r[3], "color": r[4] or "#ff6b6b", "done": bool(r[5]), "seen": bool(r[6]), "expires_at": r[7]} for r in rows]
        _m._callout_cache = items
        _m._callout_cache_ts = time.time()
        return {"items": items}
    except Exception as e:
        print(f"[callouts/get] {e}")
        return {"items": []}


@router.post("/api/callouts")
async def api_callouts_post(request: Request):
    token = request.cookies.get("vx_session")
    s = _m._get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    body = await request.json()
    cid  = body.get("id", "")
    text = body.get("text", "").strip()
    if not cid or not text:
        raise HTTPException(status_code=400, detail="id and text required")
    if not _m._DATABASE_URL:
        return {"ok": True}
    # from_name은 서버에서 Redmine 유저 정보로 결정
    from_name = "나"
    try:
        rm_user = _m.fetch("/users/current.json", redmine_url=s["url"], api_key=s["key"])
        u = rm_user.get("user", {})
        from_name = f"{u.get('lastname', '')} {u.get('firstname', '')}".strip() or u.get("login", "") or "나"
    except Exception:
        pass
    now = time.time()
    expires_at = now + 21 * 24 * 3600  # 3주
    vals = (cid, from_name, body.get("date", ""), text, body.get("color", "#ff6b6b"), False, False, now, expires_at)

    def _insert():
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO vantix_callouts (id, from_name, date, text, color, done, seen, created, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    vals
                )
            conn.commit()
    try:
        await _m._adb(_insert)
        _m._callout_cache = None  # 캐시 무효화
    except Exception as e:
        print(f"[callouts/post] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}


@router.patch("/api/callouts/{cid}")
async def api_callouts_patch(cid: str, request: Request):
    token = request.cookies.get("vx_session")
    s = _m._get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    body = await request.json()
    if not _m._DATABASE_URL:
        return {"ok": True}
    has_done = "done" in body
    has_seen = "seen" in body
    has_extend = body.get("extend_expiry") is True
    done_val = bool(body.get("done"))
    seen_val = bool(body.get("seen"))

    # 기간 연장은 등록자 본인만 가능
    if has_extend:
        try:
            rm_user = _m.fetch("/users/current.json", redmine_url=s["url"], api_key=s["key"])
            u = rm_user.get("user", {})
            requester = f"{u.get('lastname', '')} {u.get('firstname', '')}".strip() or u.get("login", "")
        except Exception:
            requester = ""

        def _check_owner():
            with _m._db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT from_name FROM vantix_callouts WHERE id=%s", (cid,))
                    row = cur.fetchone()
                    return row[0] if row else None
        owner = await _m._adb(_check_owner)
        if owner != requester:
            raise HTTPException(status_code=403, detail="본인 callout만 연장할 수 있습니다")

    def _update():
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                if has_done:
                    cur.execute("UPDATE vantix_callouts SET done=%s WHERE id=%s", (done_val, cid))
                if has_seen:
                    cur.execute("UPDATE vantix_callouts SET seen=%s WHERE id=%s", (seen_val, cid))
                if has_extend:
                    new_expires = time.time() + 21 * 24 * 3600
                    cur.execute("UPDATE vantix_callouts SET expires_at=%s WHERE id=%s", (new_expires, cid))
            conn.commit()
    try:
        await _m._adb(_update)
        _m._callout_cache = None
    except Exception as e:
        print(f"[callouts/patch] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}


@router.delete("/api/callouts/{cid}")
async def api_callouts_delete(cid: str, request: Request):
    token = request.cookies.get("vx_session")
    s = _m._get_session(token or "")
    if not s:
        raise HTTPException(status_code=401)
    if not _m._DATABASE_URL:
        return {"ok": True}
    # 본인 또는 Redmine admin만 삭제 가능
    try:
        rm_user = _m.fetch("/users/current.json", redmine_url=s["url"], api_key=s["key"])
        u = rm_user.get("user", {})
        requester = f"{u.get('lastname', '')} {u.get('firstname', '')}".strip() or u.get("login", "")
        is_admin = bool(u.get("admin", False))
    except Exception:
        requester, is_admin = "", False

    def _check_and_delete():
        with _m._db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT from_name FROM vantix_callouts WHERE id=%s", (cid,))
                row = cur.fetchone()
                if not row:
                    return "not_found"
                if not is_admin and row[0] != requester:
                    return "forbidden"
                cur.execute("DELETE FROM vantix_callouts WHERE id=%s", (cid,))
            conn.commit()
            return "ok"
    try:
        result = await _m._adb(_check_and_delete)
        if result == "not_found":
            raise HTTPException(status_code=404)
        if result == "forbidden":
            raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
        _m._callout_cache = None
    except HTTPException:
        raise
    except Exception as e:
        print(f"[callouts/delete] {e}")
        raise HTTPException(status_code=500)
    return {"ok": True}
