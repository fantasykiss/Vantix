#!/usr/bin/env python3
"""
Redmine 실시간 웹 대시보드
실행: python main.py
접속: http://localhost:8000
"""

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ==================== 서버 설정 ====================
from config import BASE_URL, API_KEY, ANTHROPIC_API_KEY, EMAIL_CFG, REPORT_DAY, REPORT_HOUR, REPORT_MINUTE
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
AI_CACHE_TTL = 300  # 5분

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

from config import DEFAULT_PROJECT_ID, DEFAULT_UPDATED_AFTER

_scheduler = BackgroundScheduler(timezone="Asia/Seoul")
_scheduler.add_job(_job_refresh_cache, "interval", minutes=30, id="cache_refresh")
_scheduler.add_job(_job_weekly_report, CronTrigger(
    day_of_week=REPORT_DAY, hour=REPORT_HOUR, minute=REPORT_MINUTE
), id="weekly_report")
_scheduler.start()
print(f"  스케줄러 시작!")


# ==================== API 유틸 ====================

def fetch(path, params=None, retries=2):
    url = BASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Redmine-API-Key": API_KEY})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < retries - 1:
                print(f"  재시도 ({attempt+1}/{retries-1}): offset {params.get('offset','?') if params else '?'}")
                time.sleep(1)
            else:
                print(f"  요청 실패: {url}\n     {e}")
    return {}


