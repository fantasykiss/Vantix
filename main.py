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

# ── Risk 스냅샷 저장 ──────────────────────────────────────────
import json, os
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

    # 프로젝트별 스냅샷
    for p in projects:
        pname = p.get("name", "")
        if not pname:
            continue
        key = f"project_{pname}"
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
save_risk_snapshot()  # 서버 시작 시 즉시 1회 실행
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


def get_groups(project_id=""):
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
        members = fetch(f"/projects/{project_id}/memberships.json").get("memberships", [])
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
        issues = get_issues(project_id, updated_after="2024-01-01")

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
.card, .section-card, .dashboard-card, [class*="card"]:not(.ai-card-dark):not([class*="tl-"]), [class*="section"]:not(.ai-analysis-section), .grid-item, .panel {
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
  height: 72px;
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid rgba(207, 196, 197, 0.25);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 40px;
  position: sticky;
  top: 0;
  z-index: 200;
  font-family: 'DM Sans', sans-serif;
}
.navbar-brand {
  display: flex;
  flex-direction: column;
  justify-content: center;
  flex-shrink: 0;
}
.navbar-logo {
  font-family: 'DM Sans', sans-serif;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: #111;
  line-height: 1;
}
.navbar-tagline {
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #e74c3c;
  margin-top: 4px;
  font-weight: 400;
}
.navbar-center {
  display: flex;
  align-items: center;
  gap: 28px;
  flex: 1;
  padding: 0 40px;
}
.navbar-filter-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.navbar-filter-label {
  font-size: 8px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #aaa;
  font-family: 'DM Sans', sans-serif;
}
.navbar-filter-value {
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: #111;
  font-weight: 500;
  border-bottom: 1.5px solid #111;
  padding-bottom: 3px;
  font-family: 'DM Sans', sans-serif;
}
.navbar-filter-value select,
.navbar-filter-value input[type="date"] {
  font-family: 'DM Sans', sans-serif;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: #111;
  font-weight: 500;
  background: transparent;
  border: none;
  outline: none;
  cursor: pointer;
  padding: 0;
  appearance: none;
  -webkit-appearance: none;
}
.navbar-filter-value select {
  padding-right: 14px;
  background-image: url("data:image/svg+xml,%3Csvg width='8' height='5' viewBox='0 0 8 5' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l3 3 3-3' stroke='%23666' stroke-width='1.2' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 0 center;
}
.navbar-divider {
  width: 1px;
  height: 28px;
  background: rgba(207, 196, 197, 0.4);
  flex-shrink: 0;
}
.navbar-live {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.navbar-live-val {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #555;
  display: flex;
  align-items: center;
  gap: 5px;
  font-family: 'DM Sans', sans-serif;
  white-space: nowrap;
  min-width: 120px;
}
.navbar-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
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
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.btn-ghost:hover { border-color: #111; color: #111; }
.btn-black {
  background: #111;
  color: #fff;
  border: 1px solid #111;
}
.btn-black:hover { background: #333; border-color: #333; }
.btn-primary-outline {
  background: transparent;
  border: 1.5px solid #111;
  color: #111;
  font-weight: 700;
  letter-spacing: 0.18em;
}
.btn-primary-outline:hover {
  background: #111;
  color: #fff;
}

/* ── Filter Bar (통합됨 — 사용 안 함) ── */
.filter-bar { display: none; }
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
  grid-template-columns: 3fr 7fr;
  gap: 0;
  background: #fbf9f8;
  margin-bottom: 0;
}
.summary-row2 {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 0;
  background: #fbf9f8;
  margin-bottom: 24px;
}
.sum-card {
  padding: 32px 36px;
  border-right: 1px solid #e8e6e2;
  position: relative;
  background: #ffffff;
}
.sum-card.gray {
  background: #f5f3f3;
}
.sum-card.wide {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.risk-members-section { background: #fff; padding: 0; }
.risk-members-header { display: flex; justify-content: space-between; align-items: flex-end; padding: 14px 18px 12px; border-bottom: 1px solid rgba(0,0,0,0.1); margin-bottom: 0; }
.risk-members-label { font-size: 10px; font-weight: 500; letter-spacing: 0.2em; text-transform: uppercase; color: rgba(0,0,0,0.4); margin-bottom: 2px; }
.risk-members-title { font-size: 12px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: #000; }
.risk-member-row { display: block; border-bottom: 1px solid rgba(0,0,0,0.06); cursor: pointer; transition: background 0.15s; }
.risk-member-row:last-child { border-bottom: none; }
.risk-member-row:hover { background: #f5f3f3; }
.risk-member-row.selected { background: #f5f3f3; outline: 1px solid rgba(0,0,0,0.15); }
.risk-member-top { display: flex; align-items: center; gap: 10px; padding: 11px 18px; }
.risk-member-name-block { flex: 0 0 20%; min-width: 0; }
.risk-member-bar-section { display: flex; flex-direction: column; gap: 5px; flex: 0 0 55%; min-width: 0; }
.risk-member-bar-row { display: flex; align-items: center; gap: 6px; }
.risk-member-bar-lbl { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(0,0,0,0.35); width: 28px; flex-shrink: 0; }
.risk-member-bar-track { height: 3px; background: #f0f0f0; flex: 1; min-width: 0; max-width: 600px; overflow: hidden; }
.risk-member-bar-fill { height: 100%; transition: width 0.4s ease; }
.risk-member-bar-val { font-size: 11px; font-weight: 700; width: 36px; text-align: right; flex-shrink: 0; }
.risk-member-avatar { width: 30px; height: 30px; border-radius: 0; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; flex-shrink: 0; margin-right: 12px; }
.risk-member-info { flex: 1; min-width: 0; }
.risk-member-name { font-size: 13px; font-weight: 700; letter-spacing: -0.01em; text-transform: uppercase; color: #000; }
.risk-member-dept { font-size: 10px; color: rgba(0,0,0,0.4); margin-top: 1px; }
.risk-member-stats { display: flex; align-items: center; gap: 14px; flex-shrink: 0; margin-right: 10px; }
.risk-member-stat { text-align: right; }
.risk-member-stat-val { font-size: 13px; font-weight: 700; line-height: 1; }
.risk-member-stat-label { font-size: 9px; color: rgba(0,0,0,0.4); text-transform: uppercase; letter-spacing: 0.07em; margin-top: 2px; }
.risk-member-divider { width: 1px; height: 22px; background: rgba(0,0,0,0.08); }
.risk-member-badge { font-size: 9px; font-weight: 700; padding: 2px 7px; border-radius: 0; letter-spacing: 0.08em; text-transform: uppercase; align-self: center; }
.risk-member-arrow { font-size: 13px; color: rgba(0,0,0,0.25); margin-left: 10px; flex-shrink: 0; }
.risk-member-row:hover .risk-member-arrow,
.risk-member-row.selected .risk-member-arrow { color: #000; }
.risk-metric-row { margin-bottom: 12px; }
.risk-metric-row:last-child { margin-bottom: 0; }
.risk-metric-header { display: flex; justify-content: space-between; font-size: 10px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 5px; }
.risk-gauge-bg { width: 100%; height: 3px; background: rgba(0,0,0,0.06); }
.risk-gauge-fill { height: 3px; }
.risk-task-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.risk-task-row:last-child { margin-bottom: 0; }
.risk-task-left { display: flex; gap: 8px; flex: 1; min-width: 0; }
.risk-task-num { font-size: 9px; font-weight: 900; color: rgba(0,0,0,0.2); flex-shrink: 0; padding-top: 2px; }
.risk-task-name { font-size: 11px; font-weight: 500; letter-spacing: 0.02em; text-transform: uppercase; color: #000; line-height: 1.4; }
.risk-task-badge { font-size: 9px; font-weight: 700; padding: 2px 7px; flex-shrink: 0; letter-spacing: 0.06em; }
.sum-card.clickable { cursor: pointer; }
.sum-card.clickable:hover { background: #f7f5f2; }
.sum-label {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #999;
  margin-bottom: 16px;
  font-family: 'DM Sans', sans-serif;
}
.sum-value {
  font-size: 52px;
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1;
  color: #111;
}
.sum-value.amber { color: #d97706; }
.sum-value.red   { color: #e74c3c; }
.sum-value.blue  { color: #0047A0; }
.sum-spark {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sum-delta { font-size: 11px; color: #999; }
.sum-delta.up { color: #8b1a1a; }
.sum-delta.down { color: #16a34a; }
.sum-hint { font-size: 9px; color: #bbb; letter-spacing: 0.08em; }
.trend-area {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  height: 100%;
}
.trend-area svg {
  width: 100%;
  height: 100px;
}
.risk-level-text { font-size: 1.5rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
#risk-score-display {
  font-family: 'DM Sans', sans-serif;
  font-weight: 800;
  font-size: 90px;
  line-height: 1;
  letter-spacing: -2px;
  transition: color 0.4s ease;
}
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

/* ── AI Analysis Section ── */
.ai-analysis-section {
  display: grid;
  grid-template-columns: 1fr minmax(280px, 35%);
  gap: 0;
  margin: 0 16px 16px;
  align-items: stretch;
}
.ai-chart-card {
  background: #fff;
  border-radius: 0;
  border: 0.5px solid #e0ddd8;
  padding: 20px;
  position: relative;
}
.ai-chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.ai-chart-title {
  font-family: 'DM Sans', sans-serif;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #111;
}
.ai-chart-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.ai-legend {
  display: flex;
  gap: 12px;
  align-items: center;
}
.ai-legend-item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  font-weight: 500;
  color: #666;
}
.ai-legend-dot { width: 20px; height: 2px; border-radius: 1px; flex-shrink: 0; }
.ai-gear-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: #bbb;
  font-size: 13px;
  padding: 2px 4px;
  border-radius: 4px;
  line-height: 1;
}
.ai-gear-btn:hover { background: #f5f3f0; color: #666; }
.ai-gear-btn.active { color: #333; background: #f0eeeb; }
.ai-settings-panel {
  display: none;
  position: absolute;
  top: 44px;
  right: 20px;
  background: #fff;
  border: 0.5px solid #e0ddd8;
  border-radius: 8px;
  padding: 14px 16px;
  z-index: 10;
  width: 220px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.08);
}
.ai-settings-panel.open { display: block; }
.ai-settings-label {
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  font-weight: 600;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 10px;
}
.ai-settings-row { margin-bottom: 12px; }
.ai-settings-row label {
  font-family: 'DM Sans', sans-serif;
  font-size: 11px;
  color: #555;
  display: block;
  margin-bottom: 6px;
}
.ai-dow-btns { display: flex; gap: 4px; }
.ai-dow-btn {
  flex: 1;
  padding: 5px 2px;
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  font-weight: 500;
  border: 0.5px solid #e0ddd8;
  border-radius: 4px;
  background: #fff;
  color: #666;
  cursor: pointer;
  text-align: center;
}
.ai-dow-btn.active { background: #111; color: #fff; border-color: #111; }
.ai-settings-apply {
  width: 100%;
  padding: 8px;
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  background: #111;
  color: #fff;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  margin-top: 4px;
}
.ai-chart-sub {
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  color: #aaa;
  margin-bottom: 12px;
}
.ai-chart-area {
  position: relative;
  height: 220px;
}
.ai-gridlines {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  pointer-events: none;
}
.ai-gridline { width: 100%; border-top: 1px solid #f0eeeb; }
.ai-bars {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 100%;
  display: flex;
  align-items: flex-end;
  gap: 20px;
  border-bottom: 1px solid #e8e6e2;
}
.ai-bar-group {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  height: 100%;
  justify-content: flex-end;
}
.ai-bar { width: 100%; border-radius: 2px 2px 0 0; min-height: 3px; }
.ai-bar.low      { background: #ccc; }
.ai-bar.normal   { background: #333; }
.ai-bar.critical { background: #c0392b; }
.ai-bar-labels {
  display: flex;
  gap: 10px;
  margin-top: 6px;
}
.risk-chart-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  width: 100%;
  color: #999;
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.ai-bar-label {
  flex: 1;
  text-align: center;
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  color: #aaa;
}
.ai-card-dark {
  background-color: #111111;
  border-radius: 10px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}
.ai-label-mono {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #fff;
  background: #1a5276;
  padding: 4px 10px;
  display: inline-block;
  margin-bottom: 10px;
}
.ai-card-headline {
  font-family: 'DM Sans', sans-serif;
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  line-height: 1.3;
  letter-spacing: -0.01em;
  margin-bottom: 14px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ai-card-body-text {
  font-family: 'DM Sans', sans-serif;
  font-size: 11px;
  color: #888;
  line-height: 1.6;
  flex: 1;
  margin-bottom: 20px;
}
.ai-refresh-btn {
  background: #fff;
  color: #111;
  border: none;
  border-radius: 0;
  padding: 10px 16px;
  font-family: 'DM Sans', sans-serif;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  cursor: pointer;
  width: 100%;
  margin-top: auto;
}
.ai-card-dark {
  background: rgb(17,17,17);
  border-radius: 0;
  padding: 20px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.ai-label-mono {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #fff;
  background: #1a5276;
  padding: 4px 10px;
  display: inline-block;
  margin-bottom: 10px;
}
.ai-card-headline {
  font-family: 'DM Sans', sans-serif;
  font-size: 18px;
  font-weight: 700;
  color: #fff;
  line-height: 1.25;
  letter-spacing: -0.02em;
  margin-bottom: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ai-card-body-text {
  font-family: 'DM Sans', sans-serif;
  font-size: 11px;
  color: #888;
  line-height: 1.6;
  margin-bottom: 20px;
}

/* ── Main Content ── */
.main-content {
  padding: 28px 36px;
  background: #faf9f7;
}
.masonry-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  align-items: start;
}
.masonry-grid .card {
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e8e6e2;
  border-bottom: 1px solid #e8e6e2;
  min-height: 340px;
  overflow: hidden;
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
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 700;
  color: #111;
}
.card-header-sub {
  font-size: 10px;
  color: #bbb;
  font-weight: 400;
  letter-spacing: 0.5px;
}
.card-body { overflow-y: auto; flex: 1; min-height: 0; scrollbar-width: thin; scrollbar-color: #ddd transparent; }

/* ── Tables ── */
.data-table {
  width: 100%;
  border-collapse: collapse;
}
.data-table thead th {
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: #111;
  font-weight: 700;
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid #e8e6e2;
  white-space: nowrap;
}
.data-table tbody tr {
  border-bottom: 1px solid #e8e6e2;
}
.data-table tbody tr:hover { background: #f7f5f2; }
.tab-table tbody tr { border-left: 3px solid transparent; }
.data-table tbody td { font-size: 11px; padding: 13px 12px; vertical-align: middle; }
.data-table tbody tr:last-child { border-bottom: none; }
.td-subject { font-size: 11px; font-weight: 700; color: #111; margin-bottom: 2px; }
.td-project { font-size: 10px; color: #bbb; font-weight: 300; }
.td-group-badge { display: inline-block; font-size: 9px; font-weight: 500; letter-spacing: 0.08em; padding: 2px 6px; border: 0.5px solid #ccc; color: #555; text-transform: uppercase; }
.status-badge { display: inline-block; font-size: 9px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; padding: 3px 8px; border: 1px solid #111; color: #111; }
.status-badge.progress { background: #111; color: #fff; border-color: #111; }
.status-badge.wait { border-color: #b7770d; color: #b7770d; }
.status-badge.done { border-color: #bbb; color: #bbb; }
.td-priority { font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: 0.08em; }
.td-priority.high { color: #c0392b; font-weight: 500; }
.dday-wrap { position: relative; display: inline-block; }
@keyframes dday-blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.td-dday { font-size: 16px; font-weight: 700; cursor: default; }
.td-dday.red { color: #c0392b; animation: dday-blink 1.4s ease-in-out infinite; }
.td-dday.orange { color: #b7770d; }
.td-dday.orange-overdue { color: #b7770d; animation: dday-blink 1.4s ease-in-out infinite; }
.td-dday.gray { color: #bbb; }
.td-dday.closed { color: #000; }
.dday-tooltip { display: none; position: absolute; right: 0; top: -28px; background: #111; color: #fff; font-size: 10px; padding: 4px 8px; white-space: nowrap; pointer-events: none; z-index: 10; }
.dday-wrap:hover .dday-tooltip { display: block; }

/* ── Card Issue Row (01/02 카드용) ── */
.ci-row { padding: 10px 16px; border-bottom: 0.5px solid rgba(0,0,0,0.06); cursor: pointer; }
.ci-row:last-child { border-bottom: none; }
.ci-row:hover { background: rgba(0,0,0,0.02); }
.ci-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; flex-wrap: wrap; }
.ci-num { font-size: 10px; color: rgba(0,0,0,0.3); font-weight: 400; }
.ci-sep { font-size: 10px; color: rgba(0,0,0,0.15); }
.ci-assignee { font-size: 10px; color: rgba(0,0,0,0.45); }
.ci-group { font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: rgba(0,0,0,0.4); }
.ci-status { font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; padding: 1px 5px; background: rgba(0,0,0,0.06); color: rgba(0,0,0,0.45); }
.ci-bottom { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.ci-title { font-size: 11px; font-weight: 700; color: #111; flex: 1; line-height: 1.4; }
.ci-elapsed { font-size: 11px; font-weight: 700; white-space: nowrap; }
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
.assignee-row { padding: 8px 16px 7px; border-bottom: 1px solid #f0eeea; }
.assignee-row:last-child { border-bottom: none; }
.assignee-row-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 5px; }
.assignee-row-name { font-size: 11px; font-weight: 700; color: #111; }
.assignee-row-name span { font-weight: 300; color: #bbb; }
.assignee-row-counts { font-size: 11px; color: #111; font-weight: 700; }
.assignee-bar-track { height: 2px; background: #f0eeea; width: 100%; position: relative; margin-bottom: 4px; }
.assignee-bar-fill { height: 2px; position: absolute; top: 0; left: 0; }
.assignee-bar-normal { background: #111; }
.assignee-bar-critical { background: #c0392b; }
.assignee-row-meta { display: flex; justify-content: space-between; }
.assignee-meta-l { font-size: 10px; color: #bbb; font-weight: 300; letter-spacing: 0.3px; }
.assignee-meta-l.critical { color: #c0392b; font-weight: 400; }
.assignee-meta-r { font-size: 10px; color: #bbb; font-weight: 300; letter-spacing: 0.3px; }
.assignee-meta-r.urgent { color: #c0392b; font-weight: 400; }
.count-cell { font-size: 11px; text-align: right; }
.overdue-count { color: #8b1a1a; font-weight: 600; }
.zero-count { color: #ccc; }

/* ── Version card ── */
.tl-body { position: relative; padding: 12px 16px 12px 24px; margin-left: 8px; }
.tl-line { display: none; }
.tl-item { position: relative; margin-bottom: 20px; cursor: pointer; padding-left: 12px; }
.tl-content { transition: transform 150ms linear; transform-origin: left center; }
.tl-item:hover .tl-content { transform: scale(1.05); }
.tl-item:hover .tl-passive { background: #f5f3f3; }
.tl-item:hover .tl-active-card { background: #eae8e7; }
.tl-passive { display: flex; justify-content: space-between; align-items: flex-start; padding: 4px 0; transition: background 150ms linear; }
.tl-dot { position: absolute; left: -20px; top: 6px; width: 12px; height: 12px; border-radius: 50%; background: #f5f3f3; border: 1.5px solid #ccc; transform: translateX(-50%); display: flex; align-items: center; justify-content: center; z-index: 1; }
.tl-dot::before { content: ''; position: absolute; left: 50%; transform: translateX(-50%); width: 1px; background: #ddd; top: calc(100% + 6px); height: calc(var(--tl-item-h, 60px) - 18px); }
.tl-item:last-child .tl-dot::before { display: none; }
.tl-dot::after { content: ''; width: 4px; height: 4px; border-radius: 50%; background: #ccc; }
.tl-dot.active { border-color: #111; background: #f5f3f3; }
.tl-dot.active::after { background: #111; }
.tl-dot.done { border-color: #ccc; background: #f5f3f3; }
.tl-dot.done::after { background: #ccc; }
.tl-passive-name { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
.tl-passive-name.done { color: #ccc; text-decoration: line-through; }
.tl-passive-name.upcoming { color: #bbb; }
.tl-passive-sub { font-size: 9px; color: #ccc; margin-top: 2px; }
.tl-passive-dday { font-size: 10px; white-space: nowrap; margin-left: 10px; }
.tl-active-card { background: #e4e2e2; border-left: 3px solid #111; padding: 10px 12px; margin-top: 0; }
.tl-active-card.overdue { background: #e4e2e2; border-left-color: #c0392b; }
.tl-active-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }
.tl-active-name { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #111; }
.tl-active-badge { font-size: 9px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; padding: 2px 6px; }
.tl-active-badge.overdue { background: #000; color: #fff; }
.tl-active-badge.imminent { background: #c0392b; color: #fff; }
.tl-active-desc { font-size: 10px; color: #888; line-height: 1.6; }
.tl-active-dday { font-size: 11px; font-weight: 700; margin-top: 6px; }
.tl-active-dday.red { color: #c0392b; }
.ver-name { font-size: 11px; font-weight: 700; color: #111; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sort-icon { font-size: 9px; opacity: 0.6; }
.ver-dday { font-size: 11px; font-weight: 500; white-space: nowrap; flex-shrink: 0; }
.ver-dday.red { color: #c0392b; }
.ver-dday.blue { color: #0047A0; }
.ver-dday.muted { color: #ccc; font-weight: 300; }

/* ── AI Summary ── */
.ai-card-label {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #fff;
  background: #2471a3;
  padding: 4px 10px;
  display: inline-block;
}
.ai-text { font-size: 13px; color: #444; line-height: 1.8; white-space: pre-wrap; font-weight: 300; }
.ai-text.placeholder { color: #ccc; }

/* ── Bottom Tab Section ── */
.bottom-section {
  margin: 0 36px 32px;
}
.tab-bar {
  border: 1px solid #aaa;
  display: inline-flex;
  align-items: center;
  margin-bottom: 8px;
}
.tab-item {
  padding: 12px 20px;
  font-size: 9px;
  font-weight: 400;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #bbb;
  cursor: pointer;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 6px;
  border-right: 1px solid #e8e6e2;
  border-bottom: none;
  transition: color 0.15s;
  user-select: none;
}
.tab-item:last-child { border-right: none; }
.tab-item:hover { color: #555; }
.tab-item.active { color: #111; font-weight: 700; }
.tab-item[data-tab="imminent"],
.tab-item[data-tab="overdue"] { font-weight: 700; color: #111; }
.tab-badge { font-size: 9px; font-weight: 600; }
.tab-badge.red { color: #c0392b; }
.tab-badge.blue { color: #0047A0; }
.tab-filters {
  margin-top: 8px;
  border: 1px solid #aaa;
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  margin-bottom: 0;
}
.tab-filter-cell {
  padding: 10px 14px;
  border-right: 1px solid #aaa;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tab-filter-cell:last-child { border-right: none; }
.tab-filter-label {
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #bbb;
}
.tab-filter-cell input {
  font-size: 11px;
  border: none;
  outline: none;
  background: transparent;
  color: #111;
  padding: 0;
  cursor: pointer;
  width: 100%;
}
.tab-filter-cell select {
  font-size: 11px;
  border: none;
  outline: none;
  background: transparent url("data:image/svg+xml,%3Csvg width='8' height='5' viewBox='0 0 8 5' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l3 3 3-3' stroke='%23111' stroke-width='1.2' stroke-linecap='round'/%3E%3C/svg%3E") no-repeat right 2px center;
  color: #111;
  padding-right: 14px;
  -webkit-appearance: none;
  appearance: none;
  cursor: pointer;
  width: 100%;
}
.tab-filter-cell input::placeholder { color: #bbb; }
.tab-filter-cell input:focus,
.tab-filter-cell select:focus { color: #111; }
.tab-count { font-size: 10px; color: #bbb; padding: 6px 16px; text-align: right; letter-spacing: 0.5px; }
.tab-content {
  overflow-x: auto;
  min-height: 320px;
  margin-top: 16px;
}
.tab-table {
  opacity: 1;
  transition: opacity 0.2s ease;
}
.tab-table.fading {
  opacity: 0;
}
.tab-table tbody tr { cursor: pointer; }

/* ── Pagination ── */
.pagination { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-top: 0.5px solid #e8e6e2; margin-top: 0; }
.pg-left { display: flex; align-items: center; gap: 12px; }
.pg-info { font-size: 9px; font-weight: 500; letter-spacing: 0.15em; text-transform: uppercase; color: #555; }
.pg-info span { font-weight: 700 !important; text-transform: uppercase; letter-spacing: 0.15em; }
.pg-toggle { display: flex; align-items: center; }
.pg-toggle-btn { font-size: 9px; font-weight: 400; letter-spacing: 0.1em; color: #bbb; cursor: pointer; padding: 0 6px; border-right: 1px solid #ddd; }
.pg-toggle-btn:last-child { border-right: none; }
.pg-toggle-btn.active { font-weight: 700; color: #111; }
.pg-toggle-btn:hover { color: #111; }
.pg-controls { display: flex; align-items: center; gap: 2px; }
.pg-btn { font-size: 9px; font-weight: 500; letter-spacing: 0.15em; text-transform: uppercase; padding: 4px 8px; border: none; background: transparent; color: #555; cursor: pointer; }
.pg-btn:hover { color: #111; }
.pg-btn:disabled { color: #ccc; cursor: default; }
.pg-num { font-size: 9px; font-weight: 500; letter-spacing: 0.08em; padding: 4px 8px; color: #555; cursor: pointer; }
.pg-num:hover { color: #111; }
.pg-num.active { font-weight: 700; color: #111; border-bottom: 1.5px solid #111; }
.pg-ellipsis { font-size: 9px; color: #bbb; padding: 4px 2px; }

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
  .summary-strip { grid-template-columns: 1fr; }
  .summary-row2 { grid-template-columns: 1fr; }
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
  <a href="/" style="text-decoration: none; color: inherit;">
    <div class="navbar-brand">
      <div class="navbar-logo">VANTIX</div>
      <div class="navbar-tagline">AI Project Risk Intelligence</div>
    </div>
  </a>

  <div class="navbar-center">
    <div class="navbar-filter-item">
      <div class="navbar-filter-label">Project</div>
      <div class="navbar-filter-value">
        <select id="projectSelect" onchange="onProjectChange()">
          <option value="">전체 프로젝트</option>
        </select>
      </div>
    </div>
    <div class="navbar-divider"></div>
    <div class="navbar-filter-item">
      <div class="navbar-filter-label">Since</div>
      <div class="navbar-filter-value">
        <input type="date" id="filterDateInput" value="__DEFAULT_UPDATED_AFTER__" onchange="onDateChange()" title="조회 기준일" style="opacity:0;position:absolute;width:0;height:0;">
        <span id="filterDateDisplay" onclick="document.getElementById('filterDateInput').showPicker()" style="cursor:pointer;">__DEFAULT_UPDATED_AFTER__</span>
      </div>
    </div>
    <div class="navbar-divider"></div>
    <div class="navbar-live">
      <div class="navbar-filter-label">Status</div>
      <div class="navbar-live-val">
        <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#16a34a;"></span>
        <span id="cacheAge">—</span>&nbsp;·&nbsp;<span class="live-indicator" style="color:#16a34a;font-weight:700;">LIVE</span>
      </div>
    </div>
  </div>

  <div class="navbar-actions">
    <button class="btn btn-ghost" onclick="forceRefresh()" style="display:inline-flex;align-items:center;gap:5px;" title="강제 갱신">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M2 7a5 5 0 1 0 1-3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/><path d="M2 2v3h3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="btn-text-mobile">강제 갱신</span>
    </button>
    <button class="btn btn-ghost" onclick="loadData()" style="display:inline-flex;align-items:center;gap:5px;" title="새로 고침">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7 2a5 5 0 1 1 0 10A5 5 0 0 1 7 2z" stroke="currentColor" stroke-width="1.1"/><path d="M7 5v2.5l1.5 1.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>
      <span class="btn-text-mobile">새로 고침</span>
    </button>
    <div class="navbar-divider"></div>
    <button class="btn btn-primary-outline" onclick="openReport()" style="display:inline-flex;align-items:center;gap:5px;">
      <svg width="12" height="12" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7 2v7M4.5 6.5L7 9l2.5-2.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 10v1a1 1 0 001 1h8a1 1 0 001-1v-1" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg>
      <span class="btn-text-mobile">리포트</span>
    </button>
    <button class="btn-icon" onclick="openSettingsModal()" title="설정">
      <img src="/setupicon.png" alt="설정">
    </button>
  </div>
</nav>

<!-- Summary Strip -->
<div class="summary-strip">

  <!-- 프로젝트 위험도 (3) -->
  <div class="sum-card" id="card-risk" style="background:#ffffff;border-right:1px solid rgba(207,196,197,0.3);">
    <div class="sum-label">CURRENT RISK INDEX / 위험 지수</div>
    <div style="display:flex;align-items:flex-end;gap:16px;margin-bottom:10px;">
      <div id="risk-score-wrap" style="position:relative;display:inline-block;cursor:help;">
        <span id="risk-score-display" style="font-size:120px;font-weight:800;line-height:1;color:#ccc;letter-spacing:-2px;">—</span>
        <div id="risk-tooltip" style="display:none;position:absolute;top:calc(100% + 8px);left:0;background:#111;color:#fff;font-size:11px;line-height:1.8;padding:14px 16px;white-space:nowrap;z-index:999;pointer-events:none;border-top:2px solid #e74c3c;">
          <div style="font-weight:700;font-size:10px;letter-spacing:0.12em;color:#e74c3c;margin-bottom:8px;">RISK SCORE 산정 기준 · /100 환산</div>
          <div style="display:flex;justify-content:space-between;gap:24px;margin-bottom:4px;">
            <span style="color:#e74c3c;">마감 초과 비율 × 60</span>
            <span style="color:#666;font-size:10px;">overdue / total × 60</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:24px;margin-bottom:4px;">
            <span style="color:#fb923c;">마감 임박 비율 × 30</span>
            <span style="color:#666;font-size:10px;">urgent / total × 30</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:24px;margin-bottom:10px;">
            <span style="color:#aaa;">진행 대기 비율 × 10</span>
            <span style="color:#666;font-size:10px;">pending / total × 10</span>
          </div>
          <div style="height:1px;background:#333;margin-bottom:8px;"></div>
          <div style="color:#888;font-size:10px;">최대 원점수 60 → 100점 환산 표시</div>
          <div style="color:#555;font-size:10px;">예) 원점수 30 → 표시 점수 50</div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;padding-bottom:8px;">
        <div style="display:flex;align-items:center;gap:6px;">
          <span id="risk-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#ccc;flex-shrink:0;"></span>
          <span class="sum-value" id="risk-level-text" style="font-size:13px;color:#ccc;font-weight:700;letter-spacing:0.08em;">—</span>
        </div>
      </div>
    </div>
    <div style="height:3px;background:#e5e5e5;margin-bottom:6px;">
      <div id="risk-gauge-fill" style="height:100%;width:0%;background:#ccc;transition:width 0.6s ease;"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
      <span style="font-size:8px;color:#16a34a;letter-spacing:0.1em;">LOW</span>
      <span style="font-size:8px;color:#fb923c;letter-spacing:0.1em;">HIGH</span>
      <span style="font-size:8px;color:#c0392b;letter-spacing:0.1em;">CRITICAL</span>
    </div>
    <div class="risk-sub" id="risk-sub">—</div>
    <div id="risk-gauge-wrap" style="position:relative;"></div>
    <div style="height:0.5px;background:#e8e8e8;margin:8px 0;"></div>
    <div id="risk-ai-comment" style="font-size:11px;color:#111;font-weight:600;line-height:1.6;display:flex;gap:5px;align-items:flex-start;">
      <span style="color:#e74c3c;flex-shrink:0;">✦</span>
      <span id="risk-ai-comment-text">분석 중...</span>
    </div>
    <div class="sum-hint" style="margin-top:6px;">위험경보 ↓</div>
  </div>

  <!-- Risk Score Trends 막대 차트 -->
  <div class="sum-card gray wide" style="background:#f5f3f3;padding:0;">
    <div class="ai-chart-card" style="background:transparent;border:none;">
      <div class="ai-chart-header">
        <div class="ai-chart-title">RISK SCORE TRENDS / 위험 지수 추이</div>
        <div class="ai-chart-header-right">
          <div class="ai-legend">
            <div class="ai-legend-item"><div class="ai-legend-dot" style="background:#ccc;"></div> LOW</div>
            <div class="ai-legend-item"><div class="ai-legend-dot" style="background:#333;"></div> AVG</div>
            <div class="ai-legend-item"><div class="ai-legend-dot" style="background:#c0392b;"></div> CRITICAL</div>
          </div>
          <button class="ai-gear-btn" id="riskGearBtn" onclick="toggleRiskSettings()">&#9881;</button>
        </div>
      </div>
      <div class="ai-settings-panel" id="riskSettingsPanel">
        <div class="ai-settings-label">Chart Settings</div>
        <div class="ai-settings-row">
          <label>표시 주수</label>
          <div class="ai-dow-btns" id="weeksBtns">
            <div class="ai-dow-btn" onclick="setWeeks(this,4)">4주</div>
            <div class="ai-dow-btn" onclick="setWeeks(this,8)">8주</div>
            <div class="ai-dow-btn" onclick="setWeeks(this,12)">12주</div>
          </div>
        </div>
        <button class="ai-settings-apply" onclick="applyRiskSettings()">Apply</button>
      </div>
      <div class="ai-chart-sub">Weekly volatility · updated every Friday</div>
      <div class="ai-chart-area">
        <div class="ai-gridlines">
          <div class="ai-gridline"></div>
          <div class="ai-gridline"></div>
          <div class="ai-gridline"></div>
          <div class="ai-gridline"></div>
          <div class="ai-gridline"></div>
        </div>
        <div class="ai-bars" id="riskChartBars">
          <div style="padding:20px;text-align:center;font-size:11px;color:#ccc;width:100%;">로딩 중...</div>
        </div>
      </div>
      <div class="ai-bar-labels" id="riskChartLabels"></div>
    </div>
  </div>

</div>

<!-- Summary Row 2 -->
<div class="summary-row2">

  <!-- 전체 이슈 -->
  <div class="sum-card" id="card-total" style="background:#ffffff;">
    <div class="sum-label">Total Issues</div>
    <div class="sum-value" id="val-total">—</div>
    <div class="sum-spark">
      <svg id="spark-total" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-total">— No Change</span>
    </div>
  </div>

  <!-- 오픈 이슈 -->
  <div class="sum-card clickable" id="card-open" onclick="goToTab('assignee')" style="background:#f5f3f3 !important;">
    <div class="sum-label">Open Issues</div>
    <div class="sum-value amber" id="val-open">—</div>
    <div class="sum-spark">
      <svg id="spark-open" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-open">—</span>
    </div>
    <div class="sum-hint">담당자별 현황 ↓</div>
  </div>

  <!-- 마감 임박 (신규) -->
  <div class="sum-card clickable" id="card-imminent" onclick="goToTab('imminent')" style="background:#ffffff !important;">
    <div class="sum-label">Imminent</div>
    <div class="sum-value blue" id="val-imminent">—</div>
    <div class="sum-spark">
      <span class="sum-delta" id="delta-imminent" style="color:#2980b9;">D-7 이내</span>
    </div>
    <div class="sum-hint">마감 임박 이슈 ↓</div>
  </div>

  <!-- 마감 초과 -->
  <div class="sum-card clickable" id="card-overdue" onclick="goToTab('overdue')" style="background:#e4e2e2 !important;">
    <div class="sum-label">Overdue</div>
    <div class="sum-value red" id="val-overdue">—</div>
    <div class="sum-spark">
      <svg id="spark-overdue" width="60" height="18" viewBox="0 0 60 18"></svg>
      <span class="sum-delta" id="delta-overdue">—</span>
    </div>
    <div class="sum-hint">Overdue Issues ↓</div>
  </div>

</div>

<div class="ai-analysis-section" id="sec-ai-summary">
  <!-- Risk-Heavy Members (왼쪽 컬럼) -->
  <div class="risk-members-section" style="background:#fff;">
    <div class="risk-members-header" style="padding:14px 18px 12px;">
      <div>
        <div class="risk-members-title">Risk-Heavy Members / 위험 노출 담당자</div>
      </div>
      <div onclick="goToTab('assignee')" style="font-size:10px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:rgba(0,0,0,0.4);cursor:pointer;">View All ↓</div>
    </div>
    <div id="risk-members-body"></div>
  </div>
  <div class="ai-card-dark">
    <div>
      <div class="ai-label-mono">AI Analysis</div>
      <div class="ai-card-headline" id="aiHeadline">분석 중...</div>
      <div class="ai-card-body-text" id="aiSummaryText">데이터를 불러오는 중입니다.</div>
    </div>
    <button class="ai-refresh-btn" onclick="generateAiSummary()">&#8635; &nbsp; REFRESH ANALYSIS</button>
  </div>
</div>

<!-- Main Content: Masonry -->
<div class="main-content">
  <div class="masonry-grid">

    <!-- Card 03: 그룹 현황 -->
    <div class="card" id="sec-group" style="background:#ffffff;">
      <div class="card-header">
        <span>Group Status · 그룹 현황</span>
        <span class="card-header-sub" id="group-header-sub">—</span>
      </div>
      <div class="card-body" style="padding:0;">
        <div id="group-col-header" style="display:grid;grid-template-columns:100px 1fr 52px 72px;padding:7px 16px;background:#f5f3f3;border-bottom:1px solid #e4e2e2;gap:10px;">
          <div style="font-size:9px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:#bbb;">Group</div>
          <div style="font-size:9px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:#bbb;">Overdue Ratio</div>
          <div style="font-size:9px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:#bbb;text-align:right;">OVR</div>
          <div style="font-size:9px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:#bbb;text-align:right;">Risk</div>
        </div>
        <div id="group-tbody"></div>
        <div id="group-hidden-msg" style="display:none;padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">프로젝트를 선택하면 그룹 현황이 표시됩니다.</div>
      </div>
    </div>

    <!-- Card 04: 버전/마일스톤 -->
    <div class="card" id="sec-version">
      <div class="card-header">
        <span>Milestones · 버전 / 마일스톤</span>
        <span class="card-header-sub" id="version-project-name">—</span>
      </div>
      <div class="card-body" id="version-body" style="overflow-y:auto; flex:1; min-height:0; max-height:308px;">
        <div style="padding:16px;text-align:center;font-size:11px;color:#ccc;">로딩 중...</div>
      </div>
    </div>

  </div>
</div>

<!-- Bottom Tab Section -->
<div class="bottom-section">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:8px;">
    <div class="tab-bar" style="margin-bottom:0;">
      <div class="tab-item active" data-tab="imminent" onclick="switchTab('imminent')">
        마감 임박 <span class="tab-badge blue" id="badge-imminent">0</span>
      </div>
      <div class="tab-item" data-tab="overdue" onclick="switchTab('overdue')">
        마감 초과 <span class="tab-badge red" id="badge-overdue">0</span>
      </div>
      <div class="tab-item" data-tab="assignee" onclick="switchTab('assignee')">담당자별</div>
      <div class="tab-item" data-tab="all" onclick="switchTab('all')">전체 이슈</div>
    </div>
    <span id="tabCount" style="font-size:13px;font-weight:700;color:#111;white-space:nowrap;">0건</span>
  </div>
  <div class="tab-filters">
    <div class="tab-filter-cell">
      <span class="tab-filter-label">Keyword 검색</span>
      <input type="text" id="tabKeywordSearch" placeholder="ISSUE NAME OR ID" oninput="renderTab()">
    </div>
    <div class="tab-filter-cell">
      <span class="tab-filter-label">Priority 우선순위</span>
      <select id="tabPriorityFilter" onchange="renderTab()">
        <option value="">ALL PRIORITIES</option>
        <option value="High">HIGH</option>
        <option value="Normal">NORMAL</option>
        <option value="Low">LOW</option>
      </select>
    </div>
    <div class="tab-filter-cell">
      <span class="tab-filter-label">Assignee 담당자</span>
      <select id="tabAssigneeFilter" onchange="renderTab()">
        <option value="">EVERYONE</option>
      </select>
    </div>
    <div class="tab-filter-cell">
      <span class="tab-filter-label">Status 상태</span>
      <select id="tabStatusFilter" onchange="renderTab()">
        <option value="">OPEN ISSUES</option>
        <option value="all">ALL</option>
        <option value="closed">CLOSED</option>
      </select>
    </div>
  </div>
  <div class="tab-content">
    <div id="tab-card-list"></div>
    <table class="data-table tab-table">
      <thead>
        <tr>
          <th data-sort="id" onclick="setSort('id')" style="cursor:pointer;color:#bbb;">ID<span class="sort-icon"> ⇅</span></th>
          <th data-sort="subject" onclick="setSort('subject')" style="cursor:pointer;color:#bbb;">SUBJECT 이슈 제목<span class="sort-icon"> ⇅</span></th>
          <th data-sort="dept" onclick="setSort('dept')" style="cursor:pointer;color:#bbb;">그룹<span class="sort-icon"> ⇅</span></th>
          <th data-sort="assignee" onclick="setSort('assignee')" style="cursor:pointer;color:#bbb;">담당자<span class="sort-icon"> ⇅</span></th>
          <th data-sort="status" onclick="setSort('status')" style="cursor:pointer;color:#bbb;">상태<span class="sort-icon"> ⇅</span></th>
          <th data-sort="priority" onclick="setSort('priority')" style="cursor:pointer;color:#bbb;">PRIORITY<span class="sort-icon"> ⇅</span></th>
          <th data-sort="due_date" onclick="setSort('due_date')" style="cursor:pointer;color:#000;text-align:right;">D-DAY<span class="sort-icon"> ▲</span></th>
        </tr>
      </thead>
      <tbody id="tab-tbody">
        <tr class="loading-row"><td colspan="7"><span class="loading-spinner"></span></td></tr>
      </tbody>
    </table>
  </div>
  <div class="pagination">
    <div class="pg-left">
      <span class="pg-info" id="pgInfo">Showing 1–10 of 0 entries</span>
      <div class="pg-toggle">
        <span class="pg-toggle-btn active" onclick="setPerPage(10, this)">10</span>
        <span class="pg-toggle-btn" onclick="setPerPage(50, this)">50</span>
      </div>
    </div>
    <div class="pg-controls" id="pgControls"></div>
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
      <div style="margin-top:12px;background:#0047A0;padding:10px 14px;">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="7" width="10" height="8" rx="1.5" stroke="#fff" stroke-width="1.2"/><path d="M5 7V5a3 3 0 0 1 6 0v2" stroke="#fff" stroke-width="1.2" stroke-linecap="round"/></svg>
          <span style="font-size:11px;font-weight:700;color:#fff;letter-spacing:0.08em;">보안 안내</span>
        </div>
        <div style="font-size:11px;color:#fff;opacity:0.85;">입력된 정보는 Vantix 서버로 전송되지 않으며, 로컬 브라우저에만 저장됩니다.</div>
      </div>
    </div>
  </div>
</div>

<!-- No Connection Overlay -->
<div class="no-conn-overlay" id="noConnOverlay">
  <div class="no-conn-title">Redmine 연결이 필요합니다</div>
  <button class="btn btn-black" onclick="openSettingsModal()">설정하기</button>
</div>

<div class="modal-overlay" id="confirmModal" onclick="closeConfirmModal(event)">
  <div class="modal-box" style="max-width:420px;">
    <div class="modal-header">
      <div class="modal-title" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:3px;color:#111;">REDMINE 로드맵 페이지 이동</div>
      <button class="modal-close" onclick="closeConfirmModal()">&#x2715;</button>
    </div>
    <div class="modal-body">
      <p style="font-size:12px;color:#555;line-height:1.7;margin-bottom:16px;">레드마인 로드맵 상세페이지로 이동하여 전체 현황을 확인합니다.<br><br><span id="confirmUrl" style="color:#0047A0;font-size:11px;word-break:break-all;"></span></p>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-black" style="flex:1;justify-content:center;" onclick="confirmRedmineGo()">확인</button>
        <button class="btn" style="flex:1;justify-content:center;border:1px solid #e4e2e2;" onclick="closeConfirmModal()">취소</button>
      </div>
    </div>
  </div>
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
    _riskWeeks = parseInt(localStorage.getItem('riskWeeks') || '12');
    document.querySelectorAll('#weeksBtns .ai-dow-btn').forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('onclick').includes(',' + _riskWeeks + ')'));
    });
    loadRiskHistory();
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
  renderGroupStatus(currentProjectId);
  renderTab();
  updateCacheAge();
  populateTabFilters();
}

async function renderGroupStatus(projectId) {
  var tbody = document.getElementById('group-tbody');
  var hiddenMsg = document.getElementById('group-hidden-msg');
  var headerSub = document.getElementById('group-header-sub');
  var colHeader = document.getElementById('group-col-header');

  if (!projectId || projectId === '') {
    if (tbody) tbody.innerHTML = '';
    if (hiddenMsg) hiddenMsg.style.display = 'block';
    if (colHeader) colHeader.style.display = 'none';
    return;
  }
  if (hiddenMsg) hiddenMsg.style.display = 'none';
  if (colHeader) colHeader.style.display = 'grid';

  try {
    var res = await fetch('/api/groups?project_id=' + encodeURIComponent(projectId));
    var data = await res.json();
    var groups = data.groups || [];
    if (headerSub) headerSub.textContent = groups.length + ' groups';
    if (!tbody) return;
    if (groups.length === 0) {
      tbody.innerHTML = '<div style="padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">그룹 정보 없음</div>';
      return;
    }

    tbody.innerHTML = groups.map(function(g, idx) {
      var bg = idx % 2 === 1 ? '#f5f3f3' : '#fff';
      var totalIssues = g.total_issues || 1;
      var overdueRatio = Math.min(100, Math.round((g.overdue_now / totalIssues) * 100));
      var barColor = g.risk === 'Critical' ? '#c0392b' : g.risk === 'High' ? '#000' : '#bbb';
      var riskBg = g.risk === 'Critical' ? '#c0392b' : g.risk === 'High' ? '#000' : '#e4e2e2';
      var riskColor = g.risk === 'Stable' ? '#666' : '#fff';
      var overdueColor = g.overdue_now > 0 ? '#c0392b' : '#000';

      return '<div style="display:grid;grid-template-columns:100px 1fr 52px 72px;padding:11px 16px;border-bottom:1px solid #eae8e7;gap:10px;align-items:center;background:' + bg + ';">' +
        '<div>' +
          '<div style="font-size:12px;font-weight:700;color:#000;">' + g.name + '</div>' +
          '<div style="font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:0.07em;margin-top:1px;">' + g.user_count + '명</div>' +
        '</div>' +
        '<div>' +
          '<div style="height:6px;background:#eae8e7;border-radius:3px;overflow:hidden;">' +
            '<div style="height:6px;background:' + barColor + ';width:' + overdueRatio + '%;border-radius:3px;transition:width 0.3s;"></div>' +
          '</div>' +
          '<div style="font-size:9px;color:#bbb;margin-top:3px;">' + overdueRatio + '% overdue</div>' +
        '</div>' +
        '<div style="font-size:12px;font-weight:600;text-align:right;color:' + overdueColor + ';">' + g.overdue_now + '</div>' +
        '<div style="text-align:right;"><span style="font-size:9px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;padding:3px 7px;background:' + riskBg + ';color:' + riskColor + ';">' + g.risk + '</span></div>' +
      '</div>';
    }).join('');
  } catch(e) {
    if (tbody) tbody.innerHTML = '<div style="padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">그룹 로드 실패</div>';
  }
}

// ============================================================
// Summary Cards
// ============================================================
function renderSummaryCards() {
  var d = allData;
  var trend = d.trend_7days || {};

  // Risk-Heavy Members Top3
  renderRiskMembers(allData);

  // Card 1 — 전체 이슈
  var valTotalEl = document.getElementById('val-total');
  if (valTotalEl) valTotalEl.textContent = (d.total_issues !== undefined && d.total_issues !== null) ? d.total_issues : '—';
  renderSparkline('spark-total', trend.open || [], '#999');
  var deltaTotalEl = document.getElementById('delta-total');
  if (deltaTotalEl) deltaTotalEl.textContent = '— No Change';

  // Card 2 — 오픈 이슈
  var valOpenEl = document.getElementById('val-open');
  if (valOpenEl) valOpenEl.textContent = (d.open_issues !== undefined && d.open_issues !== null) ? d.open_issues : '—';
  renderSparkline('spark-open', trend.open || [], '#b7770d');
  var dOpen = trend.delta_open || 0;
  var deltaOpenEl = document.getElementById('delta-open');
  if (deltaOpenEl) {
    deltaOpenEl.textContent = dOpen === 0 ? '— No Change' : (dOpen > 0 ? '+' + dOpen + ' Increase' : dOpen + ' Decrease');
    deltaOpenEl.className = 'sum-delta ' + (dOpen > 0 ? 'up' : dOpen < 0 ? 'down' : '');
  }

  // Card 2-5 — 마감 임박
  var valImminentEl = document.getElementById('val-imminent');
  if (valImminentEl) valImminentEl.textContent = (d.imminent_count !== undefined) ? d.imminent_count : (d.imminent_issues || []).length;

  // Card 3 — 마감 초과
  var valOverdueEl = document.getElementById('val-overdue');
  if (valOverdueEl) valOverdueEl.textContent = (d.overdue !== undefined && d.overdue !== null) ? d.overdue : '—';
  renderSparkline('spark-overdue', trend.overdue || [], '#c0392b');
  var dOverdue = trend.delta_overdue || 0;
  var deltaOverdueEl = document.getElementById('delta-overdue');
  if (deltaOverdueEl) {
    deltaOverdueEl.textContent = dOverdue === 0 ? '— No Change' : (dOverdue > 0 ? '+' + dOverdue + ' Increase' : dOverdue + ' Decrease');
    deltaOverdueEl.className = 'sum-delta ' + (dOverdue > 0 ? 'up' : dOverdue < 0 ? 'down' : '');
  }

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
    if (dotEl) dotEl.style.background = rc.dot;
    if (levelEl) { levelEl.textContent = topRisk.risk_level; levelEl.style.color = rc.text; }
    var scoreDisplayEl = document.getElementById('risk-score-display');
    if (scoreDisplayEl) {
      var normalizedScore = Math.min(Math.round(topRisk.risk_score * 100 / 60), 100);
      scoreDisplayEl.textContent = normalizedScore;
      scoreDisplayEl.style.color = rc.text;
    }
    if (subEl) subEl.textContent = '';
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
    // val-risk, val-risk-detail 업데이트
    var valRiskEl = document.getElementById('val-risk');
    if (valRiskEl) valRiskEl.textContent = topRisk.risk_level ? topRisk.risk_level.toUpperCase() : '—';
    var valRiskDetailEl = document.getElementById('val-risk-detail');
    if (valRiskDetailEl) valRiskDetailEl.textContent = topRisk.name || '—';
  } else {
    if (dotEl) dotEl.style.background = '#34d399';
    if (levelEl) { levelEl.textContent = 'Low'; levelEl.style.color = '#16a34a'; }
    var scoreDisplayEl = document.getElementById('risk-score-display');
    if (scoreDisplayEl) { scoreDisplayEl.textContent = '0'; scoreDisplayEl.style.color = '#16a34a'; }
    if (subEl) subEl.textContent = '위험 프로젝트 없음';
    var gaugeFill = document.getElementById('risk-gauge-fill');
    if (gaugeFill) gaugeFill.style.width = '0%';
    var commentEl = document.getElementById('risk-ai-comment-text');
    if (commentEl) commentEl.textContent = '위험 프로젝트 없음';
    var valRiskEl = document.getElementById('val-risk');
    if (valRiskEl) valRiskEl.textContent = 'LOW';
    var valRiskDetailEl = document.getElementById('val-risk-detail');
    if (valRiskDetailEl) valRiskDetailEl.textContent = '위험 프로젝트 없음';
  }
  var scoreWrap = document.getElementById('risk-score-wrap');
  var tooltip = document.getElementById('risk-tooltip');
  if (scoreWrap && tooltip) {
    scoreWrap.addEventListener('mouseenter', function() { tooltip.style.display = 'block'; });
    scoreWrap.addEventListener('mouseleave', function() { tooltip.style.display = 'none'; });
  }

  // Update badges
  var badgeOverdueEl = document.getElementById('badge-overdue');
  if (badgeOverdueEl) badgeOverdueEl.textContent = d.overdue || 0;
  var badgeImminentEl = document.getElementById('badge-imminent');
  if (badgeImminentEl) badgeImminentEl.textContent = (d.imminent_issues || []).length;
}

// ============================================================
// Imminent Card
// ============================================================
function renderImminentCard() {
  var issues = allData.imminent_issues || [];
  var tbody = document.getElementById('imminent-tbody');
  var imminentHeaderSub = document.getElementById('imminent-header-sub');
  if (imminentHeaderSub) imminentHeaderSub.textContent = 'D-10 이내 · ' + issues.length + '건';

  var container = document.getElementById('imminent-tbody');
  if (!container) return;
  if (issues.length === 0) {
    container.innerHTML = '<div style="padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">마감 임박 이슈 없음</div>';
    return;
  }

  container.innerHTML = issues.map(function(i) {
    var color = ddayColor(i.due_date);
    var label = ddayLabel(i.due_date);
    var assigneeShort = i.assignee_short || i.assignee || '';
    var dept = i.dept || '';
    return '<div class="ci-row" onclick="openIssue(' + i.id + ')">' +
      '<div class="ci-meta">' +
        '<span class="ci-num">#' + i.id + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-assignee">' + escHtml(assigneeShort) + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-group">' + escHtml(dept) + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-status">' + escHtml(i.status) + '</span>' +
      '</div>' +
      '<div class="ci-bottom">' +
        '<span class="ci-title">' + escHtml(i.subject) + '</span>' +
        '<span class="ci-elapsed" style="color:' + color + ';">' + label + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

// ============================================================
function getInitials(name) {
  name = name.trim();
  if (!name) return '?';
  if (/[\uAC00-\uD7A3\u4E00-\u9FFF\u3040-\u30FF]/.test(name)) return name[0];
  var words = name.trim().split(/\s+/).filter(function(w) { return /[a-zA-Z]/.test(w); });
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  var letters = name.replace(/[^a-zA-Z]/g, '');
  return letters ? letters[0].toUpperCase() : name[0];
}

function renderRiskMembers(allData) {
  var container = document.getElementById('risk-members-body');
  if (!container) return;
  var usersData = allData.users_data || {};
  var todayStr = new Date().toISOString().slice(0,10);
  var rows = [];
  for (var uname in usersData) {
    var ud = usersData[uname];
    var issues = ud.issues || [];
    var open = issues.filter(function(i) { return !CLOSED_SET_JS.has(i.status); });
    var overdueIssues = open.filter(function(i) {
      return i.due_date && i.due_date < todayStr && !HOLD_SET_JS.has(i.status);
    });
    if (overdueIssues.length === 0) continue;
    var overdueRatio = open.length > 0 ? Math.round(overdueIssues.length / open.length * 100) : 0;
    var level = overdueRatio >= 60 ? 'critical' : overdueRatio >= 30 ? 'high' : 'medium';
    rows.push({
      uname: uname, shortN: getShortName(uname), dept: getDept(uname),
      open: open.length, overdue: overdueIssues.length,
      overdueRatio: overdueRatio, level: level
    });
  }
  rows.sort(function(a, b) { return b.overdueRatio - a.overdueRatio || b.open - a.open; });
  var top3 = rows.slice(0, 3);
  if (top3.length === 0) {
    container.innerHTML = '<div style="padding:16px 18px;font-size:11px;color:rgba(0,0,0,0.4);text-transform:uppercase;letter-spacing:0.1em;">초과 이슈 없음</div>';
    return;
  }
  var LEVEL_COLOR       = { critical: '#c0392b', high: '#b45309', medium: '#555' };
  var LEVEL_LABEL       = { critical: 'CRITICAL', high: 'HIGH', medium: 'MEDIUM' };
  var LEVEL_BADGE_BG    = { critical: '#c0392b', high: '#000', medium: '#000' };
  var LEVEL_BADGE_COLOR = { critical: '#fff',     high: '#fff', medium: '#fff' };
  var maxOpen = Math.max.apply(null, top3.map(function(r) { return r.open; })) || 1;
  container.innerHTML = top3.map(function(r) {
    var color      = LEVEL_COLOR[r.level]       || '#555';
    var label      = LEVEL_LABEL[r.level]       || 'MEDIUM';
    var badgeBg    = LEVEL_BADGE_BG[r.level]   || '#000';
    var badgeFg    = LEVEL_BADGE_COLOR[r.level] || '#fff';
    var initials   = getInitials(r.shortN);
    var odValColor = r.overdueRatio >= 60 ? '#c0392b' : r.overdueRatio >= 30 ? '#b45309' : '#2e7d32';
    var openValColor = r.open > 5 ? '#b45309' : '#2e7d32';
    var openPct    = Math.round(r.open / maxOpen * 100);
    var barColor   = r.level === 'critical' ? '#c0392b' : r.level === 'high' ? '#b45309' : '#000';
    return (
      '<div class="risk-member-row" data-name="' + escHtml(r.shortN) + '" onclick="selectMemberFromCard(this)">' +
        '<div class="risk-member-top">' +
          '<div class="risk-member-avatar" style="background:' + badgeBg + ';color:' + badgeFg + ';">' + escHtml(initials) + '</div>' +
          '<div class="risk-member-name-block">' +
            '<div class="risk-member-name">' + escHtml(r.shortN) + '</div>' +
            '<div class="risk-member-dept">' + escHtml(r.dept) + '</div>' +
          '</div>' +
          '<div class="risk-member-bar-section">' +
            '<div class="risk-member-bar-row">' +
              '<span class="risk-member-bar-lbl">OVR</span>' +
              '<div class="risk-member-bar-track"><div class="risk-member-bar-fill" style="width:' + r.overdueRatio + '%;background:' + barColor + ';"></div></div>' +
              '<span class="risk-member-bar-val" style="color:' + odValColor + ';">' + r.overdueRatio + '%</span>' +
            '</div>' +
            '<div class="risk-member-bar-row">' +
              '<span class="risk-member-bar-lbl">OPN</span>' +
              '<div class="risk-member-bar-track"><div class="risk-member-bar-fill" style="width:' + openPct + '%;background:#555;"></div></div>' +
              '<span class="risk-member-bar-val" style="color:var(--color-text-primary);">' + r.open + '</span>' +
            '</div>' +
          '</div>' +
          '<span class="risk-member-badge" style="background:' + badgeBg + ';color:' + badgeFg + ';width:64px;text-align:center;flex-shrink:0;margin:0 auto;">' + label + '</span>' +
        '</div>' +
      '</div>'
    );
  }).join('');
}

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
    rows.push({ name: shortN, group: dept, total: total, open: open, overdue: overdue });
  }

  renderAssigneeRows(rows);
}

function renderAssigneeRows(data) {
  const container = document.getElementById('assignee-tbody');
  if (!container) return;
  if (!data || data.length === 0) {
    container.innerHTML = '<div style="padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">담당자 데이터 없음</div>';
    return;
  }

  const sorted = [...data].sort((a, b) => (b.overdue || 0) - (a.overdue || 0));
  const maxOverdue = sorted[0]?.overdue || 1;

  container.innerHTML = sorted.map(row => {
    const overdue = row.overdue || 0;
    const total   = row.total   || 0;
    const open    = row.open    || 0;
    const group   = row.group ? `<span>(${row.group})</span>` : '';
    const isCritical = overdue >= 3;
    const barPct  = Math.min(Math.round((overdue / maxOverdue) * 100), 100);
    const barClass = isCritical ? 'assignee-bar-critical' : 'assignee-bar-normal';

    const metaL = isCritical
      ? '<span class="assignee-meta-l critical">마감 초과 위험</span>'
      : `<span class="assignee-meta-l">진행 ${String(open).padStart(2,'0')}건</span>`;
    const metaR = isCritical
      ? '<span class="assignee-meta-r urgent">즉시 조치 필요</span>'
      : `<span class="assignee-meta-r">${overdue >= 1 ? '초과 있음' : '정상'}</span>`;
    const overdueHtml = overdue > 0
      ? `<span style="color:#c0392b;font-weight:500;">초과 ${overdue}</span>`
      : `<span style="color:#999;">초과 0</span>`;

    return `
      <div class="assignee-row">
        <div class="assignee-row-top">
          <span class="assignee-row-name">${escHtml(row.name)} ${group}</span>
          <span class="assignee-row-counts">전체 ${total} · 오픈 ${open} · ${overdueHtml}</span>
        </div>
        <div class="assignee-bar-track">
          <div class="assignee-bar-fill ${barClass}" style="width:${barPct}%;"></div>
        </div>
        <div class="assignee-row-meta">${metaL}${metaR}</div>
      </div>`;
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

  var overdueHeaderSub = document.getElementById('overdue-header-sub');
  if (overdueHeaderSub) overdueHeaderSub.textContent = overdues.length + '건';

  var container = document.getElementById('overdue-tbody');
  if (!container) return;
  if (overdues.length === 0) {
    container.innerHTML = '<div style="padding:20px 16px;font-size:11px;color:#ccc;text-align:center;">마감 초과 이슈 없음</div>';
    return;
  }
  container.innerHTML = overdues.map(function(i) {
    var elapsedAbs = Math.abs(i.elapsed);
    var color = elapsedAbs > 7 ? '#c0392b' : '#b7770d';
    var label = 'D+' + elapsedAbs;
    return '<div class="ci-row" onclick="openIssue(' + i.id + ')">' +
      '<div class="ci-meta">' +
        '<span class="ci-num">#' + i.id + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-assignee">' + escHtml(i.assigneeShort) + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-group">' + escHtml(i.dept) + '</span>' +
        '<span class="ci-sep">·</span>' +
        '<span class="ci-status">' + escHtml(i.status) + '</span>' +
      '</div>' +
      '<div class="ci-bottom">' +
        '<span class="ci-title">' + escHtml(i.subject) + '</span>' +
        '<span class="ci-elapsed" style="color:' + color + ';">' + label + '</span>' +
      '</div>' +
    '</div>';
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
    var html = '<div class="tl-body"><div class="tl-line"></div>';
    versions.forEach(function(v, idx) {
      var pct = v.done_pct || 0;
      var hasDate = !!v.due_date;
      var raw = hasDate ? ddayLabel(v.due_date) : null;
      var isOverdue = raw && raw.toString().startsWith('D+');
      var dNum = raw ? parseInt(raw.toString().replace(/[^0-9]/g, '')) : null;
      var isImminent = !isOverdue && dNum !== null && dNum <= 10;
      var isDone = pct === 100 && !isOverdue && !isImminent;
      var isActive = isOverdue || isImminent;

      html += '<div class="tl-item" onclick="goToRedmineVersion(this)" data-pid="' + currentProjectId + '" data-vid="' + v.id + '">';

      if (isActive) {
        var cardCls = isOverdue ? 'tl-active-card overdue' : 'tl-active-card';
        var badgeCls = isOverdue ? 'tl-active-badge overdue' : 'tl-active-badge imminent';
        var badgeTxt = isOverdue ? 'OVERDUE' : 'IMMINENT';
        var ddayTxt = isOverdue ? 'D+' + dNum + ' 마감 초과' : 'D-' + dNum + ' 마감 임박';
        var pctColor = isOverdue ? '#c0392b' : '#b7770d';
        var desc = '완료율 <span style="font-weight:700;color:' + pctColor + ';">' + pct + '%</span> · ' + (isOverdue ? '마감일 초과 상태. 잔여 이슈 확인 필요.' : '마감까지 ' + dNum + '일 남음.');
        html += '<div class="tl-dot active"></div>';
        html += '<div class="tl-content"><div class="' + cardCls + '">' +
          '<div class="tl-active-top">' +
            '<span class="tl-active-name">' + escHtml(v.name) + '</span>' +
            '<span class="' + badgeCls + '">' + badgeTxt + '</span>' +
          '</div>' +
          '<div class="tl-active-desc">' + desc + '</div>' +
          '<div class="tl-active-dday red">' + ddayTxt + '</div>' +
        '</div></div>';
      } else if (isDone) {
        var dStr = hasDate ? 'D+' + dNum : '완료';
        html += '<div class="tl-dot done"></div>';
        html += '<div class="tl-content"><div class="tl-passive">' +
          '<div><div class="tl-passive-name done">' + escHtml(v.name) + '</div>' +
          '<div class="tl-passive-sub">' + pct + '% 완료</div></div>' +
          '<span class="tl-passive-dday" style="color:#ccc;">' + dStr + '</span>' +
        '</div></div>';
      } else {
        var ddayColor = (hasDate && dNum <= 30) ? '#0047A0' : '#bbb';
        var ddayStr = hasDate ? 'D-' + dNum : '미정';
        var subTxt = pct > 0 ? pct + '% 진행 중' : '0% 미시작';
        html += '<div class="tl-dot"></div>';
        html += '<div class="tl-content"><div class="tl-passive">' +
          '<div><div class="tl-passive-name upcoming">' + escHtml(v.name) + '</div>' +
          '<div class="tl-passive-sub">' + subTxt + '</div></div>' +
          '<span class="tl-passive-dday" style="color:' + ddayColor + ';">' + ddayStr + '</span>' +
        '</div></div>';
      }

      html += '</div>';
    });
    html += '</div>';
    body.innerHTML = html;
    body.querySelectorAll('.tl-item').forEach(function(item) {
      var h = item.getBoundingClientRect().height;
      item.querySelector('.tl-dot').style.setProperty('--tl-item-h', h + 'px');
    });
  } catch(e) {
    body.innerHTML = '<div style="padding:16px;text-align:center;font-size:11px;color:#c0392b;">로딩 실패</div>';
  }
}

// ============================================================
// ── Sliding Pill Tab ──

// Tab Section
// ============================================================
var _confirmUrl = '';
function goToRedmineVersion(el) {
  var item = el.closest('.tl-item');
  var projectId = item.getAttribute('data-pid');
  var versionId = item.getAttribute('data-vid');
  var base = localStorage.getItem('redmine_url') || '';
  if (!base) { alert('Redmine URL이 설정되지 않았습니다.'); return; }
  _confirmUrl = base.replace(/\/$/, '') + '/projects/' + projectId + '/roadmap#version-' + versionId;
  document.getElementById('confirmUrl').textContent = _confirmUrl;
  document.getElementById('confirmModal').classList.add('open');
}
function confirmRedmineGo() {
  var url = _confirmUrl;
  closeConfirmModal();
  if (url) window.open(url, '_blank');
}
function closeConfirmModal(e) {
  if (e && e.target !== document.getElementById('confirmModal')) return;
  document.getElementById('confirmModal').classList.remove('open');
  _confirmUrl = '';
}

function selectMemberFromCard(el) {
  var shortName = el.getAttribute('data-name');
  document.querySelectorAll('.risk-member-row').forEach(function(row) {
    row.classList.toggle('selected', row.getAttribute('data-name') === shortName);
  });
  var searchEl = document.getElementById('tabAssigneeSearch');
  if (searchEl) searchEl.value = shortName;
  // tabAssigneeFilter(드롭다운)도 동기화 — 실제 필터링에 사용됨
  var assigneeSelect = document.getElementById('tabAssigneeFilter');
  if (assigneeSelect && shortName) {
    for (var i = 0; i < assigneeSelect.options.length; i++) {
      if (assigneeSelect.options[i].text.trim() === shortName.trim()) {
        assigneeSelect.selectedIndex = i;
        break;
      }
    }
  }
  switchTab('assignee');
  renderTab();
  var bottomEl = document.querySelector('.bottom-section');
  if (bottomEl) bottomEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function switchTab(tab) {
  var table = document.querySelector('.tab-table');
  if (table) table.classList.add('fading');

  setTimeout(function() {
    currentTab = tab;
    document.querySelectorAll('.tab-item').forEach(function(el) {
      el.classList.toggle('active', el.dataset.tab === tab);
    });
    // assignee 탭이 아닌 탭으로 전환 시 담당자 필터 초기화
    if (tab !== 'assignee') {
      var assigneeSelect = document.getElementById('tabAssigneeFilter');
      if (assigneeSelect) assigneeSelect.selectedIndex = 0;
      var assigneeSearch = document.getElementById('tabAssigneeSearch');
      if (assigneeSearch) assigneeSearch.value = '';
      document.querySelectorAll('.risk-member-row').forEach(function(row) {
        row.classList.remove('selected');
      });
    }
    renderTab();
    if (table) table.classList.remove('fading');
  }, 200);
}

function populateTabFilters() {
  if (!allData) return;
  var usersData = allData.users_data || {};

  var assigneeSel = document.getElementById('tabAssigneeFilter');
  if (assigneeSel) {
    var curAssignee = assigneeSel.value;
    assigneeSel.innerHTML = '<option value="">EVERYONE</option>';
    var names = Object.keys(usersData).sort();
    names.forEach(function(uname) {
      var opt = document.createElement('option');
      opt.value = uname;
      opt.textContent = getShortName(uname);
      assigneeSel.appendChild(opt);
    });
    if (curAssignee) assigneeSel.value = curAssignee;
  }
}

function getTabIssues() {
  if (!allData) return [];
  var usersData = allData.users_data || {};
  var todayStr = new Date().toISOString().slice(0,10);
  var future3 = new Date(); future3.setDate(future3.getDate() + 7);
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
  var keywordF = (document.getElementById('tabKeywordSearch') || {}).value || '';
  keywordF = keywordF.trim().toLowerCase();
  var priorityF = (document.getElementById('tabPriorityFilter') || {}).value || '';
  var assigneeF = (document.getElementById('tabAssigneeFilter') || {}).value || '';
  var statusF = (document.getElementById('tabStatusFilter') || {}).value || '';

  if (keywordF) issues = issues.filter(function(i) {
    return String(i.id).includes(keywordF) || i.subject.toLowerCase().includes(keywordF);
  });
  if (priorityF) issues = issues.filter(function(i) {
    var p = i.priority || '';
    if (priorityF === 'High') return p === 'High' || p === '높음';
    if (priorityF === 'Normal') return p === 'Normal' || p === '보통';
    if (priorityF === 'Low') return p === 'Low' || p === '낮음';
    return true;
  });
  if (assigneeF) issues = issues.filter(function(i) { return i._uname === assigneeF; });
  if (statusF === 'closed') {
    issues = issues.filter(function(i) { return CLOSED_SET_JS.has(i.status); });
  } else if (statusF === 'all') {
    // 전체 — 필터 없음
  } else {
    // 기본값: OPEN ISSUES — 완료/보류 제외
    issues = issues.filter(function(i) {
      return !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    });
  }

  // Sort
  var sk = window._sortKey || 'due_date';
  var sd = window._sortDir || 'asc';
  issues.sort(function(a, b) {
    var av, bv;
    if (sk === 'id')       { av = a.id; bv = b.id; }
    else if (sk === 'subject') { av = (a.subject||'').toLowerCase(); bv = (b.subject||'').toLowerCase(); }
    else if (sk === 'dept')    { av = (a._dept||'').toLowerCase(); bv = (b._dept||'').toLowerCase(); }
    else if (sk === 'assignee'){ av = (a._short||'').toLowerCase(); bv = (b._short||'').toLowerCase(); }
    else if (sk === 'status')  { av = (a.status||'').toLowerCase(); bv = (b.status||'').toLowerCase(); }
    else if (sk === 'priority'){ var pm={'high':0,'높음':0,'normal':1,'보통':1,'low':2,'낮음':2}; av = pm[(a.priority||'').toLowerCase()]??9; bv = pm[(b.priority||'').toLowerCase()]??9; }
    else { av = a.due_date || '9999'; bv = b.due_date || '9999'; } // due_date default
    if (av < bv) return sd === 'asc' ? -1 : 1;
    if (av > bv) return sd === 'asc' ? 1 : -1;
    return 0;
  });

  return issues;
}

function setSort(key) {
  if (window._sortKey === key) {
    window._sortDir = window._sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    window._sortKey = key;
    window._sortDir = 'asc';
  }
  currentPage = 1;
  document.querySelectorAll('th[data-sort]').forEach(function(th) {
    var icon = th.querySelector('.sort-icon');
    if (!icon) return;
    if (th.dataset.sort === window._sortKey) {
      icon.textContent = window._sortDir === 'asc' ? ' ▲' : ' ▼';
      th.style.color = '#000';
    } else {
      icon.textContent = ' ⇅';
      th.style.color = '#bbb';
    }
  });
  renderTab();
}

var currentPage = 1;
var perPage = 10;

function setPerPage(n, el) {
  perPage = n;
  currentPage = 1;
  document.querySelectorAll('.pg-toggle-btn').forEach(function(b) { b.classList.remove('active'); });
  el.classList.add('active');
  renderTab();
}

function renderPagination(total) {
  var totalPages = Math.ceil(total / perPage);
  var start = (currentPage - 1) * perPage + 1;
  var end = Math.min(currentPage * perPage, total);
  var infoEl = document.getElementById('pgInfo');
  var ctrlEl = document.getElementById('pgControls');
  if (infoEl) infoEl.innerHTML = 'Showing ' + start + '\u2013' + end + ' of <span style="color:#0047A0;font-size:11px;letter-spacing:0.1em;">' + total + ' ENTRIES</span>';
  if (!ctrlEl) return;

  var html = '';
  html += '<button class="pg-btn" onclick="goPage(' + (currentPage - 1) + ')"' + (currentPage <= 1 ? ' disabled' : '') + '>Previous</button>';

  var pages = [];
  if (totalPages <= 5) {
    for (var i = 1; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    if (currentPage > 3) pages.push('...');
    for (var i = Math.max(2, currentPage - 1); i <= Math.min(totalPages - 1, currentPage + 1); i++) pages.push(i);
    if (currentPage < totalPages - 2) pages.push('...');
    pages.push(totalPages);
  }

  pages.forEach(function(p) {
    if (p === '...') {
      html += '<span class="pg-ellipsis">...</span>';
    } else {
      html += '<span class="pg-num' + (p === currentPage ? ' active' : '') + '" onclick="goPage(' + p + ')">' + String(p).padStart(2, '0') + '</span>';
    }
  });

  html += '<button class="pg-btn" onclick="goPage(' + (currentPage + 1) + ')"' + (currentPage >= totalPages ? ' disabled' : '') + '>Next</button>';
  ctrlEl.innerHTML = html;
}

function goPage(p) {
  var total = getTabIssues().length;
  var totalPages = Math.ceil(total / perPage);
  if (p < 1 || p > totalPages) return;
  currentPage = p;
  renderTab();
}

function renderTab() {
  var issues = getTabIssues();
  document.getElementById('tabCount').textContent = issues.length + '건';

  var todayStr = new Date().toISOString().slice(0,10);
  var tbody = document.getElementById('tab-tbody');

  if (issues.length === 0) {
    var emptyHtml = '<tr><td colspan="7" style="padding:13px 12px;color:#bbb;font-size:11px;">이슈 없음</td></tr>';
    for (var e = 0; e < perPage - 1; e++) {
      emptyHtml += '<tr style="border-bottom:0.5px solid #e8e6e2;"><td colspan="7" style="padding:0;height:51px;"></td></tr>';
    }
    tbody.innerHTML = emptyHtml;
    renderPagination(0);
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

  var allIssues = issues;
  issues = issues.slice((currentPage - 1) * perPage, currentPage * perPage);
  renderPagination(allIssues.length);

  tbody.innerHTML = issues.map(function(i) {
    var isOverdue = i.due_date && i.due_date < todayStr && !CLOSED_SET_JS.has(i.status) && !HOLD_SET_JS.has(i.status);
    var isClosed = CLOSED_SET_JS.has(i.status) || HOLD_SET_JS.has(i.status);
    var elapsed, elapsedColor, elapsedCls;
    if (i._elapsed !== null) {
      if (i._elapsed < 0) {
        elapsed = 'D+' + Math.abs(i._elapsed);
        elapsed = isClosed ? 'CLOSED' : 'D+' + Math.abs(i._elapsed);
        elapsedCls = isClosed ? 'closed' : (Math.abs(i._elapsed) > 7 ? 'red' : 'orange-overdue');
      } else {
        var d = i._elapsed;
        elapsed = isClosed ? 'CLOSED' : (d <= 0 ? 'D-Day' : 'D-' + d);
        elapsedCls = isClosed ? 'closed' : (d <= 3 ? 'red' : d <= 10 ? 'orange' : 'gray');
      }
    } else { elapsed = '—'; elapsedCls = 'gray'; }

    if (!window._deptColorMap) window._deptColorMap = {};
    if (!window._deptColorIndex) window._deptColorIndex = 0;
    var _palette = ['#f59e0b','#2563eb','#e11d48','#16a34a','#7c3aed','#111111','#0891b2','#f97316'];
    if (i._dept && !window._deptColorMap[i._dept]) {
      window._deptColorMap[i._dept] = _palette[window._deptColorIndex % _palette.length];
      window._deptColorIndex++;
    }
    var rowDeptColor = (i._dept && window._deptColorMap[i._dept]) || '#ccc';
    var statusCls = CLOSED_SET_JS.has(i.status) ? 'done' : HOLD_SET_JS.has(i.status) ? 'wait' : i.status === '진행' ? 'progress' : '';
    var priorityCls = (i.priority === 'High' || i.priority === '높음') ? 'high' : '';
    var priorityTxt = (i.priority === 'High' || i.priority === '높음') ? 'HIGH'
      : (i.priority === 'Low' || i.priority === '낙음') ? 'LOW' : 'NORMAL';
    var dueTip = i.due_date ? '마감 ' + i.due_date : '마감일 없음';

    return '<tr onclick="openIssue(' + i.id + ')" style="border-left:3px solid ' + rowDeptColor + ';">' +
      '<td class="td-id">#' + i.id + '</td>' +
      '<td><div class="td-subject">' + escHtml(i.subject) + '</div>' +
        '<div class="td-project">' + escHtml(i.project) + '</div></td>' +
      '<td><span class="td-group-badge">' + escHtml(i._dept || '—') + '</span></td>' +
      '<td style="font-size:11px;font-weight:500;">' + escHtml(i._short) + '</td>' +
      '<td><span class="status-badge ' + statusCls + '">' + escHtml(i.status) + '</span></td>' +
      '<td class="td-priority ' + priorityCls + '">' + priorityTxt + '</td>' +
      '<td style="text-align:right;">' +
        '<div class="dday-wrap">' +
          '<span class="td-dday ' + elapsedCls + '">' + elapsed + '</span>' +
          '<div class="dday-tooltip">' + dueTip + '</div>' +
        '</div>' +
      '</td>' +
    '</tr>';
  }).join('');

  var currentRows = issues.length;
  var emptyRows = perPage - currentRows;
  if (emptyRows > 0) {
    var emptyHtml = '';
    for (var e = 0; e < emptyRows; e++) {
      emptyHtml += '<tr style="border-bottom:0.5px solid #e8e6e2;">' +
        '<td colspan="7" style="padding:0;height:51px;">&nbsp;</td>' +
      '</tr>';
    }
    tbody.innerHTML += emptyHtml;
  }
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

/* ── Risk Chart ── */

// ── Risk Chart ──
var _riskWeeks = 12;

function toggleRiskSettings() {
  var panel = document.getElementById('riskSettingsPanel');
  var btn = document.getElementById('riskGearBtn');
  panel.classList.toggle('open');
  btn.classList.toggle('active');
}
function setWeeks(el, val) {
  el.parentElement.querySelectorAll('.ai-dow-btn').forEach(function(b){ b.classList.remove('active'); });
  el.classList.add('active');
  _riskWeeks = val;
}
function applyRiskSettings() {
  localStorage.setItem('riskWeeks', _riskWeeks);
  document.getElementById('riskSettingsPanel').classList.remove('open');
  document.getElementById('riskGearBtn').classList.remove('active');
  loadRiskHistory();
}

async function loadRiskHistory() {
  try {
    var params = new URLSearchParams({
      project_id: currentProjectId,
      weeks: _riskWeeks
    });
    var res = await fetch('/api/risk-history?' + params);
    var data = await res.json();
    renderRiskChart(data.history || []);
  } catch(e) {
    console.error('risk history load failed', e);
  }
}
function renderRiskChart(history) {
  var bars = document.getElementById('riskChartBars');
  var labels = document.getElementById('riskChartLabels');
  if (!bars) return;
  if (history.length < 2) {
    bars.innerHTML = '<div class="risk-chart-empty">ACCUMULATING DATA</div>';
    if (labels) labels.innerHTML = '';
    return;
  }
  var maxScore = Math.max.apply(null, history.map(function(d){ return d.score; })) || 1;
  bars.innerHTML = history.map(function(d) {
    var h = Math.max(4, Math.round((d.score / maxScore) * 100));
    var lvl = (d.level || '').toLowerCase();
    var cls = lvl === 'critical' ? 'critical' : lvl === 'low' ? 'low' : 'normal';
    return '<div class="ai-bar-group"><div class="ai-bar ' + cls + '" style="height:' + h + '%;"></div></div>';
  }).join('');
  labels.innerHTML = history.map(function(d) {
    return '<div class="ai-bar-label">' + d.week + '</div>';
  }).join('');
}

/* ── AI Text Card ── */
async function generateAiSummary() {
  var el    = document.getElementById('aiSummaryText');
  var headEl = document.getElementById('aiHeadline');
  el.textContent = '분석 중...';
  if (headEl) headEl.textContent = '분석 중...';

  // Render chart immediately with current data
  loadRiskHistory();

  try {
    var params = new URLSearchParams({ project_id: currentProjectId, updated_after: currentUpdatedAfter });
    var res  = await fetch('/api/ai/report-summary?' + params);
    var data = await res.json();
    if (data.summary) {
      aiSummaryText = data.summary;
      var lines = data.summary.trim().split(String.fromCharCode(10)).filter(function(l){ return l.trim(); });
      var headlineEl = document.getElementById('aiHeadline');
      if (headlineEl) headlineEl.textContent = lines[0] || '';
      var bodyLines = lines.slice(1);
      el.innerHTML = bodyLines.map(function(line) {
        var isS1 = /^ST1?:/.test(line);
        var isS2 = /^ST2:/.test(line);
        var rawKey = line.split(':')[0];
        var keyMap = {'상황1': 'ST1', '상황2': 'ST2', '상황': 'ST', '원인': 'CA', '대처': 'AC'};
        var key = (keyMap[rawKey] || rawKey) + ':';
        var val = line.slice(line.indexOf(':') + 1).trim();
        var keyColor = '#555';
        var valColor = isS1 ? '#e05a4e' : isS2 ? '#d4824a' : '#fff';
        return '<div style="display:flex;gap:8px;margin-bottom:10px;align-items:baseline;">' +
          '<span style="font-family:DM Mono,monospace;font-size:9px;color:' + keyColor + ';text-transform:uppercase;letter-spacing:0.08em;flex-shrink:0;">' + key + '</span>' +
          '<span style="font-size:11px;color:' + valColor + ';line-height:1.5;">' + escHtml(val) + '</span>' +
          '</div>';
      }).join('');
    } else if (data.error) {
      if (headEl) headEl.textContent = 'AI 오류';
      el.textContent = data.error;
    }
  } catch(e) {
    if (headEl) headEl.textContent = '요청 실패';
    bodyEl.textContent = '서버에 연결할 수 없습니다.';
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
<script>
  document.getElementById('filterDateInput').addEventListener('change', function() {
    document.getElementById('filterDateDisplay').textContent = this.value.replace(/-/g, '.');
  });
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


@app.get("/api/groups")
async def api_groups(project_id: str = ""):
    if not project_id:
        return {"groups": []}
    groups = get_groups(project_id)
    return {"groups": groups}


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


@app.get("/api/risk-history")
async def api_risk_history(project_id: str = "", weeks: int = 12):
    """
    저장된 스냅샷에서 최근 N주 반환.
    스냅샷 없으면 빈 배열 반환 (역산 제거).
    """
    try:
        with open(RISK_HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"history": []}

    key = f"project_{project_id}" if project_id else "all"
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


@app.post("/api/risk-snapshot/trigger")
async def trigger_snapshot():
    """개발용: 수동 스냅샷 저장"""
    save_risk_snapshot()
    return {"status": "ok"}


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
