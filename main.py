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
import uvicorn

# ==================== 서버 설정 ====================
from config import BASE_URL, API_KEY, ANTHROPIC_API_KEY, EMAIL_CFG, REPORT_DAY, REPORT_HOUR, REPORT_MINUTE
import uuid as _uuid

# ── 세션 스토어 (메모리, 베타용) ──────────────────────────────
_sessions: dict = {}          # token → {url, key, created}
SESSION_TTL = 86400 * 30      # 30일
_SESSION_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")

def _load_sessions():
    global _sessions
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, "r") as f:
                data = json.load(f)
            now = time.time()
            _sessions = {k: v for k, v in data.items() if now - v["created"] < SESSION_TTL}
    except Exception:
        _sessions = {}

def _save_sessions():
    try:
        with open(_SESSION_FILE, "w") as f:
            json.dump(_sessions, f)
    except Exception:
        pass

def _get_session(token: str) -> dict | None:
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() - s["created"] > SESSION_TTL:
        del _sessions[token]
        _save_sessions()
        return None
    return s

def _require_session(request: Request) -> dict:
    token = request.cookies.get("vx_session") or request.headers.get("X-VX-Session")
    s = _get_session(token or "")
    if not s:
        raise HTTPException(status_code=401, detail="session_expired")
    return s

_load_sessions()
from app.reporter import build_report_data, render_html_report, send_report_email
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
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ==================== 접속자 관리 ====================
_visitors = {}  # ip → last_seen
VISITOR_TTL = 300  # 5분

CACHE_TTL_SECONDS = 300       # 5분 캐시 유효시간
AUTO_REFRESH_INTERVAL = 1800  # 30분마다 백그라운드 자동갱신
_auto_refresh_params = {}


def get_active_visitors():
    now = datetime.now()
    return {ip: t for ip, t in _visitors.items() if (now - t).total_seconds() < VISITOR_TTL}


def cache_key(project_id, updated_after):
    return f"{project_id}|{updated_after}"


def get_cache(project_id, updated_after):
    key = cache_key(project_id, updated_after)
    entry = _cache.get(key)
    if not entry:
        return None
    age = (datetime.now() - entry["fetched_at"]).total_seconds()
    if age > CACHE_TTL_SECONDS:
        return None
    return entry["data"]


def set_cache(project_id, updated_after, data):
    key = cache_key(project_id, updated_after)
    _cache[key] = {"data": data, "fetched_at": datetime.now()}
    print(f"  캐시 저장: {key} ({datetime.now().strftime('%H:%M:%S')})")


def cache_age_str(project_id, updated_after):
    key = cache_key(project_id, updated_after)
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
        pid  = params["project_id"]
        uaft = params["updated_after"]
        print(f"  자동갱신: {key}")
        try:
            data = build_dashboard_data(pid, uaft)
            set_cache(pid, uaft, data)
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
    history.setdefault("all", [])
    if not history["all"] or history["all"][-1]["date"] != today:
        history["all"].append({"date": today, "score": avg_score, "level": avg_level})
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
            history[key].append({"date": today, "score": score, "level": level})
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
    hour=9, minute=0, timezone="Asia/Seoul"
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
    users_data = defaultdict(lambda: {"issues": [], "projects": set()})
    for iss in issues:
        if "assigned_to" not in iss:
            continue
        uname = iss["assigned_to"]["name"]
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
        "users_data":       {k: {"issues": v["issues"], "projects": list(v["projects"])} for k, v in users_data.items()},
        "imminent_count":   len(imminent_issues),
        "imminent_issues":  imminent_issues,
        "trend_7days":      trend_7days,
    }


def get_groups(project_id="", redmine_url=None, api_key=None):
    """
    프로젝트 멤버십 기반 그룹 목록 + 8주 오버듀 역산
    - 담당자명 접두사(기획_, 서버_ 등)로 그룹 매핑
    - 주차별 오버듀 수 역산해서 스파크라인 데이터 반환
    """
    if not project_id:
        return []
    try:
        from datetime import date, timedelta

        # 1. 멤버십에서 그룹 목록 추출
        members = fetch(f"/projects/{project_id}/memberships.json", redmine_url=redmine_url, api_key=api_key).get("memberships", [])
        group_map = {}  # group_name -> {id, name, user_count}
        # 1차: 그룹 엔트리로 그룹 목록 확보
        for m in members:
            grp = m.get("group")
            if grp:
                gname = grp["name"]
                if gname not in group_map:
                    group_map[gname] = {"id": grp["id"], "name": gname, "user_count": 0}
        # 2차: 유저 엔트리 접두사로 실제 인원 카운트
        for m in members:
            user = m.get("user")
            if user and "_" in user.get("name", ""):
                prefix = user["name"].split("_")[0].strip()
                if prefix in group_map:
                    group_map[prefix]["user_count"] += 1

        if not group_map:
            return []

        # 2. 이슈 전체 로드
        issues = get_issues(project_id, updated_after="2024-01-01", redmine_url=redmine_url, api_key=api_key)

        # 3. 담당자명 접두사로 그룹 매핑 (기획_홍길동 → 기획)
        def extract_group(assignee_name):
            if "_" in assignee_name:
                prefix = assignee_name.split("_")[0].strip()
                if prefix in group_map:
                    return prefix
            return None

        # 4. 8주 역산 계산
        today = date.today()
        days_since_thu = (today.weekday() - 3) % 7  # 목요일 기준
        this_week_start = today - timedelta(days=days_since_thu)
        WEEKS = 8

        results = []
        for gname, ginfo in group_map.items():
            spark = []
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
                    due = i.get("due_date", "")
                    if due and due < week_end:
                        overdue_count += 1
                spark.append(overdue_count)

            overdue_now = spark[-1] if spark else 0
            overdue_prev = spark[-2] if len(spark) >= 2 else 0
            wow = overdue_now - overdue_prev

            # 리스크 배지: Critical(오버듀 5+), High(2+), Stable
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
                "overdue_now":  overdue_now,
                "overdue_wow":  wow,
                "risk":         risk,
                "spark":        spark,
            })

        # 오버듀 내림차순 정렬
        results.sort(key=lambda x: -x["overdue_now"])
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
    return HTMLResponse(content=html)