def fetch_all(path, key, base_params=None):
    """첫 페이지로 total_count 파악 후 나머지 페이지를 병렬로 fetch"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    limit = 100

    # 1) 첫 페이지 fetch → total_count 확인
    first_params = {**(base_params or {}), "limit": limit, "offset": 0}
    first_data = fetch(path, first_params)
    items = list(first_data.get(key, []))
    total = first_data.get("total_count", len(items))

    if total <= limit:
        return items

    # 2) 나머지 오프셋 계산
    offsets = list(range(limit, total, limit))
    print(f"  병렬 fetch: {len(offsets)+1}페이지 / 총 {total}건")

    def fetch_page(offset):
        params = {**(base_params or {}), "limit": limit, "offset": offset}
        data = fetch(path, params)
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


def get_projects():
    data = fetch("/projects.json", {"limit": 100})
    return data.get("projects", [])


def get_issues(project_id="", updated_after="2026-03-01"):
    params = {"status_id": "*"}
    if project_id:
        params["project_id"] = project_id
    if updated_after:
        params["updated_on"] = f">={updated_after}T00:00:00Z"
    return fetch_all("/issues.json", "issues", params)


def build_dashboard_data(project_id="", updated_after="2026-03-01"):
    issues = get_issues(project_id, updated_after)
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
    open_issues  = sum(1 for s in all_statuses if s not in CLOSED_SET)

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
    future_3 = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
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
        "imminent_issues":  imminent_issues,
        "trend_7days":      trend_7days,
    }


def get_versions(project_id=""):
    if not project_id:
        projects = get_projects()
        versions = []
        for p in projects:
            data = fetch(f"/projects/{p['identifier']}/versions.json")
            for v in data.get("versions", []):
                v["project_name"] = p["name"]
                versions.append(v)
        return versions
    else:
        data = fetch(f"/projects/{project_id}/versions.json")
        versions = data.get("versions", [])
        for v in versions:
            v["project_name"] = project_id
        return versions


def get_version_issues(version_id):
    return fetch_all("/issues.json", "issues", {
        "status_id": "*",
        "fixed_version_id": version_id,
    })


def build_version_data(project_id=""):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    versions = get_versions(project_id)
    today_str = date.today().strftime("%Y-%m-%d")
    active_versions = [v for v in versions if v.get("status") != "closed"]

    def fetch_version(v):
        issues_raw = get_version_issues(v["id"])
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

HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vantix — Project Risk Intelligence</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiBmaWxsPSJ3aGl0ZSIvPjx0ZXh0IHg9IjE2IiB5PSIyNCIgZm9udC1mYW1pbHk9IkFyaWFsLHNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMjIiIGZvbnQtd2VpZ2h0PSJib2xkIiBmaWxsPSIjY2MwMDAwIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj5WPC90ZXh0Pjwvc3ZnPg==">
<style>
.card, .section-card, .dashboard-card, [class*="card"], [class*="section"], .grid-item, .panel {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  border-radius: 0 !important;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  background: #faf9f7;
  color: #111;
  min-height: 100vh;
  font-size: 13px;
  -webkit-font-smoothing: antialiased;
}

/* ── Navbar ── */
.navbar {
  height: 58px;
  background: #faf9f7;
  border-bottom: 1px solid #111;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 36px;
  position: sticky;
  top: 0;
  z-index: 200;
}
.navbar-brand { display: flex; flex-direction: column; justify-content: center; }
.navbar-logo {
  font-size: 1.75rem;
  font-weight: 650;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #111;
  line-height: 0.9;
  font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
  transform: scaleY(1.15);
  display: inline-block;
}
.navbar-tagline {
  font-size: 10px;
  color: #e74c3c;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  margin-top: 3px;
  font-weight: 500;
  opacity: 0.85;
}
.navbar-actions { display: flex; align-items: center; gap: 8px; }
.btn {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  padding: 5px 12px;
  border-radius: 0;
  cursor: pointer;
  font-weight: 300;
  transition: all 0.1s;
}
.btn-ghost {
  background: transparent;
  border: 1px solid #e8e6e2;
  color: #555;
}
.btn-ghost:hover { border-color: #111; color: #111; }
.btn-black {
  background: #111;
  color: #fff;
  border: 1px solid #111;
}
.btn-black:hover { background: #333; border-color: #333; }

/* ── Filter Bar ── */
.filter-bar {
  background: #faf9f7;
  border-bottom: 1px solid #e8e6e2;
  padding: 10px 36px;
  display: flex;
  align-items: center;
  gap: 16px;
  position: sticky;
  top: 58px;
  z-index: 100;
}
.filter-bar select {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 11px;
  letter-spacing: 0.5px;
  padding: 5px 28px 5px 10px;
  border: 1px solid #ddd;
  border-radius: 0;
  background: #fff url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23999' stroke-width='1.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat right 9px center;
  color: #111;
  cursor: pointer;
  outline: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  appearance: none;
}
.filter-bar select:focus { border-color: #111; }
.filter-date-input {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 11px;
  color: #555;
  border: 1px solid #ddd;
  border-radius: 0;
  padding: 4px 8px;
  background: #fff;
  outline: none;
  cursor: pointer;
}
.filter-date-input:focus { border-color: #111; }
.filter-live { font-size: 11px; color: #999; font-weight: 300; display: flex; align-items: center; gap: 5px; }
.live-indicator { display: inline-flex; align-items: center; gap: 4px; }
.live-indicator::before {
  content: '';
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #16a34a;
  animation: livepulse 2s infinite;
}
@keyframes livepulse { 0%,100%{opacity:1} 50%{opacity:.3} }

/* ── Summary Strip ── */
.summary-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-top: 1px solid #111;
  border-bottom: 1px solid #e8e6e2;
  background: #faf9f7;
  scroll-margin-top: 42px;
}
.sum-card {
  padding: 32px 36px;
  border-right: 1px solid #e8e6e2;
  position: relative;
}
.sum-card:last-child { border-right: none; }
.sum-card.clickable { cursor: pointer; }
.sum-card.clickable:hover { background: #f7f5f2; }
.sum-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  color: #111;
  font-weight: 300;
  margin-bottom: 8px;
}
.sum-value {
  font-size: 56px;
  font-weight: 400;
  color: #111;
  line-height: 1;
  margin-bottom: 10px;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.sum-value.amber { color: #b7770d; }
.sum-value.red { color: #8b1a1a; }
.sum-spark {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.sum-delta { font-size: 10px; color: #aaa; font-weight: 300; letter-spacing: 0.5px; }
.sum-delta.up { color: #8b1a1a; }
.sum-delta.down { color: #16a34a; }
.sum-hint { font-size: 9px; color: #ccc; font-weight: 300; margin-top: 6px; letter-spacing: 0.15em; text-transform: uppercase; }
.risk-level-text { font-size: 1.5rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
.risk-sub { font-size: 10px; color: #aaa; margin-top: 6px; letter-spacing: 0.5px; }

/* ── AI Strip ── */
.ai-strip {
  background: #faf9f7;
  padding: 14px 36px;
  display: flex;
  align-items: flex-start;
  gap: 24px;
  border-bottom: 1px solid #e8e6e2;
}
.ai-strip-body { flex: 1; min-width: 0; }
.ai-strip-actions { display: flex; gap: 8px; flex-shrink: 0; align-items: flex-start; padding-top: 2px; }

/* ── Main Content ── */
.main-content {
  padding: 28px 36px;
  background: #faf9f7;
}
.masonry-grid {
  columns: 2;
  column-gap: 24px;
}
.masonry-grid .card {
  break-inside: avoid;
  margin-bottom: 24px;
  height: 320px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e8e6e2 !important;
  border-bottom: 1px solid #e8e6e2 !important;
}

/* ── Cards ── */
.card { background: #faf9f7; }
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px 10px;
  border-bottom: 1px solid #e8e6e2;
  flex-shrink: 0;
}
.card-header span:first-child {
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  font-weight: 700;
  color: #111;
}
.card-header-sub {
  font-size: 10px;
  color: #bbb;
  font-weight: 400;
  letter-spacing: 0.5px;
}
.card-body { overflow-y: auto; flex: 1; scrollbar-width: thin; scrollbar-color: #ddd transparent; }

/* ── Tables ── */
.data-table {
  width: 100%;
  border-collapse: collapse;
}
.data-table thead th {
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 2px;
  color: #bbb;
  font-weight: 400;
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid #e8e6e2;
  white-space: nowrap;
}
.data-table tbody tr {
  border-bottom: 1px solid #e8e6e2;
}
.data-table tbody tr:hover { background: #f7f5f2; }
.tab-table tbody tr { border-left: 3px solid transparent; }
.data-table tbody td {
  font-size: 11px;
  padding: 8px 12px;
  vertical-align: middle;
}
.data-table tbody tr:last-child { border-bottom: none; }
.issue-id {
  font-size: 10px;
  color: #bbb;
  white-space: nowrap;
  font-weight: 400;
  letter-spacing: 0.5px;
}
.issue-link { color: #bbb; text-decoration: none; }
.issue-link:hover { color: #111; }
.issue-subject { font-size: 11px; color: #111; max-width: 260px; font-weight: 300; }
.issue-meta { font-size: 10px; color: #bbb; margin-top: 2px; letter-spacing: 0.3px; font-weight: 300; }
.dday-text { font-size: 11px; white-space: nowrap; font-weight: 500; letter-spacing: 0.5px; }
.elapsed-text { font-size: 11px; white-space: nowrap; font-weight: 500; letter-spacing: 0.5px; }

/* ── Assignee table ── */
.dept-tag {
  font-size: 9px;
  font-weight: 500;
  white-space: nowrap;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.assignee-name { font-weight: 500; font-size: 11px; }
.count-cell { font-size: 11px; text-align: right; }
.overdue-count { color: #8b1a1a; font-weight: 600; }
.zero-count { color: #ccc; }

/* ── Version card ── */
.ver-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid #e8e6e2;
  font-size: 11px;
}
.ver-row:last-child { border-bottom: none; }
.ver-name { flex: 1; font-weight: 300; color: #111; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ver-due { font-size: 10px; white-space: nowrap; letter-spacing: 0.3px; }
.ver-progress-wrap { display: flex; align-items: center; gap: 6px; }
.ver-progress-bar { width: 100px; height: 4px; background: #d8d8d8; overflow: hidden; border-radius: 4px; }
.ver-progress-fill { height: 100%; background: #1a6e3c; border-radius: 4px; transition: width 0.3s; }
.ver-pct { font-size: 10px; color: #999; min-width: 36px; text-align: right; }

/* ── AI Summary ── */
.ai-card-label {
  font-size: 1.05rem;
  color: #1a3a6e;
  font-weight: 300;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.ai-text { font-size: 13px; color: #444; line-height: 1.8; white-space: pre-wrap; font-weight: 300; }
.ai-text.placeholder { color: #ccc; }

/* ── Bottom Tab Section ── */
.bottom-section {
  margin: 0 36px 32px;
}
.tab-bar {
  display: flex;
  padding: 12px 16px 0;
  overflow-x: auto;
}
.tab-pill-wrapper {
  position: relative;
  display: inline-flex;
  border: 1px solid #ddd;
  border-radius: 12px;
  padding: 3px;
  gap: 0;
}
.tab-hover-pill, .tab-active-pill {
  position: absolute;
  top: 3px;
  height: calc(100% - 6px);
  border-radius: 9px;
  pointer-events: none;
  transition: left 0.22s cubic-bezier(0.4,0,0.2,1),
              width 0.22s cubic-bezier(0.4,0,0.2,1),
              opacity 0.15s ease;
}
.tab-hover-pill {
  background: rgba(0,0,0,0.06);
  opacity: 0;
  z-index: 1;
}
.tab-active-pill {
  background: #fff;
  border: 1.5px solid #bbb;
  z-index: 2;
}
.tab-item {
  position: relative;
  z-index: 3;
  padding: 7px 16px;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 2px;
  cursor: pointer;
  color: #888;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 300;
  user-select: none;
  border-radius: 9px;
  border: none;
  background: transparent;
  transition: color 0.15s ease;
}
.tab-item:hover { color: #333; }
.tab-item.active { color: #111; font-weight: 600; }
.tab-badge {
  font-size: 9px;
  padding: 1px 5px;
  font-weight: 600;
}
.tab-badge.red { color: #8b1a1a; }
.tab-badge.blue { color: #2563eb; }
.tab-filters {
  padding: 10px 16px;
  border-bottom: 1px solid #e8e6e2;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.tab-filters select {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 11px;
  padding: 4px 28px 4px 8px;
  border: 1px solid #e0e0e0;
  border-radius: 0;
  background: #fff url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23999' stroke-width='1.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat right 9px center;
  color: #111;
  outline: none;
  cursor: pointer;
  -webkit-appearance: none;
  -moz-appearance: none;
  appearance: none;
}
.tab-filters input {
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 11px;
  padding: 4px 8px;
  border: 1px solid #e0e0e0;
  border-radius: 0;
  background: #fff;
  color: #111;
  outline: none;
}
.tab-filters input::placeholder { color: #ccc; }
.tab-filters select:focus,
.tab-filters input:focus { border-color: #111; }
.tab-count { font-size: 10px; color: #bbb; margin-left: auto; letter-spacing: 0.5px; }
.tab-content { overflow-x: auto; }
.tab-table tbody tr { cursor: pointer; }

/* ── Issue Modal ── */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.3);
  z-index: 1000;
  align-items: center;
  justify-content: center;
}
.modal-overlay.open { display: flex; }
.modal-box {
  background: #fff;
  border-radius: 0;
  width: 520px;
  max-width: 95vw;
  max-height: 88vh;
  overflow-y: auto;
  border: 1px solid #111;
}
.modal-header {
  padding: 18px 20px 14px;
  border-bottom: 1px solid #e8e6e2;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.modal-title { font-size: 13px; font-weight: 500; color: #111; line-height: 1.4; flex: 1; letter-spacing: 0.3px; }
.modal-close {
  width: 24px; height: 24px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; color: #999; font-size: 18px; flex-shrink: 0;
  border: none; background: none;
}
.modal-close:hover { color: #111; }
.modal-body { padding: 16px 20px; }
.modal-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.modal-meta-item {
  font-size: 10px;
  background: #f5f5f5;
  padding: 3px 8px;
  color: #555;
  letter-spacing: 0.3px;
}
.modal-field { margin-bottom: 14px; }
.modal-field label {
  display: block;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 2px;
  color: #aaa;
  font-weight: 400;
  margin-bottom: 5px;
}
.modal-field select,
.modal-field input,
.modal-field textarea {
  width: 100%;
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 12px;
  padding: 7px 10px;
  border: 1px solid #ddd;
  border-radius: 0;
  background: #fff;
  color: #111;
  outline: none;
}
.modal-field select:focus,
.modal-field input:focus,
.modal-field textarea:focus { border-color: #111; }
.modal-field textarea { resize: vertical; min-height: 70px; }
.modal-footer {
  padding: 12px 20px 16px;
  border-top: 1px solid #e8e6e2;
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  align-items: center;
}
.modal-redmine-link { font-size: 10px; color: #bbb; text-decoration: none; margin-right: auto; letter-spacing: 0.3px; }
.modal-redmine-link:hover { color: #111; }
.journals-list { margin-top: 8px; }
.journal-item { padding: 8px 0; border-bottom: 1px solid #e8e6e2; font-size: 11px; }
.journal-item:last-child { border-bottom: none; }
.journal-meta { font-size: 10px; color: #bbb; margin-bottom: 4px; }
.journal-notes { color: #444; line-height: 1.6; white-space: pre-wrap; }

/* ── Loading ── */
.loading-spinner {
  display: inline-block;
  width: 12px; height: 12px;
  border: 1px solid #e0e0e0;
  border-top-color: #111;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.loading-row td { text-align: center; padding: 24px; color: #bbb; font-size: 11px; }
.empty-row td { text-align: center; padding: 24px; color: #ccc; font-size: 11px; letter-spacing: 1px; }

/* ── Settings Modal ── */
.settings-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.45);
  z-index: 2000;
  align-items: center;
  justify-content: center;
}
.settings-overlay.open { display: flex; }
.settings-box {
  background: #fff;
  border: 1px solid #111;
  width: 420px;
  max-width: 95vw;
  padding: 0;
}
.settings-header {
  padding: 20px 24px 16px;
  border-bottom: 1px solid #e8e6e2;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.settings-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 3px;
  color: #111;
}
.settings-close {
  width: 24px; height: 24px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; color: #999; font-size: 18px;
  border: none; background: none;
}
.settings-close:hover { color: #111; }
.settings-body { padding: 24px; }
.settings-field { margin-bottom: 18px; }
.settings-field label {
  display: block;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 2.5px;
  color: #aaa;
  font-weight: 400;
  margin-bottom: 6px;
}
.settings-field input {
  width: 100%;
  box-sizing: border-box;
  font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  font-size: 12px;
  padding: 9px 12px;
  border: 1px solid #ddd;
  border-radius: 0;
  background: #fff;
  color: #111;
  outline: none;
  letter-spacing: 0.3px;
}
.settings-field input:focus { border-color: #111; }
.settings-footer {
  padding: 0 24px 24px;
  display: flex;
  flex-direction: column;
  gap: 0;
}
/* ── No Connection Overlay ── */
.no-conn-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: #faf9f7;
  z-index: 1500;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 16px;
}
.no-conn-overlay.visible { display: flex; }
.no-conn-title {
  font-size: 13px;
  font-weight: 400;
  color: #555;
  letter-spacing: 1px;
}
/* ── Settings Icon Button ── */
.btn-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 8px;
  height: 32px;
  cursor: pointer;
  background: transparent;
  border: none;
  opacity: 0.55;
  transition: opacity 0.15s;
}
.btn-icon:hover { opacity: 1; }
.btn-icon img { height: 18px; width: 18px; object-fit: contain; }

/* ── Responsive ── */
@media (max-width: 768px) {
  .summary-strip { grid-template-columns: repeat(2, 1fr); }
  .summary-strip .sum-card:nth-child(2) { border-right: none; }
  .summary-strip .sum-card:nth-child(3) { border-right: 1px solid #e8e6e2; }
  .main-content { padding: 16px; }
  .masonry-grid { columns: 1; }
  .ai-strip { padding: 12px 16px; flex-direction: column; gap: 10px; }
  .navbar { padding: 0 16px; }

}

/* 모바일: 버튼 아이콘화 + 카드 리스트 */
@media (max-width: 600px) {
  .navbar-tagline { display: none; }
  .navbar-logo { font-size: 1.2rem; }
  .btn-text-mobile { display: none; }
  .btn.btn-ghost, .btn.btn-black { padding: 5px 8px; min-width: 0; }
  .tab-issue-card { background: #fff !important; border: 1px solid #e8e8e8 !important; border-left: 4px solid #ccc; padding: 10px 12px; cursor: pointer; margin-bottom: 8px; }
  .tab-card-status { font-size: 10px; font-weight: 500; padding: 2px 8px; border-radius: 2px; }
  .tab-card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 5px; }
  .tab-card-id { font-size: 10px; color: #bbb; }
  .tab-card-title { font-size: 12px; color: #111; line-height: 1.5; margin-bottom: 6px; }
  .tab-card-meta { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .tab-card-date { font-size: 10px; color: #bbb; }
  .filter-bar { padding: 8px 16px; }
  .bottom-section { margin: 0 16px 24px; }
}
#tab-card-list { display: none !important; }
@media (max-width: 600px) {
  #tab-card-list { display: flex !important; flex-direction: column; gap: 8px; padding: 12px; background: #f0efed; }
  .tab-table { display: none !important; }
  .tab-issue-card { background: #fff !important; border: none !important; border-left: 4px solid #ccc !important; padding: 10px 12px !important; cursor: pointer; }
}
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar">
  <div class="navbar-brand">
    <div class="navbar-logo">VANTIX</div>
    <div class="navbar-tagline">AI Project Risk Intelligence</div>
  </div>
  <div class="navbar-actions">
    <button class="btn btn-ghost" onclick="openReport()" style="display:inline-flex;align-items:center;gap:5px;">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7 2v7M4.5 6.5L7 9l2.5-2.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 10v1a1 1 0 001 1h8a1 1 0 001-1v-1" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>
      <span class="btn-text-mobile">리포트</span>
    </button>
    <button class="btn btn-ghost" onclick="forceRefresh()" style="display:inline-flex;align-items:center;gap:5px;" title="강제 갱신">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M2 7a5 5 0 1 0 1-3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><path d="M2 2v3h3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="btn-text-mobile">강제 갱신</span>
    </button>
    <button class="btn btn-black" onclick="loadData()" style="display:inline-flex;align-items:center;gap:5px;" title="새로 고침">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7 2a5 5 0 1 1 0 10A5 5 0 0 1 7 2z" stroke="currentColor" stroke-width="1.1"/><path d="M7 5v2.5l1.5 1.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>
      <span class="btn-text-mobile">새로 고침</span>
    </button>
    <button class="btn-icon" onclick="openSettingsModal()" title="설정">
      <img src="/setupicon.png" alt="설정">
    </button>
  </div>
</nav>

<!-- Filter Bar -->
<div class="filter-bar">
  <select id="projectSelect" onchange="onProjectChange()">
    <option value="">전체 프로젝트</option>
  </select>
  <input type="date" id="filterDateInput" class="filter-date-input" value="__DEFAULT_UPDATED_AFTER__" onchange="onDateChange()" title="조회 기준일">
  <span class="filter-live">
    <span id="cacheAge">—</span>
    &nbsp;·&nbsp;
    <span class="live-indicator"><span style="color:#16a34a;font-weight:700;">LIVE</span></span>
  </span>
</div>

<!-- Summary Cards -->
<div class="summary-strip">
  <!-- Card 1: 전체 이슈 -->
  <div class="sum-card" id="card-total">
    <div class="sum-label">전체 이슈</div>
    <div class="sum-value" id="val-total">—</div>
    <div class="sum-spark">
      <svg id="spark-total" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-total">— 유지</span>
    </div>
  </div>
  <!-- Card 2: 오픈 이슈 -->
  <div class="sum-card clickable" id="card-open" onclick="goToTab('assignee')">
    <div class="sum-label">오픈 이슈</div>
    <div class="sum-value amber" id="val-open">—</div>
    <div class="sum-spark">
      <svg id="spark-open" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-open">—</span>
    </div>
    <div class="sum-hint">담당자별 현황 ↓</div>
  </div>
  <!-- Card 3: 마감 초과 -->
  <div class="sum-card clickable" id="card-overdue" onclick="goToTab('overdue')">
    <div class="sum-label">마감 초과</div>
    <div class="sum-value red" id="val-overdue">—</div>
    <div class="sum-spark">
      <svg id="spark-overdue" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-overdue">—</span>
    </div>
    <div class="sum-hint">마감 초과 이슈 ↓</div>
  </div>
  <!-- Card 4: 프로젝트 위험도 -->
  <div class="sum-card clickable" id="card-risk" onclick="goToTab('overdue')">
    <div class="sum-label">프로젝트 위험도</div>
    <div id="risk-level-row" style="margin-bottom:6px;">
      <span class="risk-dot" id="risk-dot" style="background:#ccc;"></span>
      <span class="risk-level-text" id="risk-level-text" style="color:#ccc;">—</span>
    </div>
    <div class="risk-gauge-wrap" id="risk-gauge-wrap" style="position:relative;">
      <div style="height:4px;background:#e5e5e5;border-radius:2px;overflow:hidden;margin-bottom:3px;">
        <div id="risk-gauge-fill" style="height:100%;width:0%;border-radius:2px;transition:width 0.6s ease;background:#ccc;"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
        <span style="font-size:9px;color:#16a34a;letter-spacing:0.04em;font-family:'Inter',sans-serif;">LOW</span>
        <span style="font-size:9px;color:#fb923c;letter-spacing:0.04em;font-family:'Inter',sans-serif;">HIGH</span>
        <span style="font-size:9px;color:#c0392b;letter-spacing:0.04em;font-family:'Inter',sans-serif;">CRITICAL</span>
      </div>
      <div class="risk-sub" id="risk-sub">—</div>
      <div id="risk-tooltip" style="display:none;position:absolute;bottom:calc(100% + 8px);left:0;background:#111;color:#fff;font-size:11px;line-height:1.7;padding:10px 14px;border-radius:6px;white-space:nowrap;z-index:999;pointer-events:none;">
        <div style="font-weight:600;margin-bottom:4px;letter-spacing:0.04em;">점수 산정 기준</div>
        <div>마감 초과 비율 × 60점</div>
        <div>마감 임박 비율 × 30점</div>
        <div>진행 대기 비율 × 10점</div>
        <div style="border-top:0.5px solid rgba(255,255,255,0.2);margin-top:6px;padding-top:6px;">
          <span style="color:#f87171;">Critical</span> ≥30 &nbsp;
          <span style="color:#fb923c;">High</span> ≥15 &nbsp;
          <span style="color:#fbbf24;">Medium</span> ≥5 &nbsp;
          <span style="color:#34d399;">Low</span> &lt;5
        </div>
      </div>
    </div>
    <div style="height:0.5px;background:#e8e8e8;margin:8px 0;"></div>
    <div id="risk-ai-comment" style="font-size:11px;color:#111;font-weight:600;line-height:1.6;min-height:16px;display:flex;gap:5px;align-items:flex-start;">
      <span style="color:#e74c3c;flex-shrink:0;">✦</span>
      <span id="risk-ai-comment-text">분석 중...</span>
    </div>
    <div class="sum-hint" style="margin-top:6px;">위험경보 ↓</div>
  </div>
</div>

<!-- AI Summary Strip (full-width, below KPI) -->
<div class="ai-strip" id="sec-ai-summary">
  <div class="ai-strip-body">
    <div class="ai-card-label" style="font-weight:700;">AI 주간 요약</div>
    <div class="ai-text placeholder" id="aiSummaryText">AI 분석을 실행하려면 'AI 재생성' 버튼을 클릭하세요.</div>
  </div>
  <div class="ai-strip-actions">
    <button class="btn btn-black" onclick="generateAiSummary()">AI 분석 갱신</button>
  </div>
</div>

<!-- Main Content: Masonry -->
<div class="main-content">
  <div class="masonry-grid">

    <!-- Card A: 마감 임박 -->
    <div class="card" id="sec-imminent">
      <div class="card-header">
        <span>마감 임박</span>
        <span class="card-header-sub" id="imminent-header-sub">D-3 이내 · 0건</span>
      </div>
      <div class="card-body">
        <table class="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>제목</th>
              <th>D-Day</th>
            </tr>
          </thead>
          <tbody id="imminent-tbody">
            <tr class="loading-row"><td colspan="3"><span class="loading-spinner"></span></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Card B: 담당자별 현황 -->
    <div class="card" id="sec-assignee">
      <div class="card-header">
        <span>담당자별 현황</span>
        <span class="card-header-sub">초과 많은 순</span>
      </div>
      <div class="card-body">
        <table class="data-table">
          <thead>
            <tr>
              <th>그룹</th>
              <th>담당자</th>
              <th style="text-align:right;">전체</th>
              <th style="text-align:right;">오픈</th>
              <th style="text-align:right;">초과</th>
            </tr>
          </thead>
          <tbody id="assignee-tbody">
            <tr class="loading-row"><td colspan="5"><span class="loading-spinner"></span></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Card C: 마감 초과 이슈 -->
    <div class="card" id="sec-overdue">
      <div class="card-header">
        <span>마감 초과 이슈</span>
        <span class="card-header-sub" id="overdue-header-sub">0건</span>
      </div>
      <div class="card-body">
        <table class="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>제목</th>
              <th>경과</th>
            </tr>
          </thead>
          <tbody id="overdue-tbody">
            <tr class="loading-row"><td colspan="3"><span class="loading-spinner"></span></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Card D: 버전/마일스톤 -->
    <div class="card" id="sec-version" style="display:none;">
      <div class="card-header">
        <span>버전 / 마일스톤</span>
        <span class="card-header-sub" id="version-project-name">—</span>
      </div>
      <div class="card-body" id="version-body">
        <div style="padding:16px;text-align:center;font-size:11px;color:#ccc;">로딩 중...</div>
      </div>
    </div>

  </div>
</div>

<!-- Bottom Tab Section -->
<div class="bottom-section">
  <div class="tab-bar">
    <div class="tab-pill-wrapper" id="tabPillWrapper">
      <div class="tab-hover-pill" id="tabHoverPill"></div>
      <div class="tab-active-pill" id="tabActivePill"></div>
      <div class="tab-item active" data-tab="imminent" onclick="switchTab('imminent')">
        마감 임박 <span class="tab-badge blue" id="badge-imminent">0</span>
      </div>
      <div class="tab-item" data-tab="overdue" onclick="switchTab('overdue')">
        마감 초과 <span class="tab-badge red" id="badge-overdue">0</span>
      </div>
      <div class="tab-item" data-tab="assignee" onclick="switchTab('assignee')">담당자별</div>
      <div class="tab-item" data-tab="all" onclick="switchTab('all')">전체 이슈</div>
    </div>
  </div>
  <div class="tab-filters">
    <select id="tabGroupFilter" onchange="renderTab()">
      <option value="">전체 그룹</option>
    </select>
    <input type="text" id="tabAssigneeSearch" placeholder="담당자 검색..." oninput="renderTab()">
    <select id="tabStatusFilter" onchange="renderTab()">
      <option value="">전체 상태</option>
    </select>
    <select id="tabSortFilter" onchange="renderTab()">
      <option value="due">마감일순</option>
      <option value="elapsed">D+N순</option>
      <option value="assignee">담당자순</option>
    </select>
    <span class="tab-count" id="tabCount">0건</span>
  </div>
  <div class="tab-content">
    <div id="tab-card-list"></div>
    <table class="data-table tab-table">
      <thead>
        <tr>
          <th>#</th>
          <th>제목</th>
          <th>그룹</th>
          <th>담당자</th>
          <th>상태</th>
          <th>마감일</th>
          <th>경과</th>
        </tr>
      </thead>
      <tbody id="tab-tbody">
        <tr class="loading-row"><td colspan="7"><span class="loading-spinner"></span></td></tr>
      </tbody>
    </table>
  </div>
</div>

<!-- Issue Edit Modal -->
<!-- Settings Modal -->
<div class="settings-overlay" id="settingsModal">
  <div class="settings-box">
    <div class="settings-header">
      <div class="settings-title">Redmine 연결 설정</div>
      <button class="settings-close" onclick="closeSettingsModal()">&#x2715;</button>
    </div>
    <div class="settings-body">
      <div class="settings-field">
        <label>Redmine URL</label>
        <input type="text" id="settingsUrl" placeholder="https://your-redmine.example.com" value="__REDMINE_BASE_URL__">
      </div>
      <div class="settings-field">
        <label>API Key</label>
        <input type="password" id="settingsApiKey" placeholder="API Key를 입력하세요" value="__REDMINE_API_KEY__">
      </div>
    </div>
    <div class="settings-footer">
      <div style="font-size:11px;color:#999;line-height:1.8;margin-bottom:14px;padding:10px 12px;background:#f7f7f7;border-left:2px solid #ddd;">
        💡 API 키 확인 →<br>Redmine 접속 › <b>내 계정</b> › 우측 하단 <b>API 액세스 키</b> › <b>표시</b> 클릭
      </div>
      <button class="btn btn-black" onclick="saveSettings()" style="width:100%;justify-content:center;">SAVE</button>
      <div style="font-size:11px;color:#bbb;text-align:center;margin-top:12px;display:flex;align-items:center;justify-content:center;gap:5px;">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="7" width="10" height="8" rx="1.5" stroke="#bbb" stroke-width="1.2"/><path d="M5 7V5a3 3 0 0 1 6 0v2" stroke="#bbb" stroke-width="1.2" stroke-linecap="round"/></svg>
        입력된 정보는 외부로 전송되지 않으며 브라우저에만 저장됩니다.
      </div>
    </div>
  </div>
</div>

<!-- No Connection Overlay -->
<div class="no-conn-overlay" id="noConnOverlay">
  <div class="no-conn-title">Redmine 연결이 필요합니다</div>
  <button class="btn btn-black" onclick="openSettingsModal()">설정하기</button>
</div>

<div class="modal-overlay" id="issueModal" onclick="closeModalOnOverlay(event)">
  <div class="modal-box">
    <div class="modal-header">
      <div class="modal-title" id="modalTitle">이슈 로딩 중...</div>
      <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-body">
      <div class="modal-meta" id="modalMeta"></div>
      <div class="modal-field">
        <label>상태</label>
        <select id="modalStatus"></select>
      </div>
      <div class="modal-field">
        <label>담당자</label>
        <select id="modalAssignee"></select>
      </div>
      <div class="modal-field">
        <label>마감일</label>
        <input type="date" id="modalDueDate">
      </div>
      <div class="modal-field">
        <label>버전</label>
        <select id="modalVersion"></select>
      </div>
      <div class="modal-field">
        <label>코멘트</label>
        <textarea id="modalNotes" placeholder="변경 내용 또는 메모를 입력하세요..."></textarea>
      </div>
      <div id="journalsSection" style="display:none;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.05em;color:#999;font-weight:500;margin-bottom:8px;">활동 이력</div>
        <div class="journals-list" id="journalsList"></div>
      </div>
    </div>
    <div class="modal-footer">
      <a id="modalRedmineLink" href="#" target="_blank" class="modal-redmine-link">Redmine에서 열기 &#x2197;</a>
      <button class="btn btn-ghost" onclick="closeModal()">취소</button>
      <button class="btn btn-black" onclick="saveIssue()">저장</button>
    </div>
  </div>
</div>

<script>
// ============================================================
// State
// ============================================================
var allData = null;
var currentProjectId = '__DEFAULT_PROJECT_ID__';
var currentUpdatedAfter = '__DEFAULT_UPDATED_AFTER__';
var currentTab = 'imminent';
var currentModalIssueId = null;
var aiSummaryText = '';

var REDMINE_BASE = '__REDMINE_BASE_URL__';

// ============================================================
// Dept colors & status badges
// ============================================================
var DEPT_COLORS = {
  '기획': '#c2410c',
  'PM':   '#15803d',
  '클라': '#1d4ed8',
  '서버': '#7e22ce',
  'UI':   '#be123c',
};
function deptTag(dept) {
  var c = DEPT_COLORS[dept] || '#888';
  return '<span class="dept-tag" style="color:' + c + ';">' + escHtml(dept || '—') + '</span>';
}

var STATUS_COLORS = {
  '진행': '#2563eb', '진행대기': '#7c3aed', 'In Progress': '#2563eb',
  '해결': '#16a34a', '해결됨': '#16a34a', 'Resolved': '#16a34a',
  '완료': '#6b7280', 'Closed': '#6b7280', '반려': '#6b7280', 'Rejected': '#6b7280',
  '보류': '#d97706', '보류(스펙아웃)': '#d97706', '스펙아웃': '#d97706',
};
function statusBadge(status) {
  var c = STATUS_COLORS[status] || '#999';
  return '<span style="color:' + c + ';font-size:10px;">' + escHtml(status) + '</span>';
}

var CLOSED_SET_JS = new Set(['완료','Closed','반려','Rejected','해결됨','Resolved','해결']);
var HOLD_SET_JS = new Set(['보류','보류(스펙아웃)','스펙아웃']);

// ============================================================
// D-Day helpers
// ============================================================
function dday(dueDateStr) {
  if (!dueDateStr) return null;
  var today = new Date(); today.setHours(0,0,0,0);
  var due = new Date(dueDateStr + 'T00:00:00');
  return Math.floor((due - today) / 86400000);
}

function ddayLabel(dueDateStr) {
  if (!dueDateStr) return '—';
  var d = dday(dueDateStr);
  if (d === null) return '—';
  if (d < 0) return 'D+' + Math.abs(d);
  if (d === 0) return 'D-Day';
  return 'D-' + d;
}

function ddayColor(dueDateStr) {
  if (!dueDateStr) return '#bbb';
  var d = dday(dueDateStr);
  if (d === null) return '#bbb';
  if (d < 0) return '#c0392b';
  if (d === 0) return '#f59e0b';
  if (d <= 3) return '#2563eb';
  return '#bbb';
}

// ============================================================
// Sparkline
// ============================================================
function renderSparkline(svgId, values, color) {
  var svg = document.getElementById(svgId);
  if (!svg || !values || values.length < 2) return;
  var W = 60, H = 18;
  var mn = Math.min.apply(null, values);
  var mx = Math.max.apply(null, values);
  var range = mx - mn || 1;
  var pts = values.map(function(v, i) {
    var x = (i / (values.length - 1)) * W;
    var y = H - ((v - mn) / range) * (H - 3) - 1;
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  svg.innerHTML = '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>';
}

// ============================================================
// Init
// ============================================================
async function init() {
  var savedUrl = localStorage.getItem('redmine_url');
  var savedKey = localStorage.getItem('redmine_apikey');
  if (!savedUrl || !savedKey) {
    openSettingsModal();
    return;
  }
  await loadProjects();
  await loadData();
}

async function loadProjects() {
  try {
    var res = await fetch('/api/projects');
    var projects = await res.json();
    var sel = document.getElementById('projectSelect');
    sel.innerHTML = '<option value="">전체 프로젝트</option>';
    projects.forEach(function(p) {
      var opt = document.createElement('option');
      opt.value = p.identifier;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
    if (currentProjectId) {
      sel.value = currentProjectId;
      if (sel.value !== currentProjectId) { currentProjectId = ''; }
    }
  } catch(e) {
    console.error('loadProjects error:', e);
  }
}

async function loadData() {
  var params = new URLSearchParams({ project_id: currentProjectId, updated_after: currentUpdatedAfter });
  try {
    var res = await fetch('/api/data?' + params, { signal: AbortSignal.timeout(15000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    allData = await res.json();
    setServerStatus('online');
    renderAll();
    generateAiSummary();
  } catch(e) {
    console.error('loadData error:', e);
    setServerStatus('offline');
    if (allData) renderAll();
  }
}

function setServerStatus(status) {
  var liveEl = document.querySelector('.live-indicator');
  var cacheEl = document.getElementById('cacheAge');
  if (status === 'offline') {
    liveEl.innerHTML = '<span style="color:#c0392b;font-weight:700;">OFF</span>';
    cacheEl.textContent = '서버 연결 끊김';
  } else {
    liveEl.innerHTML = '<span style="color:#16a34a;font-weight:700;">LIVE</span>';
  }
}

function renderAll() {
  if (!allData) return;
  renderSummaryCards();
  renderImminentCard();
  renderAssigneeCard();
  renderOverdueCard();
  renderVersionCard();
  renderTab();
  updateCacheAge();
  populateTabFilters();
}

// ============================================================
// Summary Cards
// ============================================================
function renderSummaryCards() {
  var d = allData;
  var trend = d.trend_7days || {};

  // Card 1 — 전체 이슈
  document.getElementById('val-total').textContent = (d.total_issues !== undefined && d.total_issues !== null) ? d.total_issues : '—';
  renderSparkline('spark-total', trend.open || [], '#999');
  document.getElementById('delta-total').textContent = '— 유지';

  // Card 2 — 오픈 이슈
  document.getElementById('val-open').textContent = (d.open_issues !== undefined && d.open_issues !== null) ? d.open_issues : '—';
  renderSparkline('spark-open', trend.open || [], '#b7770d');
  var dOpen = trend.delta_open || 0;
  var deltaOpenEl = document.getElementById('delta-open');
  deltaOpenEl.textContent = dOpen === 0 ? '— 유지' : (dOpen > 0 ? '+' + dOpen + ' 증가' : dOpen + ' 감소');
  deltaOpenEl.className = 'sum-delta ' + (dOpen > 0 ? 'up' : dOpen < 0 ? 'down' : '');

  // Card 3 — 마감 초과
  document.getElementById('val-overdue').textContent = (d.overdue !== undefined && d.overdue !== null) ? d.overdue : '—';
  renderSparkline('spark-overdue', trend.overdue || [], '#c0392b');
  var dOverdue = trend.delta_overdue || 0;
  var deltaOverdueEl = document.getElementById('delta-overdue');
  deltaOverdueEl.textContent = dOverdue === 0 ? '— 유지' : (dOverdue > 0 ? '+' + dOverdue + ' 증가' : dOverdue + ' 감소');
  deltaOverdueEl.className = 'sum-delta ' + (dOverdue > 0 ? 'up' : dOverdue < 0 ? 'down' : '');

  // Card 4 — 위험도
  var riskList = d.project_risk || [];
  var topRisk;
  if (!currentProjectId && riskList.length > 1) {
    // 전체 선택 시 평균 score 계산
    var avgScore = Math.round(riskList.reduce(function(s, r) { return s + r.risk_score; }, 0) / riskList.length * 10) / 10;
    var avgLevel = avgScore >= 30 ? 'Critical' : avgScore >= 15 ? 'High' : avgScore >= 5 ? 'Medium' : 'Low';
    topRisk = { risk_score: avgScore, risk_level: avgLevel, name: '전체 ' + riskList.length + '개 프로젝트 평균' };
  } else {
    topRisk = riskList[0];
  }
  var dotEl = document.getElementById('risk-dot');
  var levelEl = document.getElementById('risk-level-text');
  var subEl = document.getElementById('risk-sub');

  var RISK_COLORS = {
    Critical: { dot: '#f87171', text: '#c0392b' },
    High:     { dot: '#fb923c', text: '#fb923c' },
    Medium:   { dot: '#fbbf24', text: '#b7770d' },
    Low:      { dot: '#34d399', text: '#16a34a' },
  };

  if (topRisk) {
    var rc = RISK_COLORS[topRisk.risk_level] || { dot: '#ccc', text: '#999' };
    dotEl.style.background = rc.dot;
    levelEl.textContent = topRisk.risk_level;
    levelEl.style.color = rc.text;
    subEl.textContent = '';
    // 게이지 바
    var gaugeFill = document.getElementById('risk-gauge-fill');
    if (gaugeFill) {
      var s = topRisk.risk_score;
      var pct;
      if (s < 5)       pct = (s / 5) * 25;
      else if (s < 15) pct = 25 + ((s - 5) / 10) * 25;
      else if (s < 30) pct = 50 + ((s - 15) / 15) * 25;
      else             pct = 75 + Math.min((s - 30) / 70, 1) * 25;
      gaugeFill.style.width = pct.toFixed(1) + '%';
      gaugeFill.style.background = rc.dot;
    }
    // AI 코멘트 호출
    var commentEl = document.getElementById('risk-ai-comment-text');
    if (commentEl && topRisk.name) {
      commentEl.textContent = '분석 중...';
      var topIssues = (topRisk.issues_overdue || []).slice(0, 3).map(function(i) {
        return i.subject;
      }).join(', ');
      fetch('/api/ai/risk-comment?name=' + encodeURIComponent(topRisk.name) +
        '&score=' + topRisk.risk_score +
        '&overdue=' + (d.overdue || 0) +
        '&urgent=' + ((d.imminent_issues || []).length) +
        '&open_issues=' + (d.open_issues || 0) +
        '&top_issues=' + encodeURIComponent(topIssues))
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.comment) commentEl.textContent = data.comment;
          else commentEl.textContent = '—';
        })
        .catch(function() { commentEl.textContent = '—'; });
    }
  } else {
    dotEl.style.background = '#34d399';
    levelEl.textContent = 'Low';
    levelEl.style.color = '#16a34a';
    subEl.textContent = '위험 프로젝트 없음';
    var gaugeFill = document.getElementById('risk-gauge-fill');
    if (gaugeFill) { gaugeFill.style.width = '0%'; }
    var commentEl = document.getElementById('risk-ai-comment-text');
    if (commentEl) commentEl.textContent = '위험 프로젝트 없음';
  }
  var gaugeWrap = document.getElementById('risk-gauge-wrap');
  var tooltip = document.getElementById('risk-tooltip');
  if (gaugeWrap && tooltip) {
    gaugeWrap.addEventListener('mouseenter', function() { tooltip.style.display = 'block'; });
    gaugeWrap.addEventListener('mouseleave', function() { tooltip.style.display = 'none'; });
  }

  // Update badges
  document.getElementById('badge-overdue').textContent = d.overdue || 0;
  document.getElementById('badge-imminent').textContent = (d.imminent_issues || []).length;
}

// ============================================================
// Imminent Card
// ============================================================
function renderImminentCard() {
  var issues = allData.imminent_issues || [];
  var tbody = document.getElementById('imminent-tbody');
  document.getElementById('imminent-header-sub').textContent = 'D-3 이내 · ' + issues.length + '건';

  if (issues.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="3">마감 임박 이슈 없음</td></tr>';
    return;
  }

  tbody.innerHTML = issues.map(function(i) {
    var color = ddayColor(i.due_date);
    var label = ddayLabel(i.due_date);
    var meta = escHtml((i.assignee_short || i.assignee) + ' · ' + (i.dept || '') + ' · ' + i.status);
    return '<tr onclick="openIssue(' + i.id + ')">' +
      '<td class="issue-id"><a href="' + REDMINE_BASE + '/issues/' + i.id + '" target="_blank" class="issue-link" onclick="event.stopPropagation()">#' + i.id + '</a></td>' +
      '<td>' +
        '<div class="issue-subject">' + escHtml(i.subject) + '</div>' +
        '<div class="issue-meta">' + meta + '</div>' +
      '</td>' +
      '<td><span class="dday-text" style="color:' + color + ';">' + label + '</span></td>' +
      '</tr>';
  }).join('');
}

// ============================================================
// Assignee Card
// ============================================================
function renderAssigneeCard() {
  var usersData = allData.users_data || {};
  var todayStr = new Date().toISOString().slice(0,10);

  var rows = [];
  for (var uname in usersData) {
    var ud = usersData[uname];
    var issues = ud.issues || [];
    var total = issues.length;
    var open = issues.filter(function(i) { return !CLOSED_SET_JS.has(i.status); }).length;
    var overdue = issues.filter(function(i) {
      return i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    }).length;
    var dept = getDept(uname);
    var shortN = getShortName(uname);
    rows.push({ uname: uname, shortN: shortN, dept: dept, total: total, open: open, overdue: overdue });
  }
  rows.sort(function(a, b) { return b.overdue - a.overdue || b.open - a.open; });

  var tbody = document.getElementById('assignee-tbody');
  if (rows.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">데이터 없음</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(function(r) {
    return '<tr class="assignee-stat-row" onclick="selectAssigneeFromCard(this)" data-name="' + escHtml(r.shortN) + '" style="cursor:pointer;">' +
      '<td>' + deptTag(r.dept) + '</td>' +
      '<td class="assignee-name">' + escHtml(r.shortN) + '</td>' +
      '<td class="count-cell">' + r.total + '</td>' +
      '<td class="count-cell">' + r.open + '</td>' +
      '<td class="count-cell ' + (r.overdue > 0 ? 'overdue-count' : 'zero-count') + '">' + r.overdue + '</td>' +
      '</tr>';
  }).join('');
}

// ============================================================
// Overdue Card
// ============================================================
function renderOverdueCard() {
  var usersData = allData.users_data || {};
  var todayStr = new Date().toISOString().slice(0,10);

  var overdues = [];
  for (var uname in usersData) {
    var ud = usersData[uname];
    var issues = ud.issues || [];
    issues.forEach(function(i) {
      if (i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status)) {
        var d = dday(i.due_date);
        overdues.push(Object.assign({}, i, { assigneeShort: getShortName(uname), dept: getDept(uname), elapsed: d }));
      }
    });
  }
  overdues.sort(function(a, b) { return a.elapsed - b.elapsed; });

  document.getElementById('overdue-header-sub').textContent = overdues.length + '건';

  var tbody = document.getElementById('overdue-tbody');
  if (overdues.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="3">마감 초과 이슈 없음</td></tr>';
    return;
  }
  tbody.innerHTML = overdues.map(function(i) {
    var elapsedAbs = Math.abs(i.elapsed);
    var color = elapsedAbs > 7 ? '#c0392b' : '#b7770d';
    var label = 'D+' + elapsedAbs;
    var meta = escHtml(i.assigneeShort + ' · ' + i.dept + ' · ' + i.status);
    return '<tr onclick="openIssue(' + i.id + ')">' +
      '<td class="issue-id"><a href="' + REDMINE_BASE + '/issues/' + i.id + '" target="_blank" class="issue-link" onclick="event.stopPropagation()">#' + i.id + '</a></td>' +
      '<td>' +
        '<div class="issue-subject">' + escHtml(i.subject) + '</div>' +
        '<div class="issue-meta">' + meta + '</div>' +
      '</td>' +
      '<td><span class="elapsed-text" style="color:' + color + ';">' + label + '</span></td>' +
      '</tr>';
  }).join('');
}

// ============================================================
// Version Card
// ============================================================
async function renderVersionCard() {
  if (!currentProjectId) {
    document.getElementById('sec-version').style.display = 'none';
    return;
  }
  var card = document.getElementById('sec-version');
  card.style.display = 'block';
  document.getElementById('version-project-name').textContent = currentProjectId;
  var body = document.getElementById('version-body');
  body.innerHTML = '<div style="padding:16px;text-align:center;font-size:11px;color:#ccc;">로딩 중...</div>';

  try {
    var res = await fetch('/api/versions?project_id=' + encodeURIComponent(currentProjectId));
    var versions = await res.json();
    if (!versions || versions.length === 0) {
      body.innerHTML = '<div style="padding:16px;text-align:center;font-size:11px;color:#ccc;">버전 없음</div>';
      return;
    }
    body.innerHTML = versions.map(function(v) {
      var ddColor = v.due_date ? ddayColor(v.due_date) : '#bbb';
      var ddLbl = v.due_date ? ddayLabel(v.due_date) : '—';
      return '<div class="ver-row">' +
        '<div class="ver-name" title="' + escHtml(v.name) + '">' + escHtml(v.name) + '</div>' +
        (v.due_date ? '<div class="ver-due" style="color:' + ddColor + ';">' + v.due_date + ' <span style="font-size:9px;">' + ddLbl + '</span></div>' : '') +
        '<div class="ver-progress-wrap">' +
          '<div class="ver-progress-bar"><div class="ver-progress-fill" style="width:' + v.done_pct + '%;"></div></div>' +
          '<span class="ver-pct">' + v.done_pct + '%</span>' +
        '</div>' +
        '</div>';
    }).join('');
  } catch(e) {
    body.innerHTML = '<div style="padding:16px;text-align:center;font-size:11px;color:#c0392b;">로딩 실패</div>';
  }
}

// ============================================================
// ── Sliding Pill Tab ──
(function() {
  function initPill() {
    var wrapper = document.getElementById('tabPillWrapper');
    var hoverPill = document.getElementById('tabHoverPill');
    var activePill = document.getElementById('tabActivePill');
    if (!wrapper) return;

    function coords(el) {
      var wr = wrapper.getBoundingClientRect();
      var er = el.getBoundingClientRect();
      return { left: er.left - wr.left - 3, width: er.width };
    }

    function setActivePill(el) {
      var c = coords(el);
      activePill.style.left = c.left + 'px';
      activePill.style.width = c.width + 'px';
    }

    var tabs = wrapper.querySelectorAll('.tab-item');
    var activeEl = wrapper.querySelector('.tab-item.active') || tabs[0];
    setActivePill(activeEl);

    tabs.forEach(function(tab) {
      tab.addEventListener('mouseenter', function() {
        if (tab === activeEl) { hoverPill.style.opacity = '0'; return; }
        var c = coords(tab);
        hoverPill.style.left = c.left + 'px';
        hoverPill.style.width = c.width + 'px';
        hoverPill.style.opacity = '1';
      });
    });

    wrapper.addEventListener('mouseleave', function() {
      hoverPill.style.opacity = '0';
    });

    window._pillSyncTab = function(tabName) {
      var el = wrapper.querySelector('[data-tab="' + tabName + '"]');
      if (el) { activeEl = el; setActivePill(el); hoverPill.style.opacity = '0'; }
    };
  }
  document.addEventListener('DOMContentLoaded', initPill);
})();

// Tab Section
// ============================================================
function selectAssigneeFromCard(el) {
  var shortName = el.getAttribute('data-name');
  document.querySelectorAll('.assignee-stat-row').forEach(function(row) {
    row.style.backgroundColor = '';
  });
  el.style.backgroundColor = '#f7f5f2';
  var searchEl = document.getElementById('tabAssigneeSearch');
  if (searchEl) searchEl.value = shortName;
  switchTab('assignee');
  var bottomEl = document.querySelector('.bottom-section');
  if (bottomEl) bottomEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-item').forEach(function(el) {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  if (window._pillSyncTab) window._pillSyncTab(tab);
  // 담당자별 탭이 아닌 탭으로 전환 시 담당자 필터 초기화
  if (tab !== 'assignee') {
    var searchEl = document.getElementById('tabAssigneeSearch');
    if (searchEl) searchEl.value = '';
    document.querySelectorAll('.assignee-stat-row').forEach(function(row) {
      row.style.backgroundColor = '';
    });
  }
  renderTab();
}

function populateTabFilters() {
  if (!allData) return;
  var usersData = allData.users_data || {};

  var depts = new Set();
  var statuses = new Set();
  for (var uname in usersData) {
    depts.add(getDept(uname));
    (usersData[uname].issues || []).forEach(function(i) { statuses.add(i.status); });
  }

  var groupSel = document.getElementById('tabGroupFilter');
  var curGroup = groupSel.value;
  groupSel.innerHTML = '<option value="">전체 그룹</option>';
  Array.from(depts).sort().forEach(function(d) {
    var opt = document.createElement('option');
    opt.value = d; opt.textContent = d;
    groupSel.appendChild(opt);
  });
  if (curGroup) groupSel.value = curGroup;

  var statusSel = document.getElementById('tabStatusFilter');
  var curStatus = statusSel.value;
  statusSel.innerHTML = '<option value="">전체 상태</option>';
  Array.from(statuses).sort().forEach(function(s) {
    var opt = document.createElement('option');
    opt.value = s; opt.textContent = s;
    statusSel.appendChild(opt);
  });
  if (curStatus) statusSel.value = curStatus;
}

function getTabIssues() {
  if (!allData) return [];
  var usersData = allData.users_data || {};
  var todayStr = new Date().toISOString().slice(0,10);
  var future3 = new Date(); future3.setDate(future3.getDate() + 3);
  var future3Str = future3.toISOString().slice(0,10);

  var issues = [];
  for (var uname in usersData) {
    var ud = usersData[uname];
    (ud.issues || []).forEach(function(i) {
      issues.push(Object.assign({}, i, {
        _uname: uname,
        _dept: getDept(uname),
        _short: getShortName(uname),
        _elapsed: i.due_date ? dday(i.due_date) : null,
      }));
    });
  }

  // Filter by tab
  if (currentTab === 'overdue') {
    issues = issues.filter(function(i) {
      return i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    });
  } else if (currentTab === 'imminent') {
    issues = issues.filter(function(i) {
      return i.due_date && i.due_date >= todayStr && i.due_date <= future3Str &&
        !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    });
  }
  // 'assignee' and 'all' keep all issues

  // Apply user filters
  var groupF = document.getElementById('tabGroupFilter').value;
  var assigneeF = document.getElementById('tabAssigneeSearch').value.trim().toLowerCase();
  var statusF = document.getElementById('tabStatusFilter').value;
  var sortF = document.getElementById('tabSortFilter').value;

  if (groupF) issues = issues.filter(function(i) { return i._dept === groupF; });
  if (assigneeF) issues = issues.filter(function(i) {
    return i._short.toLowerCase().includes(assigneeF) || i._uname.toLowerCase().includes(assigneeF);
  });
  if (statusF) issues = issues.filter(function(i) { return i.status === statusF; });

  // Sort
  if (sortF === 'due') {
    issues.sort(function(a, b) { return (a.due_date || '9999') < (b.due_date || '9999') ? -1 : 1; });
  } else if (sortF === 'elapsed') {
    issues.sort(function(a, b) { return (a._elapsed !== null ? a._elapsed : 9999) - (b._elapsed !== null ? b._elapsed : 9999); });
  } else if (sortF === 'assignee') {
    issues.sort(function(a, b) { return a._uname.localeCompare(b._uname); });
  }

  return issues;
}

function renderTab() {
  var issues = getTabIssues();
  document.getElementById('tabCount').textContent = issues.length + '건';

  var todayStr = new Date().toISOString().slice(0,10);
  var tbody = document.getElementById('tab-tbody');

  if (issues.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">이슈 없음</td></tr>';
    return;
  }

  // 모바일 카드 렌더링
  var cardList = document.getElementById('tab-card-list');
  if (cardList) {
    cardList.innerHTML = issues.map(function(i) {
      var isOverdue = i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
      var elapsed, elapsedColor;
      if (i._elapsed !== null) {
        if (i._elapsed < 0) {
          elapsed = 'D+' + Math.abs(i._elapsed);
          elapsedColor = isOverdue ? (Math.abs(i._elapsed) > 7 ? '#c0392b' : '#b7770d') : '#c0392b';
        } else {
          elapsed = ddayLabel(i.due_date);
          elapsedColor = ddayColor(i.due_date);
        }
      } else { elapsed = '—'; elapsedColor = '#bbb'; }
      if (!window._deptColorMap) window._deptColorMap = {};
      if (!window._deptColorIndex) window._deptColorIndex = 0;
      var _palette = ['#f59e0b','#2563eb','#e11d48','#16a34a','#7c3aed','#111111','#0891b2','#f97316'];
      if (i._dept && !window._deptColorMap[i._dept]) {
        window._deptColorMap[i._dept] = _palette[window._deptColorIndex % _palette.length];
        window._deptColorIndex++;
      }
      var deptColor = (i._dept && window._deptColorMap[i._dept]) || '#aaa';
      var statusBg = i.status === '진행' ? '#dcfce7' : i.status === '완료' ? '#f0f0f0' : '#fef9c3';
      var statusColor = i.status === '진행' ? '#16a34a' : i.status === '완료' ? '#888' : '#92400e';
      return '<div class="tab-issue-card" onclick="openIssue(' + i.id + ')" style="border-left:4px solid ' + deptColor + ';">' +
        '<div class="tab-card-top">' +
          '<span class="tab-card-id">#' + i.id + '</span>' +
          '<span style="font-size:12px;font-weight:600;color:' + elapsedColor + ';">' + elapsed + '</span>' +
        '</div>' +
        '<div class="tab-card-title">' + escHtml(i.subject) + '</div>' +
        '<div class="tab-card-meta">' +
          deptTag(i._dept) +
          '<span style="font-size:11px;font-weight:500;color:#111;">' + escHtml(i._short) + '</span>' +
          '<span class="tab-card-status" style="background:' + statusBg + ';color:' + statusColor + ';">' + escHtml(i.status) + '</span>' +
          '<span class="tab-card-date">' + (i.due_date || '—') + '</span>' +
        '</div>' +
        '</div>';
    }).join('');
  }

  tbody.innerHTML = issues.map(function(i) {
    var isOverdue = i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    var elapsed, elapsedColor;
    if (i._elapsed !== null) {
      if (i._elapsed < 0) {
        elapsed = 'D+' + Math.abs(i._elapsed);
        elapsedColor = isOverdue ? (Math.abs(i._elapsed) > 7 ? '#c0392b' : '#b7770d') : '#c0392b';
      } else {
        elapsed = ddayLabel(i.due_date);
        elapsedColor = ddayColor(i.due_date);
      }
    } else {
      elapsed = '—';
      elapsedColor = '#bbb';
    }
    if (!window._deptColorMap) window._deptColorMap = {};
    if (!window._deptColorIndex) window._deptColorIndex = 0;
    var _palette = ['#f59e0b','#2563eb','#e11d48','#16a34a','#7c3aed','#111111','#0891b2','#f97316'];
    if (i._dept && !window._deptColorMap[i._dept]) {
      window._deptColorMap[i._dept] = _palette[window._deptColorIndex % _palette.length];
      window._deptColorIndex++;
    }
    var rowDeptColor = (i._dept && window._deptColorMap[i._dept]) || '#aaa';
    return '<tr onclick="openIssue(' + i.id + ')" style="border-left:3px solid ' + rowDeptColor + ';">' +
      '<td class="issue-id">#' + i.id + '</td>' +
      '<td>' +
        '<div class="issue-subject">' + escHtml(i.subject) + '</div>' +
        '<div class="issue-meta">' + escHtml(i.project) + '</div>' +
      '</td>' +
      '<td>' + deptTag(i._dept) + '</td>' +
      '<td style="font-size:11px;font-weight:500;">' + escHtml(i._short) + '</td>' +
      '<td>' + statusBadge(i.status) + '</td>' +
      '<td style="font-family:\\'DM Mono\\',monospace;font-size:10px;color:#999;">' + (i.due_date || '—') + '</td>' +
      '<td><span class="elapsed-text" style="color:' + elapsedColor + ';">' + elapsed + '</span></td>' +
      '</tr>';
  }).join('');
}

// ============================================================
// Cache age display
// ============================================================
function updateCacheAge() {
  var el = document.getElementById('cacheAge');
  if (!allData) { el.textContent = '—'; return; }
  var liveEl = document.querySelector('.live-indicator');
  if (liveEl && liveEl.textContent.includes('OFF')) return;
  if (allData.cached && allData.cache_age) {
    el.textContent = '캐시 ' + allData.cache_age;
  } else {
    el.textContent = '방금 갱신됨';
  }
}

// ============================================================
// Issue Modal
// ============================================================
async function openIssue(id) {
  currentModalIssueId = id;
  var modal = document.getElementById('issueModal');
  modal.classList.add('open');
  document.getElementById('modalTitle').textContent = '#' + id + ' 로딩 중...';
  document.getElementById('modalMeta').innerHTML = '';
  document.getElementById('modalStatus').innerHTML = '';
  document.getElementById('modalAssignee').innerHTML = '';
  document.getElementById('modalVersion').innerHTML = '';
  document.getElementById('modalDueDate').value = '';
  document.getElementById('modalNotes').value = '';
  document.getElementById('journalsSection').style.display = 'none';

  try {
    var res = await fetch('/api/issue/' + id);
    var data = await res.json();
    var issue = data.issue || {};
    var statuses = data.statuses || [];
    var assignees = data.assignees || [];
    var versions = data.versions || [];

    document.getElementById('modalTitle').textContent = issue.subject || ('이슈 #' + id);
    document.getElementById('modalRedmineLink').href = REDMINE_BASE + '/issues/' + id;

    // Meta
    var metaItems = [
      issue.project && issue.project.name,
      issue.tracker && issue.tracker.name,
      issue.priority && issue.priority.name,
      issue.created_on ? '생성 ' + issue.created_on.slice(0,10) : null,
    ].filter(Boolean);
    document.getElementById('modalMeta').innerHTML = metaItems.map(function(m) {
      return '<span class="modal-meta-item">' + escHtml(m) + '</span>';
    }).join('');

    // Status select
    var statusSel = document.getElementById('modalStatus');
    statusSel.innerHTML = statuses.map(function(s) {
      return '<option value="' + s.id + '" ' + (s.name === (issue.status && issue.status.name) ? 'selected' : '') + '>' + escHtml(s.name) + '</option>';
    }).join('');

    // Assignee select
    var assigneeSel = document.getElementById('modalAssignee');
    assigneeSel.innerHTML = '<option value="">— 미지정 —</option>' +
      assignees.map(function(a) {
        return '<option value="' + a.id + '" ' + (a.id === (issue.assigned_to && issue.assigned_to.id) ? 'selected' : '') + '>' + escHtml(a.name) + '</option>';
      }).join('');

    // Due date
    document.getElementById('modalDueDate').value = issue.due_date || '';

    // Version select
    var verSel = document.getElementById('modalVersion');
    verSel.innerHTML = '<option value="">— 버전 없음 —</option>' +
      versions.map(function(v) {
        return '<option value="' + v.id + '" ' + (v.id === (issue.fixed_version && issue.fixed_version.id) ? 'selected' : '') + '>' + escHtml(v.name) + '</option>';
      }).join('');

    // Journals
    var journals = (issue.journals || []).filter(function(j) { return j.notes; });
    if (journals.length > 0) {
      document.getElementById('journalsSection').style.display = 'block';
      var recent = journals.slice(-5).reverse();
      document.getElementById('journalsList').innerHTML = recent.map(function(j) {
        return '<div class="journal-item">' +
          '<div class="journal-meta">' + escHtml((j.user && j.user.name) || '?') + ' · ' + (j.created_on ? j.created_on.slice(0,10) : '') + '</div>' +
          '<div class="journal-notes">' + escHtml(j.notes) + '</div>' +
          '</div>';
      }).join('');
    }

  } catch(e) {
    document.getElementById('modalTitle').textContent = '로딩 실패';
    console.error('openIssue error:', e);
  }
}

async function saveIssue() {
  if (!currentModalIssueId) return;
  var statusSel = document.getElementById('modalStatus');
  var assigneeSel = document.getElementById('modalAssignee');
  var verSel = document.getElementById('modalVersion');
  var dueDate = document.getElementById('modalDueDate').value;
  var notes = document.getElementById('modalNotes').value;

  var body = {};
  if (statusSel.value) body.status_id = parseInt(statusSel.value);
  if (assigneeSel.value) body.assigned_to_id = parseInt(assigneeSel.value);
  if (verSel.value) body.fixed_version_id = parseInt(verSel.value);
  if (dueDate) body.due_date = dueDate;
  if (notes.trim()) body.notes = notes;

  try {
    var res = await fetch('/api/issue/' + currentModalIssueId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var result = await res.json();
    if (result.ok) {
      closeModal();
      await loadData();
    } else {
      alert('저장 실패: ' + (result.error || '알 수 없는 오류'));
    }
  } catch(e) {
    alert('저장 중 오류 발생');
    console.error(e);
  }
}

function closeModal() {
  document.getElementById('issueModal').classList.remove('open');
  currentModalIssueId = null;
}

function closeModalOnOverlay(e) {
  if (e.target === document.getElementById('issueModal')) closeModal();
}

// ============================================================
// AI Summary
// ============================================================
async function generateAiSummary() {
  var el = document.getElementById('aiSummaryText');
  el.textContent = '분석 중...';
  el.className = 'ai-text placeholder';
  try {
    var params = new URLSearchParams({ project_id: currentProjectId, updated_after: currentUpdatedAfter });
    var res = await fetch('/api/ai/report-summary?' + params);
    var data = await res.json();
    if (data.summary) {
      aiSummaryText = data.summary;
      el.className = 'ai-text';
      el.innerHTML = data.summary.split(String.fromCharCode(10)).join("<br>");
    } else if (data.error) {
      el.textContent = 'AI 오류: ' + data.error;
      el.className = 'ai-text placeholder';
    }
  } catch(e) {
    el.textContent = '요청 실패';
    el.className = 'ai-text placeholder';
  }
}

function openAiSummary() {
  document.getElementById('sec-ai-summary').scrollIntoView({ behavior: 'smooth' });
  if (!aiSummaryText) generateAiSummary();
}

async function includeInReport() {
  try {
    var params = new URLSearchParams({ project_id: currentProjectId, updated_after: currentUpdatedAfter });
    var res = await fetch('/api/report/preview?' + params);
    var html = await res.text();
    if (aiSummaryText) {
      var aiBlock = '<div style="background:#f9f9f9;border-left:3px solid #c0392b;padding:12px 16px;margin:16px 0;font-size:13px;line-height:1.7;">' + escHtml(aiSummaryText) + '</div>';
      html = html.replace('<table', aiBlock + '<table');
    }
    var blob = new Blob([html], { type: 'text/html' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'vantix-report-' + new Date().toISOString().slice(0,10) + '.html';
    a.click();
    URL.revokeObjectURL(url);
  } catch(e) {
    alert('리포트 생성 실패');
  }
}

async function openReport() {
  try {
    var params = new URLSearchParams({ project_id: currentProjectId, updated_after: currentUpdatedAfter });
    var res = await fetch('/api/report/preview?' + params);
    var html = await res.text();
    if (aiSummaryText) {
      var aiBlock = '<div style="background:#f9f9f9;border-left:3px solid #c0392b;padding:12px 16px;margin:16px 0;font-size:13px;line-height:1.7;">' + escHtml(aiSummaryText) + '</div>';
      html = html.replace('<table', aiBlock + '<table');
    }
    var blob = new Blob([html], { type: 'text/html' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'vantix-report-' + new Date().toISOString().slice(0,10) + '.html';
    a.click();
    URL.revokeObjectURL(url);
  } catch(e) {
    alert('리포트 생성 실패');
  }
}

// ============================================================
// Force Refresh
// ============================================================
async function forceRefresh() {
  try {
    await fetch('/api/cache/clear');
    await loadData();
  } catch(e) {
    console.error('forceRefresh error:', e);
  }
}

// ============================================================
// Project select change
// ============================================================
function onProjectChange() {
  currentProjectId = document.getElementById('projectSelect').value;
  loadData();
}

function onDateChange() {
  var val = document.getElementById('filterDateInput').value;
  if (val) { currentUpdatedAfter = val; loadData(); }
}

// ============================================================
// Tab navigation helper
// ============================================================
function goToTab(tabName) {
  switchTab(tabName);
  var el = document.querySelector('.bottom-section');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ============================================================
// Scroll helper
// ============================================================
function scrollToSection(id) {
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ============================================================
// Settings Modal
// ============================================================
function openSettingsModal() {
  document.getElementById('noConnOverlay').classList.remove('visible');
  document.getElementById('settingsModal').classList.add('open');
}

function closeSettingsModal() {
  document.getElementById('settingsModal').classList.remove('open');
  var url = localStorage.getItem('redmine_url');
  var key = localStorage.getItem('redmine_apikey');
  if (!url || !key) {
    document.getElementById('noConnOverlay').classList.add('visible');
  }
}

function saveSettings() {
  var url = document.getElementById('settingsUrl').value.trim();
  var key = document.getElementById('settingsApiKey').value.trim();
  if (!url || !key) return;
  localStorage.setItem('redmine_url', url);
  localStorage.setItem('redmine_apikey', key);
  document.getElementById('settingsModal').classList.remove('open');
  document.getElementById('noConnOverlay').classList.remove('visible');
  loadProjects();
  loadData();
}

// ============================================================
// Utility
// ============================================================
function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function getDept(uname) {
  if (!uname) return '—';
  var parts = uname.split('_');
  if (parts.length >= 2) {
    var dept = parts[0].replace(/^[0-9]+/, '');
    return dept || '—';
  }
  return uname;
}

function getShortName(uname) {
  if (!uname) return '—';
  var parts = uname.split('_');
  return parts.length >= 2 ? parts[parts.length - 1] : uname;
}

// ============================================================
// Boot
// ============================================================
document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>"""


