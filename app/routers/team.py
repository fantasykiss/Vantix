"""
팀(워크스페이스) 관련 라우트: /api/team*, /api/account/projects

REFACTOR_PLAN.md Phase A — main.py에서 기계적으로 이전 (로직 변경 없음).
/api/account/projects는 엄밀히는 계정 설정이지만 원본에서 "팀(워크스페이스)" 블록에
함께 있던 코드라 지역성을 유지하기 위해 그대로 이 파일에 포함했다.
"""
import re
import time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.constants import plan_member_limit
import main as _m

router = APIRouter()


@router.get("/api/team")
async def api_team_get(request: Request):
    """현재 유저가 속한 팀 정보. 미소속이면 자기 자신이 오너인 빈 워크스페이스 기준."""
    uid = _m._current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    role = _m._get_workspace_role(uid)
    owner_id = _m._plan_owner_id(uid)
    plan = _m._get_user_plan(uid)
    limit = plan_member_limit(plan)

    m = _m._get_membership(uid)
    if m:
        ws_id = m["workspace_id"]
    elif role == "owner":
        # 아직 워크스페이스 미생성 — 멤버 조회용으로만 존재 여부 확인 (생성은 초대 시점)
        with _m._users_db() as conn:
            row = conn.execute("SELECT id FROM vantix_workspaces WHERE owner_user_id=?", (uid,)).fetchone()
        ws_id = row["id"] if row else None
    else:
        ws_id = None

    members, invites = [], []
    if ws_id:
        members = _m._get_workspace_members(ws_id)
        with _m._users_db() as conn:
            inv_rows = conn.execute(
                "SELECT id, email, role, created_at FROM vantix_invitations WHERE workspace_id=? AND status='pending' ORDER BY created_at",
                (ws_id,)
            ).fetchall()
            invites = [dict(r) for r in inv_rows]
    # 솔로 오너(워크스페이스 미생성)는 자기 자신만 표시
    if not members:
        me = _m._get_user_by_id(uid)
        members = [{"user_id": uid, "role": "owner", "email": me["email"] if me else "", "created_at": 0}]

    used = len(members) + len(invites)
    return JSONResponse({
        "role": role,
        "is_owner": role == "owner",
        "plan": plan,
        "member_limit": limit,
        "seats_used": used,
        "seats_left": (-1 if limit == -1 else max(0, limit - used)),
        "members": members,
        "invitations": invites,
        "my_user_id": uid,
    })


@router.post("/api/team/invite")
async def api_team_invite(request: Request):
    """팀원 초대 (오너만). body: {email, role}"""
    uid = _m._current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    if _m._get_workspace_role(uid) != "owner":
        raise HTTPException(status_code=403, detail="팀원 초대는 오너만 가능합니다")

    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "viewer").strip().lower()
    if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        raise HTTPException(status_code=400, detail="유효한 이메일을 입력하세요")
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="역할은 admin 또는 viewer만 가능합니다")

    me = _m._get_user_by_id(uid)
    if me and email == me["email"].lower():
        raise HTTPException(status_code=400, detail="본인은 초대할 수 없습니다")

    ws_id = _m._ensure_workspace(uid)

    # 좌석 한도 검증
    plan = _m._get_user_plan(uid)
    limit = plan_member_limit(plan)
    if limit != -1 and _m._count_workspace_seats(ws_id) >= limit:
        raise HTTPException(status_code=422, detail=f"현재 플랜의 팀원 한도({limit}명)에 도달했습니다")

    # 이미 멤버인지 확인
    target = _m._get_user_by_email(email)
    if target:
        existing_m = _m._get_membership(target["id"])
        if existing_m and existing_m["workspace_id"] == ws_id:
            raise HTTPException(status_code=409, detail="이미 팀에 속한 멤버입니다")
        if existing_m:
            raise HTTPException(status_code=409, detail="이미 다른 팀에 소속된 유저입니다")

    # 중복 초대 방지
    with _m._users_db() as conn:
        dup = conn.execute(
            "SELECT id FROM vantix_invitations WHERE workspace_id=? AND email=? AND status='pending'",
            (ws_id, email)
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="이미 초대장을 보낸 이메일입니다")

    # 인증된 기존 유저면 즉시 합류, 아니면 초대장 저장
    base_url = str(request.base_url).rstrip("/")
    is_existing = bool(target and target.get("email_verified"))
    if is_existing and not _m._get_membership(target["id"]):
        _m._add_member(ws_id, target["id"], role)
        _m._send_invite_email(email, me["email"], role, base_url, is_existing=True)
        return JSONResponse({"ok": True, "joined": True})

    token = str(_uuid.uuid4()).replace("-", "")
    with _m._users_db() as conn:
        conn.execute(
            "INSERT INTO vantix_invitations (workspace_id, email, role, token, status, created_at) VALUES (?,?,?,?,'pending',?)",
            (ws_id, email, role, token, time.time())
        )
    _m._send_invite_email(email, me["email"], role, base_url, is_existing=False)
    return JSONResponse({"ok": True, "joined": False})