@app.get("/api/projects")
async def api_projects(request: Request, s: dict = Depends(_require_session)):
    projects = get_projects()
    return [{"identifier": p["identifier"], "name": p["name"]} for p in projects]


@app.get("/api/data")
async def api_data(request: Request, project_id: str = "", updated_after: str = "2026-03-01", force: bool = False, s: dict = Depends(_require_session)):
    key = cache_key(project_id, updated_after)
    _auto_refresh_params[key] = {"project_id": project_id, "updated_after": updated_after}
    if not force:
        cached = get_cache(project_id, updated_after)
        if cached:
            age = cache_age_str(project_id, updated_after)
            print(f"  캐시 히트: {key} ({age})")
            return {**cached, "cached": True, "cache_age": age}
    print(f"  Redmine fetch: {key}")
    data = build_dashboard_data(project_id, updated_after, redmine_url=s["url"], api_key=s["key"])
    set_cache(project_id, updated_after, data)

    import threading as _threading
    def _bg_generate_signals(pid):
        try:
            cache_k = f"action-signals|{pid}"
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

    _threading.Thread(target=_bg_generate_signals, args=(project_id,), daemon=True).start()

    return {**data, "cached": False, "cache_age": None}


@app.get("/api/groups")
async def api_groups(request: Request, project_id: str = "", s: dict = Depends(_require_session)):
    if not project_id:
        return {"groups": []}
    groups = get_groups(project_id, redmine_url=s["url"], api_key=s["key"])
    return {"groups": groups}


@app.get("/api/versions")
async def api_versions(request: Request, project_id: str = "", force: bool = False, s: dict = Depends(_require_session)):
    vkey = f"versions|{project_id}"
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
    records = history.get(key, [])[-weeks:]

    result = []
    for idx, r in enumerate(records, 1):
        result.append({
            "week": f"W{idx}",
            "score": r["score"],
            "level": r["level"],
            "date": r["date"]
        })

    return {"history": result}



@app.get("/api/visitors")
async def visitors(request: Request):
    ip = request.client.host
    _visitors[ip] = datetime.now()
    active = get_active_visitors()
    return {"count": len(active)}


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
    _sessions[token] = {"url": rm_url, "key": rm_key, "created": time.time()}
    _save_sessions()
    response = JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", ""), "token": token})
    response.set_cookie("vx_session", token, httponly=False, max_age=SESSION_TTL, samesite="lax", secure=False)
    return response

@app.post("/api/disconnect")
async def api_disconnect(request: Request):
    """세션 종료 + 쿠키 만료"""
    token = request.cookies.get("vx_session")
    if token and token in _sessions:
        del _sessions[token]
        _save_sessions()
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
    if not rm_url or not rm_key:
        raise HTTPException(status_code=400, detail="url과 api_key 필수")
    try:
        test_url = rm_url + "/users/current.json"
        req = urllib.request.Request(test_url, headers={"X-Redmine-API-Key": rm_key})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            user_data = json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Redmine 연결 실패: {str(e)}")
    _sessions[token] = {"url": rm_url, "key": rm_key, "created": time.time()}
    _save_sessions()
    return JSONResponse({"ok": True, "user": user_data.get("user", {}).get("login", "")})

@app.get("/api/issue/{issue_id}")
def api_get_issue(issue_id: int, request: Request):
    from concurrent.futures import ThreadPoolExecutor
    s = _require_session(request)
    issue_data = fetch(f"/issues/{issue_id}.json", {"include": "journals"}, redmine_url=s["url"], api_key=s["key"])
    issue = issue_data.get("issue", {})
    pid = issue.get("project", {}).get("identifier") or str(issue.get("project", {}).get("id", ""))

    def get_statuses(): return fetch("/issue_statuses.json").get("issue_statuses", [])
    def get_members():
        items, offset = [], 0
        while True:
            data = fetch(f"/projects/{pid}/memberships.json", {"limit": 100, "offset": offset})
            batch = data.get("memberships", [])
            items += batch
            total = data.get("total_count", len(batch))
            if offset + 100 >= total:
                break
            offset += 100
        return items
    def get_versions_fn(): return fetch(f"/projects/{pid}/versions.json").get("versions", [])

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