# ==================== API 라우터 ====================

@app.get("/setupicon.png")
async def serve_setupicon():
    import os
    from fastapi.responses import FileResponse
    icon_path = os.path.join(os.path.dirname(__file__), "setupicon.png")
    return FileResponse(icon_path, media_type="image/png")

@app.get("/", response_class=HTMLResponse)
async def root():
    # config 모듈에서 기본 날짜 값을 가져옵니다 (이미 코드 중간에 import 되어 있음)
    from config import DEFAULT_UPDATED_AFTER, DEFAULT_PROJECT_ID
    html = HTML_PAGE.replace("__REDMINE_BASE_URL__", BASE_URL.rstrip("/"))
    html = html.replace("__REDMINE_API_KEY__", API_KEY or "")
    html = html.replace("__DEFAULT_UPDATED_AFTER__", DEFAULT_UPDATED_AFTER)
    html = html.replace("__DEFAULT_PROJECT_ID__", DEFAULT_PROJECT_ID or "")

    return HTMLResponse(content=html)

@app.get("/api/projects")
async def api_projects():
    projects = get_projects()
    return [{"identifier": p["identifier"], "name": p["name"]} for p in projects]


@app.get("/api/data")
async def api_data(project_id: str = "", updated_after: str = "2026-03-01", force: bool = False):
    key = cache_key(project_id, updated_after)
    _auto_refresh_params[key] = {"project_id": project_id, "updated_after": updated_after}
    if not force:
        cached = get_cache(project_id, updated_after)
        if cached:
            age = cache_age_str(project_id, updated_after)
            print(f"  캐시 히트: {key} ({age})")
            return {**cached, "cached": True, "cache_age": age}
    print(f"  Redmine fetch: {key}")
    data = build_dashboard_data(project_id, updated_after)
    set_cache(project_id, updated_after, data)
    return {**data, "cached": False, "cache_age": None}


