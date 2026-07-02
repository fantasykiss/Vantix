"""
Redmine 연결 관련 라우트.
/connect, /api/connect*, /api/disconnect, /api/update-connection

REFACTOR_PLAN.md Phase A — main.py에서 기계적으로 이전 (로직 변경 없음).
세션 저장/조회, rate limit, SSL 컨텍스트 등 공용 인프라는 main.py에 계속 정의돼 있고
여기서는 main.py가 app.routers.connect를 import하는 시점(main.py 하단)에
이미 정의가 끝난 상태이므로 순환 임포트 없이 안전하게 참조한다.
"""
import json
import time
import urllib.parse
import urllib.request
import uuid as _uuid
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import PORTONE_STORE_ID, PORTONE_CHANNEL_KEY, PORTONE_CHANNEL_KEY_INICIS, PORTONE_CHANNEL_KEY_TOSSPAY
import main as _m  # 세션 스토어 / rate limit / 상수 등 공용 인프라 참조용

router = APIRouter()


@router.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    token = request.cookies.get("vx_session")
    if _m._get_session(token or "") and _m._get_session_user_id(token or ""):
        return RedirectResponse(url="/")
    template_path = os.path.join(os.path.dirname(_m.__file__), "templates", "connect.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    # DEMO_URL/DEMO_KEY 환경변수가 있으면 "Try Vantix" 버튼 활성화
    demo_flag = "true" if (_m.DEMO_URL and _m.DEMO_KEY) else "false"
    html = html.replace("__DEMO_AVAILABLE__", demo_flag)
    html = html.replace("__PORTONE_STORE_ID__", PORTONE_STORE_ID)
    html = html.replace("__PORTONE_CHANNEL_KEY__", PORTONE_CHANNEL_KEY)
    html = html.replace("__PORTONE_CHANNEL_KEY_INICIS__", PORTONE_CHANNEL_KEY_INICIS)
    html = html.replace("__PORTONE_CHANNEL_KEY_TOSSPAY__", PORTONE_CHANNEL_KEY_TOSSPAY)
    return HTMLResponse(content=html)


@router.post("/api/connect/demo")
async def api_connect_demo(request: Request):
    """DEMO_URL/DEMO_KEY 환경변수로 자동 세션 발급 — Try Vantix 버튼용"""
    if not _m.DEMO_URL or not _m.DEMO_KEY:
        raise HTTPException(status_code=404, detail="데모 미설정")
    token = str(_uuid.uuid4())
    _m._save_session(token, _m.DEMO_URL, _m.DEMO_KEY, time.time())
    _m._demo_tokens.add(token)
    response = JSONResponse({"ok": True})
    response.set_cookie("vx_session", token, httponly=True, max_age=_m.DEMO_SESSION_TTL, samesite="lax", secure=True)
    return response


def _validate_redmine_url(url: str) -> str | None:
    """URL scheme(http/https만), 사설 IP 대역 차단. 문제 있으면 에러 문자열 반환."""
    import ipaddress as _ipaddress
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return "유효하지 않은 URL입니다"
    if parsed.scheme not in ("http", "https"):
        return "http 또는 https URL만 허용됩니다"
    hostname = parsed.hostname or ""
    if not hostname:
        return "유효하지 않은 URL입니다"
    try:
        ip = _ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return "허용되지 않는 주소입니다"
    except ValueError:
        pass
    return None


@router.post("/api/connect")
async def api_connect(request: Request):
    """베타 온보딩: Redmine URL + API Key 검증 후 세션 발급"""
    client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    _m._check_rate_limit(client_ip)
    body = await request.json()
    rm_url = (body.get("url") or "").strip().rstrip("/")
    rm_key = (body.get("api_key") or "").strip()
    if not rm_url or not rm_key:
        raise HTTPException(status_code=400, detail="url과 api_key 필수")

    url_err = _validate_redmine_url(rm_url)
    if url_err:
        raise HTTPException(status_code=400, detail=url_err)

    # Redmine 연결 검증
    try:
        test_url = rm_url + "/users/current.json"
        req = urllib.request.Request(test_url, headers={"X-Redmine-API-Key": rm_key})
        with urllib.request.urlopen(req, timeout=30, context=_m.SSL_CONTEXT) as resp:
            user_data = json.loads(resp.read().decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Redmine 연결에 실패했습니다. URL과 API 키를 확인해주세요.")

    token = str(_uuid.uuid4())
    _m._save_session(token, rm_url, rm_key, time.time())
    response = JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", ""), "token": token})
    response.set_cookie("vx_session", token, httponly=True, max_age=_m.SESSION_TTL, samesite="lax", secure=True)
    return response


@router.post("/api/disconnect")
async def api_disconnect(request: Request):
    """세션 종료 + 쿠키 만료"""
    token = request.cookies.get("vx_session")
    if token:
        _m._delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("vx_session")
    return response


@router.post("/api/update-connection")
async def api_update_connection(request: Request):
    """설정에서 연결 정보 변경 — 기존 세션 토큰 재사용"""
    token = request.cookies.get("vx_session")
    if not token or not _m._get_session(token):
        raise HTTPException(status_code=401, detail="session_expired")
    body = await request.json()
    rm_url = (body.get("url") or "").strip().rstrip("/")
    rm_key = (body.get("api_key") or "").strip()
    existing = _m._get_session(token)
    if not rm_key and existing:
        rm_key = existing.get("key", "")
    if not rm_url or not rm_key:
        raise HTTPException(status_code=400, detail="url과 api_key 필수")
    url_err = _validate_redmine_url(rm_url)
    if url_err:
        raise HTTPException(status_code=400, detail=url_err)
    try:
        test_url = rm_url + "/users/current.json"
        req = urllib.request.Request(test_url, headers={"X-Redmine-API-Key": rm_key})
        with urllib.request.urlopen(req, timeout=30, context=_m.SSL_CONTEXT) as resp:
            user_data = json.loads(resp.read().decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Redmine 연결에 실패했습니다. URL과 API 키를 확인해주세요.")
    _m._save_session(token, rm_url, rm_key, time.time())
    return JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", "")})