@app.get("/api/report/preview")
async def api_report_preview(project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    from fastapi.responses import HTMLResponse
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
    report = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    html   = render_html_report(report)
    return HTMLResponse(content=html)


@app.post("/api/report/send")
async def api_report_send(project_id: str = "", updated_after: str = "2026-03-01", s: dict = Depends(_require_session)):
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
    report  = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    html    = render_html_report(report)
    subject = f"[Vantix] 주간 리포트 {report.period_label}"
    return send_report_email(html, subject, EMAIL_CFG)


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
    cache_k = f"action-signals|{project_id}"

    # force=True면 캐시 삭제
    if force and cache_k in _ai_cache:
        del _ai_cache[cache_k]

    # 캐시 있으면 바로 반환
    cached = _get_ai_cache(cache_k)
    if cached:
        parsed = json.loads(cached)
        if parsed:
            return {"signals": parsed}

    # _cache에서 project_id 매칭되는 항목 찾기 (updated_after 무관)
    dashboard = None
    for k, entry in list(_cache.items()):
        if k.split("|")[0] == project_id:
            age = (datetime.now() - entry["fetched_at"]).total_seconds()
            if age < CACHE_TTL_SECONDS:
                dashboard = entry["data"]
                break
    if not dashboard:
        # 직접 빌드 후 캐시 저장
        dashboard = build_dashboard_data(project_id, DEFAULT_UPDATED_AFTER)
        set_cache(project_id, DEFAULT_UPDATED_AFTER, dashboard)

    risks = dashboard.get("project_risk", [])[:5]
    if not risks:
        return {"signals": []}

    # 담당자 편중도 계산
    users_data = dashboard.get("users_data", {})
    assignee_counts = {u: len(v["issues"]) for u, v in users_data.items() if v["issues"]}
    total_issues = sum(assignee_counts.values()) or 1
    top_assignee = max(assignee_counts, key=assignee_counts.get) if assignee_counts else None
    top_count = assignee_counts.get(top_assignee, 0)
    top_pct = round(top_count / total_issues * 100)

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

    context_lines = (
        f"담당자 최대 집중도: {top_assignee if top_assignee else '없음'} "
        f"({top_count}건/{total_issues}건, {top_pct}%)\n"
        f"전주 대비 리스크 변화: {delta_str}점"
    )

    prompt = (
        "아래 프로젝트 리스크 데이터를 분석해서 JSON 배열만 반환해줘. 설명 없이 JSON만.\n"
        "반드시 3~5개 액션을 생성해야 해.\n"
        "각 항목 형식:\n"
        "{\"priority\": \"P1\"|\"P2\"|\"P3\", "
        "\"timing\": \"IMMEDIATE\"|\"THIS WEEK\"|\"NEXT SPRINT\", "
        "\"action_type\": \"REASSIGN\"|\"ESCALATE\"|\"SCHEDULE\"|\"MONITOR\", "
        "\"action\": \"구체적 처방 한 줄(한국어, 반드시 구체적 수치나 대상 명시, '~를 추천합니다' 또는 '~를 제안합니다' 형태)\", "
        "\"reason\": \"원인 한 줄(한국어, 데이터 근거 포함)\", "
        "\"who\": \"담당자 역할\"}\n"
        f"리스크 데이터:\n{risk_lines}\n"
        f"컨텍스트:\n{context_lines}\n"
        "우선순위 기준:\n"
        "P1/IMMEDIATE: overdue > 0 이거나 담당자 집중도 70% 이상이거나 전주 대비 +20점 이상\n"
        "P2/THIS WEEK: HIGH 리스크이거나 urgent > 0이거나 전주 대비 +10점 이상\n"
        "P3/NEXT SPRINT: MEDIUM 이하, 예방 조치\n"
        "액션 생성 순서:\n"
        "1. 담당자 업무 편중 해소 (집중도 70% 이상이면 반드시 REASSIGN 포함)\n"
        "2. 마감 초과 이슈 즉각 조치\n"
        "3. 마감 임박 이슈 대응\n"
        "4. 리스크 점수 급등 원인 분석\n"
        "5. 다음 스프린트 예방 조치\n"
        "JSON 배열만, 마크다운 코드블록 없이, 반드시 3개 이상"
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
    cache_k = f"report-summary|{project_id}|{updated_after}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return {"summary": cached}
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
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
    cache_k = f"delay-pred|{project_id}|{project_name}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return cached
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
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
    import time
    time.sleep(5)  # 서버 완전 기동 후 실행
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

threading.Thread(target=_preload_all_projects, daemon=True).start()


if __name__ == "__main__":
    print("=" * 50)
    print("  Vantix 시작")
    print("=" * 50)
    print(f"  브라우저에서 열기: http://localhost:8000")
    print(f"  종료: Ctrl+C")
    print(f"  자동갱신 주기: 30분")
    print("=" * 50)

    warmup_thread = threading.Thread(target=warmup_cache, daemon=True)
    warmup_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=8000)