@app.get("/api/versions")
async def api_versions(project_id: str = "", force: bool = False):
    vkey = f"versions|{project_id}"
    if not force:
        entry = _cache.get(vkey)
        if entry:
            age = (datetime.now() - entry["fetched_at"]).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return entry["data"]
    data = build_version_data(project_id)
    if data:  # 빈 결과는 캐시하지 않음
        _cache[vkey] = {"data": data, "fetched_at": datetime.now()}
    return data


@app.get("/api/cache/clear")
async def clear_cache():
    _cache.clear()
    return {"ok": True, "message": "캐시 초기화 완료"}


@app.get("/api/visitors")
async def visitors(request: Request):
    ip = request.client.host
    _visitors[ip] = datetime.now()
    active = get_active_visitors()
    return {"count": len(active)}


@app.get("/api/issue/{issue_id}")
def api_get_issue(issue_id: int):
    from concurrent.futures import ThreadPoolExecutor
    issue_data = fetch(f"/issues/{issue_id}.json", {"include": "journals"})
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
async def api_update_issue(issue_id: int, request: Request):
    body = await request.json()
    payload = {"issue": {}}
    if "status_id"        in body: payload["issue"]["status_id"]         = body["status_id"]
    if "assigned_to_id"   in body: payload["issue"]["assigned_to_id"]    = body["assigned_to_id"]
    if "fixed_version_id" in body: payload["issue"]["fixed_version_id"]  = body["fixed_version_id"]
    if "due_date"         in body: payload["issue"]["due_date"]           = body["due_date"]
    if "notes"            in body and body["notes"].strip():
        payload["issue"]["notes"] = body["notes"]

    url = BASE_URL.rstrip("/") + f"/issues/{issue_id}.json"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"X-Redmine-API-Key": API_KEY, "Content-Type": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/report/preview")