@router.patch("/api/team/members/{member_id}")
async def api_team_member_role(member_id: int, request: Request):
    """멤버 역할 변경 (오너만). body: {role}"""
    uid = _m._current_user_id(request)
    if not uid or _m._get_workspace_role(uid) != "owner":
        raise HTTPException(status_code=403, detail="오너만 가능합니다")
    body = await request.json()
    role = (body.get("role") or "").strip().lower()
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="역할은 admin 또는 viewer만 가능합니다")
    if member_id == uid:
        raise HTTPException(status_code=400, detail="오너의 역할은 변경할 수 없습니다")
    ws_id = _m._ensure_workspace(uid)
    with _m._users_db() as conn:
        conn.execute(
            "UPDATE vantix_workspace_members SET role=? WHERE workspace_id=? AND user_id=? AND role!='owner'",
            (role, ws_id, member_id)
        )
    return JSONResponse({"ok": True})


@router.delete("/api/team/members/{member_id}")
async def api_team_member_remove(member_id: int, request: Request):
    """멤버 제거 (오너만)."""
    uid = _m._current_user_id(request)
    if not uid or _m._get_workspace_role(uid) != "owner":
        raise HTTPException(status_code=403, detail="오너만 가능합니다")
    if member_id == uid:
        raise HTTPException(status_code=400, detail="오너 본인은 제거할 수 없습니다")
    ws_id = _m._ensure_workspace(uid)
    with _m._users_db() as conn:
        conn.execute(
            "DELETE FROM vantix_workspace_members WHERE workspace_id=? AND user_id=? AND role!='owner'",
            (ws_id, member_id)
        )
    return JSONResponse({"ok": True})


@router.delete("/api/team/invitations/{invite_id}")
async def api_team_invite_revoke(invite_id: int, request: Request):
    """대기중 초대 취소 (오너만)."""
    uid = _m._current_user_id(request)
    if not uid or _m._get_workspace_role(uid) != "owner":
        raise HTTPException(status_code=403, detail="오너만 가능합니다")
    ws_id = _m._ensure_workspace(uid)
    with _m._users_db() as conn:
        conn.execute(
            "UPDATE vantix_invitations SET status='revoked' WHERE id=? AND workspace_id=?",
            (invite_id, ws_id)
        )
    return JSONResponse({"ok": True})


@router.post("/api/team/leave")
async def api_team_leave(request: Request):
    """팀원이 워크스페이스에서 나가기 (오너 제외)."""
    uid = _m._current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    m = _m._get_membership(uid)
    if not m or m["role"] == "owner":
        raise HTTPException(status_code=400, detail="오너는 팀을 나갈 수 없습니다")
    with _m._users_db() as conn:
        conn.execute("DELETE FROM vantix_workspace_members WHERE user_id=? AND role!='owner'", (uid,))
    return JSONResponse({"ok": True})


@router.post("/api/account/projects")
async def api_account_set_projects(request: Request):
    """모니터링할 프로젝트 선택(일괄 교체). 플랜 개수 제한 초과 시 422.
    body: {"projects": [{"project_id": "..", "project_name": ".."}, ...]}"""
    uid = _m._current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    body = await request.json()
    projects = body.get("projects") or []
    if not isinstance(projects, list):
        raise HTTPException(status_code=400, detail="projects는 배열이어야 합니다")
    # 최초 선택은 쿨다운 적용 안 함
    existing = _m._get_user_projects(uid)
    if existing:
        days_left = _m._projects_cooldown_days_left(uid)
        if days_left > 0:
            raise HTTPException(status_code=429, detail={"error": "cooldown", "days_left": days_left})
    try:
        saved = _m._set_user_projects(uid, projects)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with _m._users_db() as conn:
        conn.execute("UPDATE vantix_users SET projects_changed_at=? WHERE id=?", (time.time(), uid))
    return JSONResponse({"ok": True, "selected_projects": saved})