async def api_report_preview(project_id: str = "", updated_after: str = "2026-03-01"):
    from fastapi.responses import HTMLResponse
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
    report = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    html   = render_html_report(report)
    return HTMLResponse(content=html)


@app.post("/api/report/send")
async def api_report_send(project_id: str = "", updated_after: str = "2026-03-01"):
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
    report  = build_report_data(dashboard, project_label=project_id or "전체 프로젝트")
    html    = render_html_report(report)
    subject = f"[Vantix] 주간 리포트 {report.period_label}"
    return send_report_email(html, subject, EMAIL_CFG)


# ==================== AI 엔드포인트 ====================

@app.get("/api/ai/risk-comment")
async def api_ai_risk_comment(
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


@app.get("/api/ai/report-summary")
async def api_ai_report_summary(project_id: str = "", updated_after: str = "2026-03-01"):
    cache_k = f"report-summary|{project_id}|{updated_after}"
    cached = _get_ai_cache(cache_k)
    if cached:
        return {"summary": cached}
    dashboard = get_cache(project_id, updated_after)
    if not dashboard:
        dashboard = build_dashboard_data(project_id, updated_after)
    top_risk  = dashboard.get("project_risk", [])[:3]
    top_names = ", ".join(f"{p['name']}({p['risk_level']}, score {p['risk_score']})" for p in top_risk) or "없음"
    users_data = dashboard.get("users_data", {})
    top_user = max(
        users_data.items(),
        key=lambda kv: sum(1 for i in kv[1]["issues"]
                           if i.get("due_date") and i["due_date"] < date.today().strftime("%Y-%m-%d")
                           and i["status"] not in CLOSED_SET),
        default=(None, None)
    )
    top_user_name = short_name(top_user[0]) if top_user[0] else "없음"
    prompt = (
        f"다음 주간 프로젝트 현황을 PM 보고용으로 3~5문장으로 요약해줘.\n"
        f"전체이슈: {dashboard.get('total_issues', 0)}, 오픈: {dashboard.get('open_issues', 0)}, "
        f"마감초과: {dashboard.get('overdue', 0)}건\n"
        f"프로젝트별 위험도: {top_names}\n"
        f"가장 초과 많은 담당자: {top_user_name if top_user_name else '없음'}\n"
        f"규칙: 마감초과가 0건이면 절대 위험하다고 쓰지 말 것. 오픈이슈가 0이면 모든 이슈가 완료된 상태임. "
        f"위험도 레벨(Low/Medium/High/Critical)을 그대로 문장에 자연스럽게 녹여서 사용할 것. "
        f"예: 'Low 수준으로 안정적', 'Critical로 즉각 대응 필요'. "
        f"담당자가 없거나 초과가 없으면 업무 분담 검토 언급 금지.\n"
        f"형식: 핵심 수치 포함, 실무적인 한국어, 제목/헤딩 없이 본문만.\n"
        f"문장은 2~3문장씩 묶어서 단락을 나눠줘. 단락 구분은 빈 줄 없이 자연스럽게 이어써줘."
    )
    try:
        summary = _call_claude(prompt, max_tokens=300)
        _set_ai_cache(cache_k, summary)
        return {"summary": summary}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/delay-prediction")
async def api_ai_delay_prediction(
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
