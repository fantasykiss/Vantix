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
from datetime import date, datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ==================== 서버 설정 ====================
BASE_URL = "http://localhost:3000"
API_KEY  = "REDACTED_REDMINE_API_KEY"
# ====================================================

PROGRESS_SET = {"진행", "진행대기", "In Progress"}
RESOLVED_SET = {"해결", "해결됨", "Resolved"}
CLOSED_SET   = {"완료", "완료(잔땡처리)", "Closed", "반려", "Rejected"}
HOLD_SET     = {"보류", "보류(스펙아웃)", "스펙아웃"}

# 그룹명 키워드 매핑 (이름 앞부분으로 판별)
DEPT_PLANNING = "기획"
DEPT_SERVER   = "서버"
DEPT_CLIENT   = "클라"

# 그룹명 정규화: 접두어 숫자/기호가 붙은 그룹을 하나로 통합
DEPT_NORMALIZE = {
    "1기획": "기획",
    "1PM":   "PM",
    "1클라": "클라",
    "1서버": "서버",
    "1UI":   "UI",
}

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

app = FastAPI()

# ==================== 캐시 ====================
_cache = {}

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
    print(f"  💾 캐시 저장: {key} ({datetime.now().strftime('%H:%M:%S')})")


def cache_age_str(project_id, updated_after):
    key = cache_key(project_id, updated_after)
    entry = _cache.get(key)
    if not entry:
        return None
    age = int((datetime.now() - entry["fetched_at"]).total_seconds())
    if age < 60:
        return f"{age}초 전"
    return f"{age // 60}분 전"


def background_refresh():
    while True:
        time.sleep(AUTO_REFRESH_INTERVAL)
        if not _auto_refresh_params:
            continue
        for key, params in list(_auto_refresh_params.items()):
            pid  = params["project_id"]
            uaft = params["updated_after"]
            print(f"  🔄 백그라운드 자동갱신: {key}")
            try:
                data = build_dashboard_data(pid, uaft)
                set_cache(pid, uaft, data)
                print(f"  ✅ 자동갱신 완료: {key}")
            except Exception as e:
                print(f"  ⚠️  자동갱신 실패: {e}")


_refresh_thread = threading.Thread(target=background_refresh, daemon=True)
_refresh_thread.start()


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
                print(f"  🔁 재시도 ({attempt+1}/{retries-1}): offset {params.get('offset','?') if params else '?'}")
                time.sleep(1)
            else:
                print(f"  ⚠️  요청 실패: {url}\n     {e}")
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
    print(f"  ⚡ 병렬 fetch: {len(offsets)+1}페이지 / 총 {total}건")

    def fetch_page(offset):
        params = {**(base_params or {}), "limit": limit, "offset": offset}
        data = fetch(path, params)
        return data.get(key, [])

    # 3) 병렬 실행 (max_workers=8)
    pages = [None] * len(offsets)
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(fetch_page, off): i for i, off in enumerate(offsets)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                pages[idx] = future.result()
            except Exception as e:
                print(f"  ⚠️ 페이지 fetch 실패 (offset={offsets[idx]}): {e}")
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


def dept_name(name):
    """'기획_홍길동' → '기획', '1기획_홍길동' → '기획' (정규화 포함)"""
    raw = name.split("_")[0] if "_" in name else name
    return DEPT_NORMALIZE.get(raw, raw)


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

    # 위험도 점수 계산 (비율 기반 0~100점)
    # = overdue/total×60 + urgent/total×30 + pending/total×10
    for pr in project_risk.values():
        total = pr["total"] or 1  # 0 나누기 방지
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
    }


# ==================== HTML ====================

HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RedRisk</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg: #07071a;
  --surface: rgba(255,255,255,.04);
  --surface2: rgba(255,255,255,.07);
  --border: rgba(255,255,255,.08);
  --accent: #6366f1;
  --accent2: #3b82f6;
  --danger: #f87171;
  --warn: #fbbf24;
  --success: #34d399;
  --purple: #a78bfa;
  --pink: #f472b6;
  --orange: #fb923c;
  --teal: #2dd4bf;
  --indigo: #818cf8;
  --text: #f1f5f9;
  --text2: #94a3b8;
  --text3: #475569;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Noto Sans KR', sans-serif;
  background: #000;
  color: var(--text);
  min-height: 100vh;
}

/* ── 헤더 ── */
.header {
  background: rgba(7,7,26,.7);
  border-bottom: 1px solid rgba(255,255,255,.06);
  padding: 24px 40px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
}
.header-left { display: flex; align-items: center; gap: 16px; }
.header-logo {
  width: 40px; height: 40px; border-radius: 10px;
  background: linear-gradient(135deg, #6366f1, #3b82f6);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px;
}
.header-title { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
.header-sub { font-size: 12px; color: var(--text2); margin-top: 2px; font-family: 'JetBrains Mono', monospace; }
.header-right { display: flex; align-items: center; gap: 12px; }
.btn {
  padding: 8px 18px; border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; border: none; transition: all .2s; font-family: inherit;
}
.btn-primary { background: var(--accent); color: white; }
.btn-primary:hover { background: #2563eb; transform: translateY(-1px); }
.btn-ghost { background: var(--surface2); color: var(--text2); border: 1px solid var(--border); }
.btn-ghost:hover { border-color: var(--accent); color: var(--text); }
.live-dot {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--success); font-family: 'JetBrains Mono', monospace;
  transition: color .3s;
}
.live-dot::before {
  content: ''; width: 7px; height: 7px; border-radius: 50%;
  background: var(--success); animation: pulse 2s infinite;
  transition: background .3s;
}
.live-dot.offline { color: var(--danger); }
.live-dot.offline::before { background: var(--danger); animation: none; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

/* ── 컨트롤 패널 ── */
.control-panel {
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 16px 40px; display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap;
}
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 11px; font-weight: 700; color: var(--text); text-transform: uppercase; letter-spacing: 1px; }
.field select, .field input {
  padding: 9px 14px; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 13px; font-family: inherit;
  min-width: 200px; outline: none; transition: border-color .2s;
}
.field select:focus, .field input:focus { border-color: var(--accent); }

/* ── 요약 카드 그리드 (5개) ── */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
  padding: 24px 40px 0;
  overflow: visible;
}

/* 초과 카드 - 상단 실선 */
.summary-card.grp-planning { border-top: 3px solid #a78bfa; }
.summary-card.grp-server   { border-top: 3px solid #2dd4bf; }
.summary-card.grp-client   { border-top: 3px solid #f472b6; }
/* 진행대기 카드 - 상단 점선 (같은 색) */
.summary-card.grp-planning.pending { border-top: 3px dashed #a78bfa; opacity: .85; }
.summary-card.grp-server.pending   { border-top: 3px dashed #2dd4bf; opacity: .85; }
.summary-card.grp-client.pending   { border-top: 3px dashed #f472b6; opacity: .85; }

/* 하단 6개 카드 — 콤팩트 사이즈 */
.summary-card.grp-planning,
.summary-card.grp-server,
.summary-card.grp-client {
  padding: 12px 16px;
  border-radius: 10px;
}
.summary-card.grp-planning .card-label,
.summary-card.grp-server   .card-label,
.summary-card.grp-client   .card-label {
  margin-bottom: 6px;
}
.summary-card.grp-planning .card-number,
.summary-card.grp-server   .card-number,
.summary-card.grp-client   .card-number {
  font-size: 26px;
}
.summary-card.grp-planning .card-sub-label,
.summary-card.grp-server   .card-sub-label,
.summary-card.grp-client   .card-sub-label {
  margin-top: 3px;
  font-size: 9px;
}
.summary-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 22px 24px;
  position: relative; overflow: visible !important; transition: transform .2s;
}
.summary-card:hover { transform: translateY(-2px); }
.summary-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  border-radius: 14px 14px 0 0;
}
.summary-card.blue::before   { background: var(--accent); }
.summary-card.cyan::before   { background: var(--accent2); }
.summary-card.red::before    { background: var(--danger); }
.summary-card.warn::before   { background: var(--warn); }
.summary-card.purple::before { background: var(--purple); }
.summary-card.pink::before   { background: var(--pink); }
.summary-card.teal::before   { background: var(--teal); }
.summary-card.indigo::before { background: var(--indigo); }

.card-label { font-size: 15px; font-weight: 800; color: var(--text2); letter-spacing: .3px; margin-bottom: 12px; }
.card-sub-label { font-size: 10px; color: var(--text2); margin-top: 4px; }
.card-number { font-size: 42px; font-weight: 900; line-height: 1; font-family: 'JetBrains Mono', monospace; }
.card-number.blue   { color: var(--accent); }
.card-number.cyan   { color: var(--accent2); }
.card-number.red    { color: var(--danger); }
.card-number.warn   { color: var(--warn); }
.card-number.purple { color: var(--purple); }
.card-number.pink   { color: var(--pink); }
.card-number.teal   { color: var(--teal); }
.card-number.indigo { color: var(--indigo); }

.card-icon {
  position: absolute; right: 16px; top: 50%; transform: translateY(-50%);
  font-size: 36px; opacity: 1;
  filter: drop-shadow(0 0 8px currentColor);
  pointer-events: none;
}
.summary-card.blue   .card-icon { color: var(--accent); }
.summary-card.cyan   .card-icon { color: var(--accent2); }
.summary-card.warn   .card-icon { color: var(--warn); }
.summary-card.red    .card-icon { color: var(--danger); }
.summary-card.purple .card-icon { color: var(--purple); }
.summary-card.pink   .card-icon { color: var(--pink); }
.summary-card.teal   .card-icon { color: var(--teal); }
.summary-card.indigo .card-icon { color: var(--indigo); }

/* ── 참여 사용자 호버 툴팁 ── */
.user-count-card { position: relative; z-index: 9999; }
.user-tooltip {
  display: none;
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  background: rgba(10,10,30,.97);
  border: 1px solid rgba(99,102,241,.45);
  border-radius: 14px;
  padding: 14px 18px;
  z-index: 9999;
  width: 900px;
  box-shadow: 0 12px 40px rgba(0,0,0,.7), 0 0 0 1px rgba(99,102,241,.15);
  pointer-events: none;
}
.user-tooltip::-webkit-scrollbar { width: 4px; }
.user-tooltip::-webkit-scrollbar-track { background: rgba(255,255,255,.03); border-radius: 2px; }
.user-tooltip::-webkit-scrollbar-thumb { background: #6366f1; border-radius: 2px; }
/* 툴팁 위쪽 화살표 */
.user-tooltip::before {
  content: '';
  position: absolute;
  top: -7px;
  left: 28px;
  width: 12px; height: 12px;
  background: rgba(10,10,30,.97);
  border-left: 1px solid rgba(99,102,241,.45);
  border-top: 1px solid rgba(99,102,241,.45);
  transform: rotate(45deg);
}
.user-count-card:hover .user-tooltip { display: block; }
.user-tooltip::-webkit-scrollbar { width: 4px; }
.user-tooltip::-webkit-scrollbar-track { background: rgba(255,255,255,.03); border-radius: 2px; }
.user-tooltip::-webkit-scrollbar-thumb { background: #6366f1; border-radius: 2px; }
.user-tooltip-title {
  font-size: 10px; font-weight: 800; color: var(--text3);
  text-transform: uppercase; letter-spacing: 1.2px;
  margin-bottom: 10px; padding-bottom: 8px;
  border-bottom: 1px solid rgba(255,255,255,.07);
  display: flex; align-items: center; justify-content: space-between;
}
.user-tooltip-total {
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent); font-size: 11px; font-weight: 700;
}
.user-tooltip-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.user-tooltip-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.user-tooltip-row-label {
  font-size: 10px; font-weight: 800;
  text-transform: uppercase; letter-spacing: .5px;
  width: 64px; min-width: 64px; flex-shrink: 0; text-align: center;
  padding: 4px 6px; border-radius: 6px;
  margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.user-tooltip-row-members {
  display: flex; flex-wrap: wrap; gap: 5px; flex: 1;
}
.user-tooltip-dept-label {
  font-size: 9px; font-weight: 800;
  text-transform: uppercase; letter-spacing: 1px;
  padding: 3px 8px; border-radius: 4px;
  margin-bottom: 4px; display: inline-block;
}
.user-tooltip-item {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 4px 4px 6px; border-radius: 6px;
  font-size: 12px; color: var(--text2);
  transition: background .1s;
}
.user-tooltip-item:hover { background: rgba(255,255,255,.05); }
.user-tooltip-avatar {
  width: 22px; height: 22px; border-radius: 6px;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  display: flex; align-items: center; justify-content: center;
  font-size: 9px; font-weight: 700; flex-shrink: 0; color: white;
}
.user-tooltip-name { flex: 1; font-size: 12px; color: var(--text); }
.user-tooltip-issue-cnt {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px; color: var(--text3);
  background: rgba(255,255,255,.05);
  padding: 1px 6px; border-radius: 4px;
}

/* 그룹별 dept label 색상 */
.dept-color-기획 { background: rgba(167,139,250,.15); color: #a78bfa; }
.dept-color-PM   { background: rgba(251,191,36,.12);  color: #fbbf24; }
.dept-color-UI   { background: rgba(244,114,182,.15); color: #f472b6; }
.dept-color-서버 { background: rgba(45,212,191,.15);  color: #2dd4bf; }
.dept-color-클라 { background: rgba(248,113,113,.15); color: #f87171; }
.dept-color-default { background: rgba(99,102,241,.12); color: #818cf8; }

/* ── 메인 콘텐츠 ── */
.content { padding: 0 40px 40px; }

/* ── 탭 ── */
.tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; }
.tab {
  padding: 10px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
  border-radius: 8px 8px 0 0; color: var(--text3); border: 1px solid transparent;
  border-bottom: none; transition: all .2s; background: none;
  font-family: inherit; margin-bottom: -1px;
}
.tab.active { background: var(--surface); color: var(--text); border-color: var(--border); border-bottom-color: var(--surface); }
.tab:hover:not(.active) { color: var(--text2); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* ── 유저 카드 그리드 ── */
.users-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }
.user-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden; transition: all .2s;
}
.user-card:hover { border-color: var(--accent); box-shadow: 0 0 20px rgba(59,130,246,.1); }
.user-card-header {
  padding: 16px 20px; display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid var(--border); background: var(--surface2);
}
.avatar {
  width: 42px; height: 42px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; font-weight: 700; flex-shrink: 0;
}
.user-name { font-size: 15px; font-weight: 700; }
.user-dept { font-size: 11px; font-weight: 700; color: var(--accent); margin-top: 3px; letter-spacing: .3px; }
.user-stats { display: flex; gap: 0; border-bottom: 1px solid var(--border); }
.stat { flex: 1; padding: 12px; text-align: center; border-right: 1px solid var(--border); }
.stat:last-child { border-right: none; }
.stat-val { font-size: 20px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.stat-val.red { color: var(--danger); }
.stat-val.warn { color: var(--warn); }
.stat-val.green { color: var(--success); }
.stat-lbl { font-size: 10px; color: var(--text3); margin-top: 2px; }

/* ── 이슈 목록 ── */
.issue-list { padding: 12px 16px; max-height: 400px; overflow-y: auto; }
.issue-list::-webkit-scrollbar { width: 6px; }
.issue-list::-webkit-scrollbar-track { background: rgba(255,255,255,.04); border-radius: 3px; }
.issue-list::-webkit-scrollbar-thumb { background: #3b82f6; border-radius: 3px; }
.issue-list::-webkit-scrollbar-thumb:hover { background: #60a5fa; }
.issue-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  border-radius: 8px; transition: background .15s; font-size: 12px;
}
.issue-item:hover { background: var(--surface2); }
.issue-id { font-family: 'JetBrains Mono', monospace; color: var(--accent); font-size: 11px; flex-shrink: 0; }
.issue-subject { flex: 1; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.issue-subject a { color: inherit; text-decoration: none; }
.issue-subject a:hover { color: var(--text); }
.badge {
  display: inline-flex; align-items: center; padding: 2px 8px;
  border-radius: 20px; font-size: 10px; font-weight: 700; flex-shrink: 0;
}
.badge-new      { background: rgba(59,130,246,.15); color: #60a5fa; }
.badge-progress { background: rgba(245,158,11,.15); color: #fbbf24; }
.badge-resolved { background: rgba(16,185,129,.15); color: #34d399; }
.badge-closed   { background: rgba(100,116,139,.15); color: #94a3b8; }
.badge-urgent   { background: rgba(251,191,36,.15); color: #fbbf24; }
.dday { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; flex-shrink: 0; }
.dday-over { color: var(--danger); }
.dday-soon { color: var(--warn); }
.dday-ok   { color: var(--text3); }

/* ── 로딩 ── */
.loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 20px; color: var(--text3);
  min-height: 60vh;
  width: 100%;
  grid-column: 1 / -1;
}
.spinner {
  display: flex; flex-direction: column; align-items: center; gap: 16px;
}
.rocket-loader { position: relative; width: 120px; height: 120px; }
.rocket-emoji {
  font-size: 56px; position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%) rotate(-45deg);
  animation: rocket-bob 1.6s ease-in-out infinite;
  filter: drop-shadow(0 0 16px rgba(59,130,246,.6));
}
@keyframes rocket-bob {
  0%,100% { transform: translate(-50%,-50%) rotate(-45deg) translateY(0px); }
  50%      { transform: translate(-50%,-50%) rotate(-45deg) translateY(-12px); }
}
.rocket-stars { position: absolute; inset: 0; }
.rstar {
  position: absolute; border-radius: 50%;
  background: white; animation: rstar-twinkle 1.5s ease-in-out infinite;
}
@keyframes rstar-twinkle { 0%,100%{opacity:0;transform:scale(.4)} 50%{opacity:1;transform:scale(1)} }
.rocket-trail {
  display: flex; gap: 6px; align-items: center;
}
.trail-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent);
  animation: trail-pulse 1.2s ease-in-out infinite;
}
@keyframes trail-pulse { 0%,100%{opacity:.2;transform:scale(.6)} 50%{opacity:1;transform:scale(1)} }
.loading-text {
  font-size: 13px; font-family: 'JetBrains Mono', monospace;
  color: #60a5fa; text-align: center; animation: text-fade 2s ease-in-out infinite;
}
@keyframes text-fade { 0%,100%{opacity:.5} 50%{opacity:1} }

/* ── 위험 경보 탭 ── */
.risk-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  background: rgba(251,146,60,.06); border: 1px solid rgba(251,146,60,.2);
  border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; flex-wrap: wrap; gap: 12px;
}
.risk-legend { font-size: 12px; color: var(--text3); }
.risk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
.risk-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden; transition: all .2s;
}
.risk-card:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,.3); }
.risk-card-top {
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: flex-start;
}
.risk-project-name { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
.risk-score-badge {
  display: flex; flex-direction: column; align-items: center;
  min-width: 56px;
}
.risk-score-num {
  font-size: 28px; font-weight: 900;
  font-family: 'JetBrains Mono', monospace; line-height: 1;
}
.risk-level-label {
  font-size: 10px; font-weight: 700; margin-top: 2px;
  text-transform: uppercase; letter-spacing: 1px;
}
.risk-stats {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 0; border-bottom: 1px solid var(--border);
}
.risk-stat { padding: 10px 8px; text-align: center; border-right: 1px solid var(--border); }
.risk-stat:last-child { border-right: none; }
.risk-stat-val { font-size: 18px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.risk-stat-lbl { font-size: 10px; color: var(--text3); margin-top: 2px; }
.risk-samples { padding: 10px 16px 14px; }
.risk-sample-title { font-size: 10px; color: var(--text3); font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.risk-sample-item {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 0; font-size: 12px; border-bottom: 1px solid var(--border);
}
.risk-sample-item:last-child { border-bottom: none; }
.risk-sample-id { font-family: 'JetBrains Mono', monospace; color: var(--accent); font-size: 11px; flex-shrink: 0; }
.risk-sample-subject { flex: 1; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.risk-sample-subject a { color: inherit; text-decoration: none; }
.risk-sample-subject a:hover { color: var(--text); }
.risk-sample-due { font-size: 10px; font-family: 'JetBrains Mono', monospace; color: var(--danger); flex-shrink: 0; }
.risk-bar { height: 4px; }

/* ── 차트 탭 ── */
.charts-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }
.chart-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 24px;
}
.chart-title { font-size: 13px; font-weight: 700; color: var(--text2); margin-bottom: 20px; text-transform: uppercase; letter-spacing: 1px; }

/* ── 필터 바 ── */
.filter-bar { display: flex; gap: 10px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }
.search-input {
  padding: 8px 14px; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 13px; font-family: inherit;
  min-width: 200px; outline: none; transition: border-color .2s;
}
.search-input:focus { border-color: var(--accent); }
.filter-select {
  padding: 8px 14px; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 13px; font-family: inherit;
  outline: none; transition: border-color .2s; cursor: pointer;
}
.filter-select:focus { border-color: var(--accent); }
.count-badge { font-size: 12px; color: var(--text3); font-family: 'JetBrains Mono', monospace; }

/* ── 마감 초과 탭 ── */
.tab-danger {
  color: var(--danger) !important;
  border-color: rgba(239,68,68,.3) !important;
  animation: tab-pulse 2s infinite;
}
.tab-danger.active {
  background: rgba(239,68,68,.1) !important;
  border-bottom-color: rgba(239,68,68,.1) !important;
  color: #ff6b6b !important;
}
@keyframes tab-pulse { 0%,100%{opacity:1} 50%{opacity:.7} }
.tab-danger-badge {
  display: inline-block; background: var(--danger); color: white;
  font-size: 10px; font-weight: 700; padding: 1px 6px;
  border-radius: 10px; margin-left: 4px; font-family: 'JetBrains Mono', monospace;
}
.overdue-header {
  background: rgba(239,68,68,.08); border: 1px solid rgba(239,68,68,.2);
  border-radius: 10px; padding: 12px 18px; margin-bottom: 16px;
  display: flex; justify-content: space-between; align-items: center;
  font-size: 13px; color: #f87171;
}
.overdue-total { font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--danger); }
.overdue-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.overdue-table thead tr { background: rgba(239,68,68,.08); }
.overdue-table th { padding: 10px 14px; text-align: left; color: #f87171; font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid rgba(239,68,68,.2); }
.overdue-table td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.overdue-table tr:hover td { background: rgba(239,68,68,.04); }
.overdue-dday { font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--danger); font-size: 12px; }
.overdue-id { font-family: 'JetBrains Mono', monospace; color: var(--accent); font-size: 11px; }

/* ── 버전 카드 ── */
.version-cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.version-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 20px 22px; cursor: pointer;
  transition: all .2s; position: relative; overflow: hidden;
}
.version-card:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 0 20px rgba(59,130,246,.1); }
.version-card::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background: var(--accent); }
.version-card.overdue-version::before { background: var(--danger); }
.version-card.near-version::before { background: var(--warn); }
.version-name { font-size: 15px; font-weight: 700; margin-bottom: 6px; }
.version-due { font-size: 11px; color: var(--text3); margin-bottom: 14px; font-family: 'JetBrains Mono', monospace; }
.version-due.overdue { color: var(--danger); font-weight: 700; }
.version-due.soon { color: var(--warn); font-weight: 700; }
.version-progress-bar { height: 6px; background: var(--border); border-radius: 3px; margin-bottom: 12px; overflow: hidden; }
.version-progress-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .5s; }
.version-stats { display: flex; gap: 0; }
.version-stat { flex: 1; text-align: center; }
.version-stat-val { font-size: 18px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.version-stat-lbl { font-size: 10px; color: var(--text3); margin-top: 2px; }

/* ── 버전 상세 이슈 테이블 ── */
.version-issue-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.version-issue-table thead tr { background: var(--surface2); }
.version-issue-table th { padding: 10px 14px; text-align: left; color: var(--text2); font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid var(--border); }
.version-issue-table td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.version-issue-table tr:hover td { background: var(--surface2); }

/* ── 빈 상태 ── */
.empty { text-align: center; padding: 40px; color: var(--text3); font-size: 13px; }

@media (max-width: 768px) {
  .header { padding: 16px 20px; }
  .summary-grid { grid-template-columns: repeat(3,1fr); padding: 16px 20px 0; }
  .content { padding: 0 20px 40px; }
  .control-panel { padding: 12px 20px; }
  .users-grid { grid-template-columns: 1fr; }
  .charts-grid { grid-template-columns: 1fr; }
}

/* ── 이슈 편집 모달 ── */
.modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,.6); backdrop-filter: blur(4px);
  align-items: center; justify-content: center;
}
.modal-backdrop.open { display: flex; }
.modal {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; width: 520px; max-width: 95vw; max-height: 90vh;
  overflow-y: auto; box-shadow: 0 24px 80px rgba(0,0,0,.6);
  animation: modal-in .2s ease;
}
@keyframes modal-in { from { opacity:0; transform:translateY(-16px); } to { opacity:1; transform:translateY(0); } }
.modal-header {
  padding: 20px 24px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
}
.modal-title { font-size: 15px; font-weight: 700; line-height: 1.4; flex: 1; }
.modal-issue-id { font-family: 'JetBrains Mono', monospace; color: var(--accent); font-size: 12px; margin-bottom: 4px; }
.modal-close {
  width: 30px; height: 30px; border-radius: 8px; border: none; background: var(--surface2);
  color: var(--text3); cursor: pointer; font-size: 16px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; transition: all .15s;
}
.modal-close:hover { background: var(--border); color: var(--text); }
.modal-body { padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }
.modal-field { display: flex; flex-direction: column; gap: 6px; }
.modal-field label { font-size: 11px; font-weight: 700; color: var(--text3); text-transform: uppercase; letter-spacing: 1px; }
.modal-field select, .modal-field input, .modal-field textarea {
  padding: 10px 14px; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 13px; font-family: inherit;
  outline: none; transition: border-color .2s; width: 100%;
}
.modal-field select:focus, .modal-field input:focus, .modal-field textarea:focus { border-color: var(--accent); }
.modal-field textarea { resize: vertical; min-height: 90px; }
.modal-footer {
  padding: 16px 24px; border-top: 1px solid var(--border);
  display: flex; gap: 10px; justify-content: flex-end; align-items: center;
}
.modal-link { font-size: 12px; color: var(--text3); text-decoration: none; margin-right: auto; }
.modal-link:hover { color: var(--accent); }
.modal-save-btn { background: var(--accent); color: white; }
.modal-save-btn:hover { background: #2563eb; }
.modal-save-btn:disabled { opacity: .5; cursor: not-allowed; }
.modal-journals { border-top: 1px solid var(--border); padding: 16px 24px; }
.modal-journals-title { font-size: 11px; font-weight: 700; color: var(--text3); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
.journal-item { padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.journal-item:last-child { border-bottom: none; }
.journal-meta { color: var(--text3); margin-bottom: 4px; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
.journal-note { color: var(--text2); line-height: 1.5; }

/* ── 큐 배지 ── */
.btn-queue {
  background: var(--warn); color: #0a0e1a; font-weight: 700;
  animation: queue-pulse 2s infinite;
}
.btn-queue:hover { background: #d97706; }
.queue-badge {
  display: inline-block; background: rgba(0,0,0,.25); color: inherit;
  font-size: 11px; font-weight: 900; padding: 1px 7px;
  border-radius: 10px; margin-left: 4px; font-family: 'JetBrains Mono', monospace;
}
@keyframes queue-pulse { 0%,100%{opacity:1} 50%{opacity:.75} }

/* ── 큐 패널 ── */
.queue-panel-backdrop {
  display: none; position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,.6); backdrop-filter: blur(4px);
  align-items: center; justify-content: center;
}
.queue-panel-backdrop.open { display: flex; }
.queue-panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; width: 680px; max-width: 95vw; max-height: 85vh;
  overflow-y: auto; box-shadow: 0 24px 80px rgba(0,0,0,.6);
  animation: modal-in .2s ease;
}
.queue-panel-header {
  padding: 20px 24px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.queue-panel-title { font-size: 16px; font-weight: 700; }
.queue-panel-body { padding: 16px 24px; }
.queue-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 12px 14px; border-radius: 10px; margin-bottom: 8px;
  background: var(--surface2); border: 1px solid var(--border);
  font-size: 13px;
}
.queue-item-info { flex: 1; }
.queue-item-id { font-family: 'JetBrains Mono', monospace; color: var(--accent); font-size: 11px; margin-bottom: 3px; }
.queue-item-title { font-weight: 600; margin-bottom: 6px; }
.queue-item-changes { display: flex; gap: 8px; flex-wrap: wrap; }
.queue-change-tag {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600;
  background: rgba(59,130,246,.15); color: #60a5fa;
}
.queue-item-remove {
  background: none; border: none; color: var(--text3); cursor: pointer;
  font-size: 16px; padding: 2px 6px; border-radius: 6px;
  transition: all .15s; flex-shrink: 0;
}
.queue-item-remove:hover { background: rgba(239,68,68,.15); color: var(--danger); }
.queue-item.done { opacity: .5; border-color: var(--success); }
.queue-item.done .queue-item-id { color: var(--success); }
.queue-item.error { border-color: var(--danger); }
.queue-panel-footer {
  padding: 16px 24px; border-top: 1px solid var(--border);
  display: flex; gap: 10px; justify-content: flex-end; align-items: center;
}
.btn-apply { background: var(--success); color: white; font-weight: 700; }
.btn-apply:hover { background: #059669; }
.btn-apply:disabled { opacity: .5; cursor: not-allowed; }

/* ── 접속자 아이콘 ── */
.visitors-wrap { display: flex; align-items: center; gap: 4px; }
.visitor-dot {
  width: 32px; height: 32px; border-radius: 50%;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  border: 2px solid var(--surface);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: white;
  margin-left: -8px; transition: transform .2s;
  cursor: default; position: relative;
}
.visitor-dot:first-child { margin-left: 0; }
.visitor-dot:hover { transform: translateY(-3px); z-index: 10; }
.visitor-dot.self {
  background: linear-gradient(135deg, var(--success), var(--accent2));
  border-color: var(--success);
}
.visitor-more {
  width: 32px; height: 32px; border-radius: 50%;
  background: var(--surface2); border: 2px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; color: var(--text3);
  margin-left: -8px;
}

/* ── 일괄반영 진행 ── */
.apply-spinner {
  display: inline-block; width: 12px; height: 12px;
  border: 2px solid rgba(255,255,255,.3);
  border-top-color: white; border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.queue-item-processing {
  font-size: 11px; color: var(--warn);
  font-family: 'JetBrains Mono', monospace;
  margin-top: 4px;
}
.queue-item.done .queue-item-processing { color: var(--success); }
.queue-item.error .queue-item-processing { color: var(--danger); }

/* ── 이슈 그룹 섹션 ── */
.issue-group { margin-bottom: 6px; }
.issue-group-header {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 8px 5px 6px; border-radius: 6px;
  font-size: 11px; font-weight: 800;
  letter-spacing: .3px;
  border-bottom: none;
  margin-bottom: 4px; cursor: pointer; user-select: none;
  transition: filter .15s;
}
.issue-group-header:hover { filter: brightness(1.2); }
.issue-group-header .group-count {
  margin-left: auto;
  border-radius: 8px; padding: 1px 7px;
  font-size: 10px; font-weight: 900;
  font-family: 'JetBrains Mono', monospace;
}
.issue-group-header .group-toggle { font-size: 9px; margin-left: 2px; opacity: .7; }
.issue-group-body.collapsed { display: none; }

.issue-group.g-urgent  .issue-group-header { background: rgba(251,191,36,.12);  color: #fbbf24; }
.issue-group.g-urgent  .issue-group-header .group-count { background: rgba(251,191,36,.2); color: #fbbf24; }
.issue-group.g-overdue .issue-group-header { background: rgba(248,113,113,.12); color: #f87171; }
.issue-group.g-overdue .issue-group-header .group-count { background: rgba(248,113,113,.2); color: #f87171; }
.issue-group.g-new     .issue-group-header { background: rgba(6,182,212,.15);  color: #22d3ee; }
.issue-group.g-new     .issue-group-header .group-count { background: rgba(6,182,212,.25); color: #22d3ee; }
.issue-group.g-progress .issue-group-header { background: rgba(59,130,246,.15); color: #60a5fa; }
.issue-group.g-progress .issue-group-header .group-count { background: rgba(59,130,246,.25); color: #60a5fa; }
.issue-group.g-resolved .issue-group-header { background: rgba(16,185,129,.15); color: #34d399; }
.issue-group.g-resolved .issue-group-header .group-count { background: rgba(16,185,129,.25); color: #34d399; }
.issue-group.g-hold    .issue-group-header { background: rgba(99,102,241,.15);  color: #a78bfa; }
.issue-group.g-hold    .issue-group-header .group-count { background: rgba(99,102,241,.25); color: #a78bfa; }
.issue-group.g-closed  .issue-group-header { background: rgba(75,85,99,.12);    color: #6b7280; }
.issue-group.g-closed  .issue-group-header .group-count { background: rgba(75,85,99,.2); color: #6b7280; }

.issue-group.g-urgent  .issue-item { border-left: 3px solid #fbbf24; }
.issue-group.g-urgent  .issue-item .issue-subject { font-weight: 700; color: #f1f5f9; }
.issue-group.g-overdue .issue-item { border-left: 3px solid #f59e0b; }
.issue-group.g-overdue .issue-item .issue-subject { font-weight: 700; color: #f1f5f9; }
.issue-group.g-new     .issue-item { border-left: 3px solid #06b6d4; }
.issue-group.g-new     .issue-item .issue-subject { font-weight: 600; color: #e2e8f0; }
.issue-group.g-progress .issue-item { border-left: 3px solid #3b82f6; }
.issue-group.g-progress .issue-item .issue-subject { font-weight: 600; color: #e2e8f0; }
.issue-group.g-resolved .issue-item { border-left: 3px solid #10b981; }
.issue-group.g-resolved .issue-item .issue-subject { color: #94a3b8; }
.issue-group.g-hold    .issue-item { border-left: 3px solid #6366f1; opacity: .75; }
.issue-group.g-closed  .issue-item { border-left: 3px solid #374151; opacity: .45; }

/* ── Frosted Glass ── */
.chart-card, .queue-panel, .modal {
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}
.summary-card {
  background: rgba(255,255,255,.04) !important;
  border: 1px solid rgba(255,255,255,.08) !important;
}
.summary-card:hover {
  background: rgba(255,255,255,.07) !important;
  border-color: rgba(99,102,241,.4) !important;
}
.user-card {
  background: rgba(255,255,255,.03) !important;
  border: 1px solid rgba(255,255,255,.07) !important;
}
.user-card:hover {
  background: rgba(255,255,255,.06) !important;
  border-color: rgba(99,102,241,.5) !important;
  box-shadow: 0 0 30px rgba(99,102,241,.12) !important;
}
.chart-card {
  background: rgba(255,255,255,.03) !important;
  border: 1px solid rgba(255,255,255,.07) !important;
}
.modal {
  background: rgba(10,10,30,.85) !important;
  border: 1px solid rgba(255,255,255,.1) !important;
}
.queue-panel {
  background: rgba(10,10,30,.9) !important;
  border: 1px solid rgba(255,255,255,.1) !important;
}
.filter-bar input, .filter-bar select, .search-input, .filter-select {
  background: rgba(255,255,255,.05) !important;
  border: 1px solid rgba(255,255,255,.08) !important;
  color: var(--text) !important;
}
.filter-bar input:focus, .search-input:focus {
  border-color: rgba(99,102,241,.6) !important;
  box-shadow: 0 0 0 3px rgba(99,102,241,.12) !important;
}
.queue-item {
  background: rgba(255,255,255,.03) !important;
  border: 1px solid rgba(255,255,255,.07) !important;
}
.version-card {
  background: rgba(255,255,255,.03) !important;
  border: 1px solid rgba(255,255,255,.07) !important;
}
.version-card:hover {
  background: rgba(255,255,255,.06) !important;
  border-color: rgba(99,102,241,.4) !important;
}
.overdue-table thead tr { background: rgba(255,255,255,.04) !important; }
.overdue-table tbody tr:hover { background: rgba(99,102,241,.08) !important; }
select option { background: #1a1a2e; color: #f1f5f9; }
select option:hover, select option:checked { background: #6366f1; color: #fff; }

/* ── 그룹 컨테이너 ── */
.summary-group-wrap {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 20px 40px 24px;
  overflow: visible;
}
.summary-group {
  background: rgba(255,255,255,.03);
  border: 1px solid rgba(255,255,255,.07);
  border-radius: 16px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: border-color .2s;
}
.summary-group:hover { border-color: rgba(255,255,255,.13); }
.summary-group-title {
  font-size: 15px; font-weight: 800;
  letter-spacing: .5px;
  margin-bottom: 2px; padding-bottom: 10px;
  border-bottom: 1px solid rgba(255,255,255,.06);
  display: flex; align-items: center; gap: 8px;
}
.summary-group.g-planning { border-top: 2px solid #a78bfa; }
.summary-group.g-planning .summary-group-title { color: #a78bfa; }
.summary-group.g-server   { border-top: 2px solid #2dd4bf; }
.summary-group.g-server   .summary-group-title { color: #2dd4bf; }
.summary-group.g-client   { border-top: 2px solid #f472b6; }
.summary-group.g-client   .summary-group-title { color: #f472b6; }
.summary-group .summary-card {
  border-radius: 10px !important;
  border-color: rgba(255,255,255,.05) !important;
  background: rgba(255,255,255,.03) !important;
  padding: 10px 14px !important;
}
.summary-group .summary-card:hover {
  background: rgba(255,255,255,.07) !important;
  transform: none !important;
}
.summary-group .summary-card::before { display: none !important; }
.summary-group .summary-card.grp-planning,
.summary-group .summary-card.grp-server,
.summary-group .summary-card.grp-client { border-top: none !important; }
.summary-group .summary-card.pending { border-top: none !important; opacity: 1 !important; }
.summary-group .summary-card.pending .card-label { opacity: .75; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-logo">🎯</div>
    <div>
      <div class="header-title">RedRisk</div>
      <div class="header-sub" id="lastUpdated">데이터 로딩 중... 1분 안에 로딩될 거에요 🙂</div>
    </div>
  </div>
  <div class="header-right">
    <div class="live-dot" id="liveDot">LIVE</div>
    <div class="visitors-wrap" id="visitorsWrap" title="현재 접속자"></div>
    <div id="cacheStatus" style="font-size:11px;color:var(--text3);font-family:'JetBrains Mono',monospace;"></div>
    <button class="btn btn-ghost" onclick="loadData(true)" title="Redmine에서 강제로 새로 불러옴">⚡ 강제갱신</button>
    <button class="btn btn-primary" onclick="loadData(false)">🔄 새로고침</button>
    <button class="btn btn-queue" id="queueBtn" onclick="openQueuePanel()" style="display:none;">
      📋 반영 대기 <span id="queueCount" class="queue-badge">0</span>
    </button>
  </div>
</div>

<div class="control-panel">
  <div class="field">
    <label>📁 프로젝트</label>
    <select id="projectSelect" onchange="loadData(true)">
      <option value="">전체 프로젝트</option>
    </select>
  </div>
  <div class="field">
    <label>📅 업데이트 기준일</label>
    <input type="date" id="updatedAfter" value="2026-03-01" onchange="loadData(true)">
  </div>
  <!-- 위험 경보 배지 -->
  <div id="riskPanelBadge" onclick="switchTab('risk', document.getElementById('tabRiskBtn'))"
    style="margin-left:auto;display:none;cursor:pointer;
           padding:10px 20px;border-radius:12px;
           border:1px solid rgba(248,113,113,.3);
           background:rgba(248,113,113,.07);
           display:flex;align-items:center;gap:14px;
           transition:all .2s;"
    onmouseover="this.style.background='rgba(248,113,113,.14)'"
    onmouseout="this.style.background='rgba(248,113,113,.07)'">
    <div id="riskPanelIcon" style="font-size:28px;line-height:1;">🟢</div>
    <div>
      <div style="font-size:11px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">🚨 프로젝트 위험 경보</div>
      <div id="riskPanelLevel" style="font-size:22px;font-weight:900;letter-spacing:-0.5px;line-height:1;">-</div>
      <div id="riskPanelSub" style="font-size:11px;color:var(--text2);margin-top:4px;font-family:'JetBrains Mono',monospace;"></div>
    </div>
  </div>
</div>

<!-- 상단 5개 카드 -->
<div class="summary-grid">
  <!-- 참여 사용자 — 호버 툴팁 포함 -->
  <div class="summary-card blue user-count-card">
    <div class="card-icon">👥</div>
    <div class="card-label">참여 사용자</div>
    <div class="card-number blue" id="statUsers">-</div>
    <div class="user-tooltip" id="userTooltip">
      <div class="user-tooltip-title">
        <span>👥 참여 인원</span>
        <span class="user-tooltip-total" id="userTooltipTotal"></span>
      </div>
      <div id="userTooltipList">
        <span style="color:var(--text3);font-size:12px;">로딩 중...</span>
      </div>
    </div>
  </div>
  <div class="summary-card cyan">
    <div class="card-icon">📋</div>
    <div class="card-label">전체 이슈</div>
    <div class="card-number cyan" id="statTotal">-</div>
  </div>
  <div class="summary-card warn">
    <div class="card-icon">🔓</div>
    <div class="card-label">오픈 이슈</div>
    <div class="card-number warn" id="statOpen">-</div>
  </div>
  <div class="summary-card indigo" onclick="switchTab('versions', document.getElementById('tabVersionBtn'));" style="cursor:pointer;" title="버전별 일감 보기">
    <div class="card-icon">🗂️</div>
    <div class="card-label">버전별 일감 ↗</div>
    <div class="card-number indigo" id="statVersions">-</div>
    <div class="card-sub-label">활성 버전 수</div>
  </div>
  <div class="summary-card red" onclick="openOverdueTab()" style="cursor:pointer;" title="클릭하면 마감 초과 이슈 목록 보기">
    <div class="card-icon">⚠️</div>
    <div class="card-label" style="color:var(--danger);">마감 초과 (전체) ↗</div>
    <div class="card-number red" id="statOverdue">-</div>
  </div>

</div>

<!-- 하단 그룹 컨테이너 -->
<div class="summary-group-wrap">
  <div class="summary-group g-planning">
    <div class="summary-group-title">✏️ 기획</div>
    <div class="summary-card grp-planning" onclick="openOverdueTabFiltered('기획')" style="cursor:pointer;">
      <div class="card-label">기획 초과 ↗</div>
      <div class="card-number purple" id="statOverduePlanning">-</div>
      <div class="card-sub-label">마감 초과</div>
    </div>
    <div class="summary-card grp-planning pending" onclick="openPendingTabFiltered('기획')" style="cursor:pointer;">
      <div class="card-label">진행대기 ↗</div>
      <div class="card-number" style="color:#a78bfa;" id="statPendingPlanning">-</div>
      <div class="card-sub-label">진행대기</div>
    </div>
  </div>
  <div class="summary-group g-server">
    <div class="summary-group-title">🖥️ 서버</div>
    <div class="summary-card grp-server" onclick="openOverdueTabFiltered('서버')" style="cursor:pointer;">
      <div class="card-label">서버 초과 ↗</div>
      <div class="card-number teal" id="statOverdueServer">-</div>
      <div class="card-sub-label">마감 초과</div>
    </div>
    <div class="summary-card grp-server pending" onclick="openPendingTabFiltered('서버')" style="cursor:pointer;">
      <div class="card-label">진행대기 ↗</div>
      <div class="card-number" style="color:#2dd4bf;" id="statPendingServer">-</div>
      <div class="card-sub-label">진행대기</div>
    </div>
  </div>
  <div class="summary-group g-client">
    <div class="summary-group-title">📱 클라이언트</div>
    <div class="summary-card grp-client" onclick="openOverdueTabFiltered('클라')" style="cursor:pointer;">
      <div class="card-label">클라 초과 ↗</div>
      <div class="card-number pink" id="statOverdueClient">-</div>
      <div class="card-sub-label">마감 초과</div>
    </div>
    <div class="summary-card grp-client pending" onclick="openPendingTabFiltered('클라')" style="cursor:pointer;">
      <div class="card-label">진행대기 ↗</div>
      <div class="card-number" style="color:#f472b6;" id="statPendingClient">-</div>
      <div class="card-sub-label">진행대기</div>
    </div>
  </div>
</div>

<div class="content">
  <div class="tabs">
    <button class="tab active" id="tabRiskBtn" onclick="switchTab('risk', this);">🚨 위험 경보 <span id="tabRiskCnt" style="display:none;background:rgba(251,146,60,.2);color:#fb923c;font-size:10px;font-weight:700;padding:1px 6px;border-radius:8px;margin-left:4px;font-family:'JetBrains Mono',monospace;"></span></button>
    <button class="tab" onclick="switchTab('users', this)">👤 담당자별</button>
    <button class="tab tab-danger" id="tabDeadlineBtn" onclick="switchTab('deadline', this)">⚠ 마감 관리 <span id="tabDeadlineCnt" class="tab-danger-badge"></span></button>
    <button class="tab" onclick="switchTab('charts', this)">📊 차트</button>
    <button class="tab" id="tabVersionBtn" onclick="switchTab('versions', this);">🗂️ 버전 <span id="tabVersionOverdueCnt" style="display:none;background:rgba(248,113,113,.2);color:#f87171;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px;"></span></button>
  </div>

  <!-- 담당자별 탭 -->
  <div id="tab-users" class="tab-content">
    <div id="usersListView">
      <div class="filter-bar">
        <input class="search-input" type="text" placeholder="🔍 담당자 검색..." oninput="filterUsers(this.value)">
        <select class="filter-select" id="deptFilter" onchange="filterUsers(document.querySelector('.search-input').value)">
          <option value="">전체 그룹</option>
        </select>
        <span class="count-badge" id="visibleCount"></span>
      </div>
      <div class="users-grid" id="usersGrid">
        <div class="loading" id="staticMainLoading"></div>
      </div>
    </div>
    <div id="userDetailView" style="display:none;">
      <div class="version-detail-header">
        <button class="btn btn-ghost" onclick="closeUserDetail()" style="margin-bottom:16px;">← 전체 인원으로 돌아가기</button>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;">
          <div class="avatar" id="userDetailAvatar" style="width:48px;height:48px;font-size:18px;"></div>
          <div>
            <div id="userDetailName" style="font-size:22px;font-weight:700;"></div>
            <div id="userDetailDept" style="font-size:12px;color:var(--text3);"></div>
          </div>
          <div style="margin-left:auto;display:flex;gap:20px;text-align:center;">
            <div><div id="userDetailTotal" style="font-size:24px;font-weight:700;">-</div><div style="font-size:11px;color:var(--text3);">전체</div></div>
            <div><div id="userDetailOpen"  style="font-size:24px;font-weight:700;color:var(--warn);">-</div><div style="font-size:11px;color:var(--text3);">오픈</div></div>
            <div><div id="userDetailOver"  style="font-size:24px;font-weight:700;color:var(--danger);">-</div><div style="font-size:11px;color:var(--text3);">초과</div></div>
            <div><div id="userDetailDone"  style="font-size:24px;font-weight:700;color:var(--success);">-</div><div style="font-size:11px;color:var(--text3);">해결</div></div>
          </div>
        </div>
      </div>
      <div id="userDetailIssues"></div>
    </div>
  </div>

  <!-- 진행대기 탭 -->
  <!-- 마감 관리 통합 탭 (마감초과 + 진행대기) -->
  <div id="tab-deadline" class="tab-content">
    <!-- 서브탭 -->
    <div style="display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:0;">
      <button class="tab active" id="subTabOverdueBtn" onclick="switchDeadlineSubTab('overdue', this)" style="font-size:12px;padding:7px 16px;">🔴 마감 초과 <span id="tabOverdueCnt" class="tab-danger-badge"></span></button>
      <button class="tab" id="subTabPendingBtn" onclick="switchDeadlineSubTab('pending', this)" style="font-size:12px;padding:7px 16px;">⏸ 진행대기 <span id="tabPendingCnt" class="tab-badge" style="background:rgba(99,102,241,.2);color:#818cf8;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px;"></span></button>
    </div>

    <!-- 마감 초과 서브탭 -->
    <div id="sub-overdue" class="deadline-sub active">
      <div class="overdue-header">
        <span>⚠ 마감일을 초과한 이슈 목록입니다. 즉시 확인이 필요합니다.</span>
        <span id="overdueCount" class="overdue-total"></span>
      </div>
      <div class="filter-bar">
        <input class="search-input" type="text" placeholder="🔍 제목 / 담당자 검색..." oninput="filterOverdue(this.value)">
        <select class="filter-select" id="overdueDeptFilter" onchange="filterOverdue(document.querySelector('#sub-overdue .search-input').value)">
          <option value="">전체 그룹</option>
        </select>
      </div>
      <div id="overdueList"></div>
    </div>

    <!-- 진행대기 서브탭 -->
    <div id="sub-pending" class="deadline-sub" style="display:none;">
      <div class="overdue-header">
        <span>⏸ 진행대기 상태인 이슈 목록입니다.</span>
        <span id="pendingCount" class="overdue-total"></span>
      </div>
      <div class="filter-bar">
        <input class="search-input" type="text" placeholder="🔍 제목 / 담당자 검색..." oninput="filterPending(this.value)">
        <select class="filter-select" id="pendingDeptFilter" onchange="filterPending(document.querySelector('#sub-pending .search-input').value)">
          <option value="">전체 그룹</option>
        </select>
        <span class="count-badge" id="pendingVisibleCount"></span>
      </div>
      <table class="overdue-table">
        <thead>
          <tr><th>#</th><th>제목</th><th>담당자</th><th>그룹</th><th>마감일</th><th>D-Day</th></tr>
        </thead>
        <tbody id="pendingTableBody"></tbody>
      </table>
    </div>
  </div>

  <!-- 차트 통합 탭 -->
  <div id="tab-charts" class="tab-content">
    <!-- 서브탭 -->
    <div style="display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:0;">
      <button class="tab active" onclick="switchChartSubTab('global', this)" style="font-size:12px;padding:7px 16px;">📊 전체 차트</button>
      <button class="tab" onclick="switchChartSubTab('group', this)" style="font-size:12px;padding:7px 16px;">🏢 그룹 차트</button>
    </div>

    <!-- 전체 차트 -->
    <div id="sub-charts-global" class="chart-sub">
      <div class="charts-grid">
        <div class="chart-card"><div class="chart-title">상태별 이슈 분포</div><canvas id="chartStatus" height="250"></canvas></div>
        <div class="chart-card"><div class="chart-title">그룹별 오픈 이슈 TOP 10</div><canvas id="chartDept" height="250"></canvas></div>
        <div class="chart-card"><div class="chart-title">우선순위별 분포</div><canvas id="chartPriority" height="250"></canvas></div>
        <div class="chart-card"><div class="chart-title">담당자별 이슈 TOP 10</div><canvas id="chartUser" height="250"></canvas></div>
      </div>
    </div>

    <!-- 그룹 차트 -->
    <div id="sub-charts-group" class="chart-sub" style="display:none;">
      <div class="filter-bar" style="margin-bottom:20px;">
        <select class="filter-select" id="chartDeptFilter" onchange="renderGroupCharts(this.value)">
          <option value="">── 그룹 선택 ──</option>
        </select>
        <span class="count-badge" id="groupChartTitle"></span>
      </div>
      <div id="groupChartSection">
        <div class="empty" style="padding:60px;">🏢 위에서 그룹을 선택해주세요</div>
      </div>
    </div>
  </div>

  <!-- 위험 경보 탭 -->
  <div id="tab-risk" class="tab-content active">
    <!-- 카드 목록 뷰 -->
    <div id="riskListView">
      <div class="risk-header">
        <div>
          <div style="font-size:18px;font-weight:700;margin-bottom:4px;">🚨 프로젝트 위험 경보</div>
          <div style="font-size:12px;color:var(--text2);line-height:1.6;">📐 점수 산출 기준: <span style="color:#f87171;font-weight:700;">초과 ×60%</span> + <span style="color:#fbbf24;font-weight:700;">임박 ×30%</span> + <span style="color:#818cf8;font-weight:700;">대기 ×10%</span> &nbsp;·&nbsp; 카드 클릭 시 전체 이슈 확인 가능</div>
        </div>
        <div style="display:flex;gap:16px;align-items:center;">
          <div class="risk-legend"><span style="color:#f87171;">🔴 Critical</span> 30점↑</div>
          <div class="risk-legend"><span style="color:#fb923c;">🟠 High</span> 15점↑</div>
          <div class="risk-legend"><span style="color:#fbbf24;">⚠️ Medium</span> 5점↑</div>
          <div class="risk-legend"><span style="color:#34d399;">🟢 Low</span> 5점↓</div>
        </div>
      </div>
      <div id="riskGrid" class="risk-grid"></div>
    </div>
    <!-- 상세 뷰 -->
    <div id="riskDetailView" style="display:none;">
      <div class="version-detail-header">
        <button class="btn btn-ghost" onclick="closeRiskDetail()" style="margin-bottom:16px;">← 위험 경보 목록으로</button>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;">
          <div>
            <div id="riskDetailTitle" style="font-size:22px;font-weight:700;"></div>
            <div id="riskDetailLevel" style="font-size:13px;margin-top:4px;"></div>
          </div>
          <div style="margin-left:auto;display:flex;gap:20px;text-align:center;">
            <div><div id="riskDetailScore" style="font-size:28px;font-weight:900;font-family:'JetBrains Mono',monospace;">-</div><div style="font-size:11px;color:var(--text3);">위험점수</div></div>
            <div><div id="riskDetailOverdue" style="font-size:24px;font-weight:700;color:#f87171;">-</div><div style="font-size:11px;color:var(--text3);">초과</div></div>
            <div><div id="riskDetailUrgent" style="font-size:24px;font-weight:700;color:#fbbf24;">-</div><div style="font-size:11px;color:var(--text3);">임박</div></div>
            <div><div id="riskDetailPending" style="font-size:24px;font-weight:700;color:#818cf8;">-</div><div style="font-size:11px;color:var(--text3);">진행대기</div></div>
          </div>
        </div>
        <!-- 필터 -->
        <div class="filter-bar">
          <input class="search-input" type="text" id="riskDetailSearch" placeholder="🔍 제목 / 담당자 검색..." oninput="filterRiskDetail()">
          <select class="filter-select" id="riskDetailTypeFilter" onchange="filterRiskDetail()">
            <option value="">전체 유형</option>
            <option value="overdue">🔴 마감 초과</option>
            <option value="urgent">⚠️ 마감 임박</option>
            <option value="pending">⏸ 진행대기</option>
          </select>
          <span class="count-badge" id="riskDetailCount"></span>
        </div>
      </div>
      <table class="overdue-table">
        <thead>
          <tr><th>#</th><th>제목</th><th>담당자</th><th>유형</th><th>상태</th><th>우선순위</th><th>마감일</th><th>D-Day</th></tr>
        </thead>
        <tbody id="riskDetailTableBody"></tbody>
      </table>
    </div>
  </div>

  <!-- 버전별 일감 탭 -->
  <div id="tab-versions" class="tab-content">
    <div id="versionCards" class="version-cards-grid">
      <div class="loading" id="staticVersionLoading"></div>
    </div>
    <div id="versionDetail" style="display:none;">
      <div class="version-detail-header">
        <button class="btn btn-ghost" onclick="closeVersionDetail()" style="margin-bottom:16px;">← 버전 목록으로</button>
        <div id="versionDetailTitle" style="font-size:18px;font-weight:700;margin-bottom:4px;"></div>
        <div id="versionDetailMeta" style="font-size:12px;color:var(--text3);margin-bottom:16px;"></div>
      </div>
      <div class="filter-bar">
        <input class="search-input" type="text" id="versionSearch" placeholder="🔍 제목 / 담당자 검색..." oninput="filterVersionIssues()">
        <select class="filter-select" id="versionStatusFilter" onchange="filterVersionIssues()">
          <option value="">전체 상태</option>
          <option value="open">오픈만</option>
          <option value="overdue">마감초과만</option>
        </select>
        <span class="count-badge" id="versionIssueCount"></span>
      </div>
      <div id="versionIssueList"></div>
    </div>
  </div>
</div>

<!-- 이슈 편집 모달 -->
<div class="modal-backdrop" id="issueModal" onclick="if(event.target===this)closeIssueModal()">
  <div class="modal">
    <div class="modal-header">
      <div>
        <div class="modal-issue-id" id="modalIssueId"></div>
        <div class="modal-title" id="modalIssueTitle"></div>
      </div>
      <button class="modal-close" onclick="closeIssueModal()">✕</button>
    </div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="modal-field"><label>📌 상태</label><select id="modalStatus"></select></div>
        <div class="modal-field"><label>👤 담당자</label><select id="modalAssignee"></select></div>
        <div class="modal-field"><label>📅 마감일</label><input type="date" id="modalDueDate"></div>
        <div class="modal-field"><label>🏷 버전</label><select id="modalVersion"></select></div>
      </div>
      <div class="modal-field"><label>💬 코멘트</label><textarea id="modalComment" placeholder="코멘트를 입력하세요..."></textarea></div>
    </div>
    <div class="modal-footer">
      <a id="modalRedmineLink" href="#" target="_blank" class="modal-link">🔗 Redmine에서 열기</a>
      <button class="btn btn-ghost" onclick="closeIssueModal()">취소</button>
      <button class="btn modal-save-btn" id="modalSaveBtn" onclick="addToQueue()">➕ 큐에 추가</button>
    </div>
    <div class="modal-journals" id="modalJournals" style="display:none;">
      <div class="modal-journals-title">💬 최근 코멘트</div>
      <div id="modalJournalList"></div>
    </div>
  </div>
</div>

<!-- 변경 큐 패널 -->
<div class="queue-panel-backdrop" id="queuePanel" onclick="if(event.target===this)closeQueuePanel()">
  <div class="queue-panel">
    <div class="queue-panel-header">
      <div class="queue-panel-title">📋 반영 대기 목록</div>
      <button class="modal-close" onclick="closeQueuePanel()">✕</button>
    </div>
    <div class="queue-panel-body" id="queuePanelBody">
      <div class="empty">대기 중인 변경사항이 없습니다</div>
    </div>
    <div class="queue-panel-footer">
      <span id="queueStatus" style="font-size:12px;color:var(--text3);margin-right:auto;font-family:'JetBrains Mono',monospace;"></span>
      <button class="btn btn-ghost" onclick="closeQueuePanel()">닫기</button>
      <button class="btn btn-ghost" onclick="clearQueue()" id="queueClearBtn">🗑 전체 취소</button>
      <button class="btn btn-apply" id="queueApplyBtn" onclick="applyQueue()">⚡ 일괄 반영</button>
    </div>
  </div>
</div>

<script>
let allUsersData = {};
let charts = {};

const CLOSED_SET   = new Set(["완료","완료(잔땡처리)","Closed","반려","Rejected"]);
const RESOLVED_SET = new Set(["해결","해결됨","Resolved"]);
const HOLD_SET_JS  = new Set(["보류","보류(스펙아웃)","스펙아웃"]);
const PROGRESS_SET = new Set(["진행","진행대기","In Progress"]);

const GROUP_META_JS = [
  { key: 'urgent',   label: '🚨 마감 임박 (3일 이내)' },
  { key: 'overdue',  label: '⚠ 일정 초과'            },
  { key: 'new',      label: '🆕 신규 발급 (이번 주)'  },
  { key: 'progress', label: '▶ 진행중'               },
  { key: 'resolved', label: '✅ 해결'                 },
  { key: 'hold',     label: '⏸ 보류'                 },
  { key: 'closed',   label: '⬜ 완료'                 },
];

const DEPT_ORDER = ['기획', 'PM', 'UI', '서버', '클라'];
const DEPT_COLOR_MAP = {
  '기획': 'dept-color-기획',
  'PM':   'dept-color-PM',
  'UI':   'dept-color-UI',
  '서버': 'dept-color-서버',
  '클라': 'dept-color-클라',
};

function classifyIssues(issues) {
  const today = new Date().toISOString().slice(0,10);
  const d = new Date(); d.setDate(d.getDate() - d.getDay() + 1);
  const weekStart = d.toISOString().slice(0,10);
  const groups = {};
  GROUP_META_JS.forEach(g => groups[g.key] = []);
  issues.forEach(i => {
    const s  = i.status, dd = i.due_date || '';
    const cr = (i.created_on||'').slice(0,10);
    const diff = dd ? Math.ceil((new Date(dd)-new Date(today))/86400000) : null;
    if (cr >= weekStart)                                groups['new'].push(i);
    else if (CLOSED_SET.has(s))                         groups['closed'].push(i);
    else if (RESOLVED_SET.has(s))                       groups['resolved'].push(i);
    else if (HOLD_SET_JS.has(s))                        groups['hold'].push(i);
    else if (PROGRESS_SET.has(s) && dd && dd < today)   groups['overdue'].push(i);
    else if (diff !== null && diff >= 0 && diff <= 3)   groups['urgent'].push(i);
    else                                                groups['progress'].push(i);
  });
  return groups;
}

function renderGroupedIssues(issues) {
  const groups = classifyIssues(issues);
  let html = '';
  GROUP_META_JS.forEach(g => {
    const list = groups[g.key];
    if (!list.length) return;
    const rows = list.map(i => `
      <div class="issue-item" style="padding-left:8px;">
        <span class="issue-id">#${i.id}</span>
        <span class="issue-subject" onclick="event.stopPropagation();openIssueModal(${i.id})" style="cursor:pointer;" title="클릭하여 편집">${i.subject}</span>
        <span class="badge ${getBadgeClass(i.status)}">${i.status}</span>
        ${getDday(i.due_date, i.status)}
      </div>`).join('');
    html += `
    <div class="issue-group g-${g.key}">
      <div class="issue-group-header" onclick="this.nextElementSibling.classList.toggle('collapsed');this.querySelector('.group-toggle').textContent=this.nextElementSibling.classList.contains('collapsed')?'▶':'▼';">
        <span>${g.label}</span>
        <span class="group-count">${list.length}</span>
        <span class="group-toggle">▼</span>
      </div>
      <div class="issue-group-body">${rows}</div>
    </div>`;
  });
  return html || '<div class="empty">이슈 없음 ✅</div>';
}

function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

function openOverdueTab() {
  switchTab('deadline', document.getElementById('tabDeadlineBtn'));
  switchDeadlineSubTab('overdue', document.getElementById('subTabOverdueBtn'));
  document.getElementById('overdueDeptFilter').value = '';
  document.querySelector('#sub-overdue .search-input').value = '';
  renderOverdueTable(overdueIssues);
  document.getElementById('overdueCount').textContent = '총 ' + overdueIssues.length + '건';
}

function openOverdueTabFiltered(dept) {
  switchTab('deadline', document.getElementById('tabDeadlineBtn'));
  switchDeadlineSubTab('overdue', document.getElementById('subTabOverdueBtn'));
  document.querySelector('#sub-overdue .search-input').value = '';
  const sel = document.getElementById('overdueDeptFilter');
  let matched = '';
  for (let opt of sel.options) {
    if (opt.value === dept) { matched = opt.value; break; }
  }
  sel.value = matched;
  filterOverdue('', dept);
}

function switchDeadlineSubTab(name, el) {
  document.querySelectorAll('#tab-deadline .tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.deadline-sub').forEach(t => t.style.display = 'none');
  el.classList.add('active');
  document.getElementById('sub-' + name).style.display = 'block';
}

function switchChartSubTab(name, el) {
  document.querySelectorAll('#tab-charts .tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.chart-sub').forEach(t => t.style.display = 'none');
  el.classList.add('active');
  document.getElementById('sub-charts-' + name).style.display = 'block';
}

let overdueIssues = [];
let _overdueDeptBuilt = false;

function renderOverdueList(issues) {
  overdueIssues = issues;
  if (!_overdueDeptBuilt) {
    const allDepts = new Set(Object.keys(allUsersData).map(name => deptName(name)));
    const sel = document.getElementById('overdueDeptFilter');
    sel.innerHTML = '<option value="">전체 그룹</option>';
    [...allDepts].sort().forEach(d => {
      const opt = document.createElement('option');
      opt.value = d; opt.textContent = d;
      sel.appendChild(opt);
    });
    _overdueDeptBuilt = true;
  }
  document.getElementById('tabOverdueCnt').textContent = issues.length;
  renderOverdueTable(issues);
  document.getElementById('overdueCount').textContent = '총 ' + issues.length + '건';
}

function renderOverdueTable(issues) {
  const html = `
  <table class="overdue-table">
    <thead><tr>
      <th>#</th><th>제목</th><th>담당자</th><th>그룹</th>
      <th>상태</th><th>우선순위</th><th>마감일</th><th>초과일수</th>
    </tr></thead>
    <tbody>
    ${issues.length === 0
      ? `<tr><td colspan="8" class="empty" style="padding:32px;font-size:14px;">✅ 마감 초과 일감이 없습니다</td></tr>`
      : issues.map(i => {
          const diff = Math.round((new Date() - new Date(i.due_date)) / 86400000);
          const dept = deptName(i.assignee);
          return `<tr data-assignee="${i.assignee.toLowerCase()}" data-subject="${i.subject.toLowerCase()}" data-dept="${dept}">
            <td><span class="overdue-id" onclick="openIssueModal(${i.id})" style="cursor:pointer;">#${i.id}</span></td>
            <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="openIssueModal(${i.id})">${i.subject}</td>
            <td>${shortName(i.assignee)}</td>
            <td>${dept}</td>
            <td><span class="badge ${getBadgeClass(i.status)}">${i.status}</span></td>
            <td>${i.priority}</td>
            <td style="font-family:'JetBrains Mono',monospace;font-size:12px;">${i.due_date}</td>
            <td><span class="overdue-dday">D+${diff}</span></td>
          </tr>`;
        }).join('')}
    </tbody>
  </table>`;
  document.getElementById('overdueList').innerHTML = html;
}

function filterOverdue(search, deptOverride) {
  const dept = (deptOverride !== undefined) ? deptOverride : document.getElementById('overdueDeptFilter').value;
  const q = search.toLowerCase();
  const filtered = overdueIssues.filter(i => {
    const matchSearch = !q || i.subject.toLowerCase().includes(q) || i.assignee.toLowerCase().includes(q);
    const matchDept   = !dept || deptName(i.assignee) === dept;
    return matchSearch && matchDept;
  });
  document.getElementById('overdueCount').textContent = `총 ${filtered.length}건`;
  if (filtered.length === 0) {
    document.getElementById('overdueList').innerHTML =
      `<div class="empty" style="padding:48px;font-size:14px;text-align:center;">✅ 초과된 일감이 없습니다</div>`;
    return;
  }
  renderOverdueTable(filtered);
}

// ==================== 참여 사용자 툴팁 ====================
function updateUserTooltip(data) {
  const tooltipList  = document.getElementById('userTooltipList');
  const tooltipTotal = document.getElementById('userTooltipTotal');
  if (!tooltipList) return;

  const grouped = {};
  Object.entries(data).forEach(([name, ud]) => {
    const dept = deptName(name);
    if (!grouped[dept]) grouped[dept] = [];
    const openCnt = ud.issues.filter(i => !CLOSED_SET.has(i.status)).length;
    grouped[dept].push({ name, openCnt });
  });

  const totalCount = Object.keys(data).length;
  if (tooltipTotal) tooltipTotal.textContent = `총 ${totalCount}명`;

  const sortedDepts = [
    ...DEPT_ORDER.filter(d => grouped[d]),
    ...Object.keys(grouped).filter(d => !DEPT_ORDER.includes(d)).sort()
  ];

  const deptColorStyle = {
    '기획':       { border: '#a78bfa', text: '#a78bfa', bg: 'rgba(167,139,250,.15)' },
    'PM':         { border: '#fbbf24', text: '#fbbf24', bg: 'rgba(251,191,36,.15)'  },
    'UI':         { border: '#f472b6', text: '#f472b6', bg: 'rgba(244,114,182,.15)' },
    '서버':       { border: '#2dd4bf', text: '#2dd4bf', bg: 'rgba(45,212,191,.15)'  },
    '클라':       { border: '#f87171', text: '#f87171', bg: 'rgba(248,113,113,.15)' },
    'ACT':        { border: '#fb923c', text: '#fb923c', bg: 'rgba(251,146,60,.15)'  },
    'ART':        { border: '#34d399', text: '#34d399', bg: 'rgba(52,211,153,.15)'  },
    'QA':         { border: '#60a5fa', text: '#60a5fa', bg: 'rgba(96,165,250,.15)'  },
    '배경(3D)':   { border: '#c084fc', text: '#c084fc', bg: 'rgba(192,132,252,.15)' },
    '배경(컨셉)': { border: '#e879f9', text: '#e879f9', bg: 'rgba(232,121,249,.15)' },
    '사운드':     { border: '#4ade80', text: '#4ade80', bg: 'rgba(74,222,128,.15)'  },
    '아트전용':   { border: '#f9a8d4', text: '#f9a8d4', bg: 'rgba(249,168,212,.15)' },
    '애니':       { border: '#fde68a', text: '#fde68a', bg: 'rgba(253,230,138,.15)' },
    '캐릭터(3D)': { border: '#6ee7b7', text: '#6ee7b7', bg: 'rgba(110,231,183,.15)' },
    '캐릭터(컨셉)':{ border: '#a5f3fc', text: '#a5f3fc', bg: 'rgba(165,243,252,.15)'},
  };
  // 색상 팔레트 (알 수 없는 그룹용 순환)
  const extraColors = [
    { border: '#818cf8', text: '#818cf8', bg: 'rgba(129,140,248,.15)' },
    { border: '#fb7185', text: '#fb7185', bg: 'rgba(251,113,133,.15)' },
    { border: '#38bdf8', text: '#38bdf8', bg: 'rgba(56,189,248,.15)'  },
    { border: '#a3e635', text: '#a3e635', bg: 'rgba(163,230,53,.15)'  },
    { border: '#fdba74', text: '#fdba74', bg: 'rgba(253,186,116,.15)' },
  ];
  let extraIdx = 0;
  const getDeptColor = (dept) => {
    if (deptColorStyle[dept]) return deptColorStyle[dept];
    // 알 수 없는 그룹은 순환 색상 부여 (같은 그룹은 항상 같은 색)
    const keys = Object.keys(deptColorStyle);
    const hash = dept.split('').reduce((a,c) => a + c.charCodeAt(0), 0);
    return extraColors[hash % extraColors.length];
  };

  const rows = sortedDepts.map(dept => {
    const c = getDeptColor(dept);
    const members = grouped[dept].sort((a,b) => a.name.localeCompare(b.name, 'ko'));
    const memberHtml = members.map(m => `
      <div style="display:flex;flex-direction:column;align-items:center;gap:2px;background:rgba(255,255,255,.04);border:1px solid ${c.border}44;border-left:3px solid ${c.border};border-radius:6px;padding:4px 8px;min-width:58px;">
        <span style="font-size:12px;font-weight:700;color:${c.text};white-space:nowrap;">${shortName(m.name)}</span>
        <span style="font-size:10px;font-weight:700;color:white;background:${c.border}55;padding:0 5px;border-radius:4px;">${m.openCnt}건</span>
      </div>`).join('');
    return `
      <div class="user-tooltip-row">
        <div class="user-tooltip-row-label" style="background:${c.bg};color:${c.text};border:1px solid ${c.border}55;" title="${dept}">${dept}</div>
        <div class="user-tooltip-row-members">${memberHtml}</div>
      </div>`;
  }).join('');

  tooltipList.innerHTML = `<div class="user-tooltip-grid">${rows}</div>`;
}

// ==================== 이슈 편집 모달 ====================
let _currentIssueId = null;
let _currentIssueData = null;

async function openIssueModal(issueId) {
  _currentIssueId = issueId;
  const modal = document.getElementById('issueModal');
  modal.classList.add('open');
  document.getElementById('modalIssueId').textContent = '#' + issueId;
  document.getElementById('modalIssueTitle').textContent = '불러오는 중...';
  document.getElementById('modalStatus').innerHTML = '';
  document.getElementById('modalAssignee').innerHTML = '';
  document.getElementById('modalVersion').innerHTML = '';
  document.getElementById('modalDueDate').value = '';
  document.getElementById('modalComment').value = '';
  document.getElementById('modalJournals').style.display = 'none';
  document.getElementById('modalSaveBtn').disabled = false;
  document.getElementById('modalSaveBtn').textContent = '➕ 큐에 추가';
  document.getElementById('modalRedmineLink').href = 'https://redmine.wemadenext.com/issues/' + issueId;

  if (issueQueue.find(q => q.id === issueId)) {
    document.getElementById('modalSaveBtn').textContent = '✏️ 큐 업데이트';
  }

  try {
    const res = await fetch(`/api/issue/${issueId}`);
    const data = await res.json();
    const issue = data.issue;
    const statuses = data.statuses;
    const assignees = data.assignees || [];
    const versions  = data.versions  || [];
    _currentIssueData = issue;

    document.getElementById('modalIssueId').textContent = '#' + issueId + ' · ' + (issue.project?.name || '');
    document.getElementById('modalIssueTitle').textContent = issue.subject || '';
    document.getElementById('modalDueDate').value = issue.due_date || '';

    const sel = document.getElementById('modalStatus');
    sel.innerHTML = '';
    statuses.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id; opt.textContent = s.name;
      if (s.name === issue.status?.name) opt.selected = true;
      sel.appendChild(opt);
    });

    const asel = document.getElementById('modalAssignee');
    asel.innerHTML = '<option value="">-- 담당자 없음 --</option>';
    const memberMap = new Map();
    assignees.forEach(a => memberMap.set(a.name, a));
    Object.keys(allUsersData).forEach(n => {
      if (!memberMap.has(n)) memberMap.set(n, { id: null, name: n });
    });
    if (issue.assigned_to && !memberMap.has(issue.assigned_to.name)) {
      memberMap.set(issue.assigned_to.name, { id: issue.assigned_to.id, name: issue.assigned_to.name });
    }
    [...memberMap.values()]
      .sort((a, b) => a.name.localeCompare(b.name, 'ko'))
      .forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.id ?? a.name;
        opt.textContent = shortName(a.name);
        opt.dataset.fullname = a.name;
        if (a.id && a.id === issue.assigned_to?.id) opt.selected = true;
        else if (!a.id && a.name === issue.assigned_to?.name) opt.selected = true;
        asel.appendChild(opt);
      });

    const vsel = document.getElementById('modalVersion');
    vsel.innerHTML = '<option value="">-- 버전 없음 --</option>';
    const versionList = allVersionData.length > 0
      ? allVersionData.map(v => ({ id: v.id, name: v.name }))
      : versions;
    versionList.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v.id; opt.textContent = v.name;
      if (v.id === issue.fixed_version?.id) opt.selected = true;
      vsel.appendChild(opt);
    });

    const journals = (issue.journals || []).filter(j => j.notes?.trim());
    if (journals.length) {
      document.getElementById('modalJournals').style.display = '';
      document.getElementById('modalJournalList').innerHTML = journals.slice(-5).reverse().map(j => `
        <div class="journal-item">
          <div class="journal-meta">${j.user?.name || '?'} · ${j.created_on?.slice(0,16).replace('T',' ') || ''}</div>
          <div class="journal-note">${j.notes}</div>
        </div>`).join('');
    }
  } catch(e) {
    document.getElementById('modalIssueTitle').textContent = '❌ 불러오기 실패: ' + e.message;
  }
}

function closeIssueModal() {
  document.getElementById('issueModal').classList.remove('open');
  _currentIssueId = null;
  _currentIssueData = null;
}

function addToQueue() {
  if (!_currentIssueId || !_currentIssueData) return;
  const statusSel   = document.getElementById('modalStatus');
  const assigneeSel = document.getElementById('modalAssignee');
  const versionSel  = document.getElementById('modalVersion');
  const entry = {
    id:               _currentIssueId,
    title:            _currentIssueData.subject,
    status_id:        parseInt(statusSel.value),
    status_name:      statusSel.options[statusSel.selectedIndex]?.text || '',
    orig_status:      _currentIssueData.status?.name || '',
    assigned_to_id:   (assigneeSel.value && !isNaN(assigneeSel.value)) ? parseInt(assigneeSel.value) : null,
    assignee_name:    assigneeSel.options[assigneeSel.selectedIndex]?.text || '',
    orig_assignee:    _currentIssueData.assigned_to ? shortName(_currentIssueData.assigned_to.name) : '없음',
    fixed_version_id: versionSel.value ? parseInt(versionSel.value) : null,
    version_name:     versionSel.options[versionSel.selectedIndex]?.text || '',
    orig_version:     _currentIssueData.fixed_version?.name || '없음',
    due_date:         document.getElementById('modalDueDate').value || null,
    orig_due:         _currentIssueData.due_date || null,
    notes:            document.getElementById('modalComment').value.trim(),
  };
  const idx = issueQueue.findIndex(q => q.id === entry.id);
  if (idx >= 0) issueQueue[idx] = entry;
  else issueQueue.push(entry);
  updateQueueBadge();
  closeIssueModal();
  showToast(`#${entry.id} 큐에 추가됨 (총 ${issueQueue.length}건)`);
}

// ==================== 변경 큐 ====================
let issueQueue = [];

function updateQueueBadge() {
  const btn = document.getElementById('queueBtn');
  const cnt = document.getElementById('queueCount');
  if (issueQueue.length > 0) {
    btn.style.display = '';
    cnt.textContent = issueQueue.length;
  } else {
    btn.style.display = 'none';
  }
}

function openQueuePanel() { renderQueuePanel(); document.getElementById('queuePanel').classList.add('open'); }
function closeQueuePanel() { document.getElementById('queuePanel').classList.remove('open'); }

function renderQueuePanel() {
  const body = document.getElementById('queuePanelBody');
  if (!issueQueue.length) { body.innerHTML = '<div class="empty">대기 중인 변경사항이 없습니다</div>'; return; }
  body.innerHTML = issueQueue.map((q, i) => {
    const changes = [];
    if (q.status_name !== q.orig_status) changes.push(`📌 ${q.orig_status} → ${q.status_name}`);
    if (q.assignee_name !== q.orig_assignee) changes.push(`👤 ${q.orig_assignee} → ${q.assignee_name}`);
    if (q.version_name !== q.orig_version) changes.push(`🏷 ${q.orig_version} → ${q.version_name}`);
    if (q.due_date !== q.orig_due) changes.push(`📅 ${q.orig_due || '없음'} → ${q.due_date || '없음'}`);
    if (q.notes) changes.push(`💬 코멘트 있음`);
    if (!changes.length) changes.push('변경 없음');
    return `
    <div class="queue-item" id="qitem-${q.id}">
      <div class="queue-item-info">
        <div class="queue-item-id">#${q.id}</div>
        <div class="queue-item-title">${q.title}</div>
        <div class="queue-item-changes">${changes.map(c => `<span class="queue-change-tag">${c}</span>`).join('')}</div>
      </div>
      <button class="queue-item-remove" onclick="removeFromQueue(${q.id})">✕</button>
    </div>`;
  }).join('');
}

function removeFromQueue(id) { issueQueue = issueQueue.filter(q => q.id !== id); updateQueueBadge(); renderQueuePanel(); }
function clearQueue() { issueQueue = []; updateQueueBadge(); renderQueuePanel(); document.getElementById('queueStatus').textContent = ''; }

async function applyQueue() {
  if (!issueQueue.length) return;
  const applyBtn = document.getElementById('queueApplyBtn');
  const clearBtn = document.getElementById('queueClearBtn');
  const statusEl = document.getElementById('queueStatus');
  applyBtn.disabled = true; clearBtn.disabled = true;
  applyBtn.innerHTML = '<span style="display:inline-flex;align-items:center;gap:6px;"><span class="apply-spinner"></span> 반영 중...</span>';
  let success = 0, fail = 0;
  const total = issueQueue.length;
  for (const q of issueQueue) {
    const el = document.getElementById(`qitem-${q.id}`);
    if (el) {
      el.style.borderColor = '#f59e0b'; el.style.background = 'rgba(245,158,11,.08)';
      const info = el.querySelector('.queue-item-info');
      if (info) info.insertAdjacentHTML('beforeend', '<div class="queue-item-processing">⏳ 반영 중...</div>');
    }
    const done = success + fail;
    const pct = Math.round(done / total * 100);
    statusEl.innerHTML = `<div style="display:flex;flex-direction:column;gap:4px;width:100%;"><div style="font-size:11px;color:var(--text3);">반영 중... ${done}/${total}</div><div style="height:4px;background:var(--border);border-radius:2px;width:140px;"><div style="height:100%;width:${pct}%;background:var(--accent);border-radius:2px;transition:width .3s;"></div></div></div>`;
    try {
      const res = await fetch(`/api/issue/${q.id}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ status_id: q.status_id, assigned_to_id: q.assigned_to_id, fixed_version_id: q.fixed_version_id, due_date: q.due_date, notes: q.notes }),
      });
      const data = await res.json();
      if (data.ok) {
        success++;
        if (el) { el.classList.add('done'); el.style.borderColor=''; el.style.background=''; el.querySelector('.queue-item-remove').style.display='none'; const p=el.querySelector('.queue-item-processing'); if(p)p.textContent='✅ 완료'; }
      } else {
        fail++;
        if (el) { el.classList.add('error'); el.style.borderColor=''; el.style.background=''; const p=el.querySelector('.queue-item-processing'); if(p)p.textContent='❌ 실패'; }
      }
    } catch(e) {
      fail++;
      if (el) { el.classList.add('error'); el.style.borderColor=''; el.style.background=''; const p=el.querySelector('.queue-item-processing'); if(p)p.textContent='❌ 오류'; }
    }
  }
  statusEl.innerHTML = `<div style="display:flex;flex-direction:column;gap:4px;width:100%;"><div style="font-size:11px;color:${fail?'var(--danger)':'var(--success)'};">✅ ${success}건 완료${fail?' / ❌ '+fail+'건 실패':''}</div><div style="height:4px;background:var(--border);border-radius:2px;width:140px;"><div style="height:100%;width:100%;background:${fail?'var(--danger)':'var(--success)'};border-radius:2px;"></div></div></div>`;
  applyBtn.innerHTML = '✅ 반영 완료'; clearBtn.disabled = false;
  issueQueue = issueQueue.filter(q => { const el=document.getElementById(`qitem-${q.id}`); return el && el.classList.contains('error'); });
  updateQueueBadge();
  setTimeout(() => loadData(true), 1200);
}

function showToast(msg) {
  let t = document.getElementById('_toast');
  if (!t) {
    t = document.createElement('div');
    t.id = '_toast';
    t.style.cssText = 'position:fixed;bottom:32px;left:50%;transform:translateX(-50%);background:#1e2d45;color:#e2e8f0;padding:10px 20px;border-radius:10px;font-size:13px;z-index:9999;transition:opacity .3s;border:1px solid #3b82f6;';
    document.body.appendChild(t);
  }
  t.textContent = msg; t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.opacity = '0'; }, 2000);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeIssueModal(); closeQueuePanel(); }
});

// ==================== 로켓 로더 ====================
function makeRocketSpinner(msg) {
  const stars = Array.from({length:8}, (_,i) => {
    const top=5+Math.random()*80, left=5+Math.random()*80, size=2+Math.random()*3, delay=i*0.2;
    return `<div class="rstar" style="top:${top}%;left:${left}%;width:${size}px;height:${size}px;animation-delay:${delay}s;"></div>`;
  }).join('');
  const dots = Array.from({length:5}, (_,i) => `<div class="trail-dot" style="animation-delay:${i*0.15}s;"></div>`).join('');
  return `<div class="spinner"><div class="rocket-loader"><div class="rocket-stars">${stars}</div><div class="rocket-emoji">🚀</div></div><div class="rocket-trail">${dots}</div></div>`;
}

function makeMatrixLoading(msg) {
  return `<div class="loading">${makeRocketSpinner(msg)}<div class="loading-text">${msg}<br><span style="font-size:11px;opacity:.6;">1분 안에 로딩될 거에요 🙂</span></div></div>`;
}

// ==================== 담당자 상세 뷰 ====================
function openUserDetail(name, dept) {
  const issues = allUsersData[name]?.issues || [];
  const today  = new Date().toISOString().slice(0,10);
  const total   = issues.length;
  const open    = issues.filter(i => !CLOSED_SET.has(i.status)).length;
  const overdue = issues.filter(i => i.due_date && i.due_date < today && !CLOSED_SET.has(i.status) && !HOLD_SET_JS.has(i.status)).length;
  const resolved = issues.filter(i => RESOLVED_SET.has(i.status)).length;
  document.getElementById('userDetailAvatar').textContent = shortName(name).slice(0,2);
  document.getElementById('userDetailName').textContent   = shortName(name);
  document.getElementById('userDetailDept').textContent   = dept;
  document.getElementById('userDetailTotal').textContent  = total;
  document.getElementById('userDetailOpen').textContent   = open;
  document.getElementById('userDetailOver').textContent   = overdue;
  document.getElementById('userDetailDone').textContent   = resolved;
  document.getElementById('userDetailIssues').innerHTML   = renderGroupedIssues(issues);
  document.getElementById('usersListView').style.display  = 'none';
  document.getElementById('userDetailView').style.display = '';
}

function closeUserDetail() {
  document.getElementById('userDetailView').style.display = 'none';
  document.getElementById('usersListView').style.display  = '';
}

// ==================== 진행대기 탭 ====================
let allPendingIssues = [];
let _pendingDeptBuilt = false;

function buildPendingData(usersData) {
  allPendingIssues = [];
  Object.entries(usersData).forEach(([name, ud]) => {
    ud.issues.forEach(i => {
      if (i.status === '진행대기' || i.status === '진행 대기') {
        allPendingIssues.push({...i, assignee: name});
      }
    });
  });
  allPendingIssues.sort((a,b) => (a.due_date||'9999') > (b.due_date||'9999') ? 1 : -1);
  const cnt = document.getElementById('tabPendingCnt');
  if (cnt) cnt.textContent = allPendingIssues.length;
  if (!_pendingDeptBuilt) {
    const sel = document.getElementById('pendingDeptFilter');
    if (sel) {
      const allDepts = [...new Set(Object.keys(allUsersData).map(name => deptName(name)))].sort();
      allDepts.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d; opt.textContent = d;
        sel.appendChild(opt);
      });
      _pendingDeptBuilt = true;
    }
  }
  renderPendingTable(allPendingIssues);
}

let _allProjectRisk = [];

function renderRisk(projectRisk) {
  _allProjectRisk = projectRisk;
  const grid = document.getElementById('riskGrid');
  if (!grid) return;

  // 탭 배지 업데이트 (Critical+High 개수)
  const highCount = projectRisk.filter(p => p.risk_level === 'Critical' || p.risk_level === 'High').length;
  // 마감관리 탭 배지 (초과+대기 합산)
  const deadlineBadge = document.getElementById('tabDeadlineCnt');
  if (deadlineBadge) {
    const overdueCount2 = overdueIssues ? overdueIssues.length : 0;
    const pendingCount2 = allPendingIssues ? allPendingIssues.length : 0;
    const total2 = overdueCount2 + pendingCount2;
    deadlineBadge.textContent = total2;
    deadlineBadge.style.display = total2 > 0 ? 'inline-block' : 'none';
  }
  const badge = document.getElementById('tabRiskCnt');
  if (badge) {
    badge.textContent = highCount;
    badge.style.display = highCount > 0 ? 'inline-block' : 'none';
  }

  // 패널 배지 + 요약 카드 업데이트 함수
  function updateRiskBadge(topRisk, criticalCnt, highCnt, total) {
    // 패널 배지
    const badge = document.getElementById('riskPanelBadge');
    if (badge) {
      badge.style.display = 'flex';
      badge.style.borderColor = `${topRisk.risk_color}55`;
      badge.style.background  = `${topRisk.risk_color}12`;
      badge.onmouseover = () => badge.style.background = `${topRisk.risk_color}22`;
      badge.onmouseout  = () => badge.style.background = `${topRisk.risk_color}12`;
      document.getElementById('riskPanelIcon').textContent  = topRisk.risk_icon;
      document.getElementById('riskPanelLevel').textContent = topRisk.risk_level;
      document.getElementById('riskPanelLevel').style.color = topRisk.risk_color;
      document.getElementById('riskPanelSub').textContent   = `Critical ${criticalCnt} · High ${highCnt} · ${total}개 프로젝트`;
      document.getElementById('riskPanelSub').style.color = topRisk.risk_color;
    }

  }

  if (!projectRisk.length) {
    grid.innerHTML = '<div class="empty" style="padding:60px;font-size:14px;">✅ 위험 프로젝트가 없습니다</div>';
    const low = { risk_icon:'🟢', risk_level:'Low', risk_color:'#34d399' };
    updateRiskBadge(low, 0, 0, 0);
    return;
  }

  const topRisk      = projectRisk[0];
  const criticalCount = projectRisk.filter(p => p.risk_level === 'Critical').length;
  const highCount2    = projectRisk.filter(p => p.risk_level === 'High').length;
  updateRiskBadge(topRisk, criticalCount, highCount2, projectRisk.length);

  grid.innerHTML = projectRisk.map((p, idx) => {
    const barColor = p.risk_color;
    return `
    <div class="risk-card" style="border-top:3px solid ${barColor};cursor:pointer;" onclick="openRiskDetail(${idx})">
      <div class="risk-card-top">
        <div>
          <div class="risk-project-name">${p.name}</div>
          <div style="font-size:11px;color:var(--text3);margin-top:2px;">전체 ${p.total}건 · 오픈 ${p.open}건 · 클릭하여 상세보기</div>
        </div>
        <div class="risk-score-badge">
          <div class="risk-score-num" style="color:${barColor};">${p.risk_score}</div>
          <div class="risk-level-label" style="color:${barColor};">${p.risk_icon} ${p.risk_level}</div>
        </div>
      </div>
      <div class="risk-stats">
        <div class="risk-stat"><div class="risk-stat-val" style="color:#f87171;">${p.overdue}</div><div class="risk-stat-lbl">초과</div></div>
        <div class="risk-stat"><div class="risk-stat-val" style="color:#fbbf24;">${p.urgent}</div><div class="risk-stat-lbl">임박</div></div>
        <div class="risk-stat"><div class="risk-stat-val" style="color:#818cf8;">${p.pending}</div><div class="risk-stat-lbl">진행대기</div></div>
        <div class="risk-stat"><div class="risk-stat-val" style="color:#34d399;">${p.open}</div><div class="risk-stat-lbl">오픈</div></div>
      </div>
    </div>`;
  }).join('');
}

let _currentRiskIssues = [];

function openRiskDetail(idx) {
  const p = _allProjectRisk[idx];
  if (!p) return;

  // 이슈 합치기 (중복 제거 - id 기준)
  const seen = new Set();
  const all = [];
  const tag = (issues, type) => issues.forEach(i => {
    if (!seen.has(i.id)) { seen.add(i.id); all.push({...i, _type: type}); }
  });
  tag(p.issues_overdue || [], 'overdue');
  tag(p.issues_urgent  || [], 'urgent');
  tag(p.issues_pending || [], 'pending');
  all.sort((a, b) => (a.due_date || '9999') > (b.due_date || '9999') ? 1 : -1);
  _currentRiskIssues = all;

  // 헤더 정보
  document.getElementById('riskDetailTitle').textContent = p.name;
  document.getElementById('riskDetailLevel').innerHTML =
    `<span style="color:${p.risk_color};font-weight:700;">${p.risk_icon} ${p.risk_level}</span> · 위험 점수 ${p.risk_score}점`;
  document.getElementById('riskDetailScore').style.color = p.risk_color;
  document.getElementById('riskDetailScore').textContent = p.risk_score;
  document.getElementById('riskDetailOverdue').textContent = p.overdue;
  document.getElementById('riskDetailUrgent').textContent = p.urgent;
  document.getElementById('riskDetailPending').textContent = p.pending;

  // 필터 초기화
  document.getElementById('riskDetailSearch').value = '';
  document.getElementById('riskDetailTypeFilter').value = '';

  // 뷰 전환
  document.getElementById('riskListView').style.display = 'none';
  document.getElementById('riskDetailView').style.display = 'block';

  renderRiskDetailTable(_currentRiskIssues);
}

function closeRiskDetail() {
  document.getElementById('riskDetailView').style.display = 'none';
  document.getElementById('riskListView').style.display = 'block';
}

function filterRiskDetail() {
  const q = document.getElementById('riskDetailSearch').value.toLowerCase();
  const type = document.getElementById('riskDetailTypeFilter').value;
  const filtered = _currentRiskIssues.filter(i => {
    const matchQ = !q || i.subject.toLowerCase().includes(q) || i.assignee.toLowerCase().includes(q);
    const matchType = !type || i._type === type;
    return matchQ && matchType;
  });
  renderRiskDetailTable(filtered);
}

function renderRiskDetailTable(issues) {
  const tbody = document.getElementById('riskDetailTableBody');
  if (!tbody) return;
  document.getElementById('riskDetailCount').textContent = `${issues.length}건`;

  const typeLabel = { overdue: '<span style="color:#f87171;font-weight:700;">🔴 초과</span>', urgent: '<span style="color:#fbbf24;font-weight:700;">⚠️ 임박</span>', pending: '<span style="color:#818cf8;font-weight:700;">⏸ 대기</span>' };

  tbody.innerHTML = issues.map(i => `
    <tr>
      <td><span class="overdue-id" onclick="openIssueModal(${i.id})" style="cursor:pointer;">#${i.id}</span></td>
      <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="openIssueModal(${i.id})">${i.subject}</td>
      <td>${shortName(i.assignee)}</td>
      <td>${typeLabel[i._type] || '-'}</td>
      <td><span class="badge ${getBadgeClass(i.status)}">${i.status}</span></td>
      <td>${i.priority}</td>
      <td>${i.due_date || '-'}</td>
      <td>${getDday(i.due_date, i.status)}</td>
    </tr>`).join('') || '<tr><td colspan="8" class="empty" style="padding:32px;">이슈가 없습니다</td></tr>';
}

function renderPendingTable(issues) {
  const tbody = document.getElementById('pendingTableBody');
  if (!tbody) return;
  document.getElementById('pendingCount').textContent = `총 ${issues.length}건`;
  document.getElementById('pendingVisibleCount').textContent = `${issues.length}건`;
  tbody.innerHTML = issues.map(i => {
    const dept = deptName(i.assignee);
    return `<tr>
      <td><span class="overdue-id" onclick="openIssueModal(${i.id})" style="cursor:pointer;">#${i.id}</span></td>
      <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="openIssueModal(${i.id})">${i.subject}</td>
      <td>${shortName(i.assignee)}</td>
      <td><span class="badge badge-progress">${dept}</span></td>
      <td>${i.due_date || '-'}</td>
      <td>${getDday(i.due_date, i.status)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty" style="padding:32px;font-size:14px;">✅ 진행대기 일감이 없습니다</td></tr>';
}

function filterPending(search = '') {
  const dept = document.getElementById('pendingDeptFilter')?.value || '';
  const s = search.toLowerCase();
  const filtered = allPendingIssues.filter(i => {
    const matchS = !s || i.subject.toLowerCase().includes(s) || i.assignee.toLowerCase().includes(s);
    const matchD = !dept || deptName(i.assignee) === dept;
    return matchS && matchD;
  });
  document.getElementById('pendingVisibleCount').textContent = `${filtered.length}건`;
  if (filtered.length === 0) {
    document.getElementById('pendingTableBody').innerHTML = `<tr><td colspan="6" class="empty" style="padding:48px;font-size:14px;text-align:center;">✅ 진행대기 중인 일감이 없습니다</td></tr>`;
    document.getElementById('pendingCount').textContent = '총 0건';
    return;
  }
  renderPendingTable(filtered);
}

function openPendingTabFiltered(dept) {
  switchTab('deadline', document.getElementById('tabDeadlineBtn'));
  switchDeadlineSubTab('pending', document.getElementById('subTabPendingBtn'));
  setTimeout(() => { const sel=document.getElementById('pendingDeptFilter'); if(sel){sel.value=dept;filterPending('');} }, 50);
}

// ==================== 접속자 추적 + 헬스체크 (통합) ====================
async function updateVisitors() {
  try {
    const res = await fetch('/api/visitors', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    renderVisitors(data.count);
    // 연결 복구 시 LIVE 복원
    const liveDot = document.getElementById('liveDot');
    if (liveDot && liveDot.classList.contains('offline')) {
      liveDot.classList.remove('offline');
      liveDot.textContent = 'LIVE';
    }
  } catch(e) {
    const liveDot = document.getElementById('liveDot');
    if (liveDot && !liveDot.classList.contains('offline')) {
      liveDot.classList.add('offline');
      const lastTime = document.getElementById('lastUpdated').textContent;
      const timeStr = lastTime.includes('업데이트') ? lastTime.replace('마지막 업데이트: ', '') : '-';
      liveDot.textContent = `연결 끊김 (${timeStr})`;
    }
  }
}

function renderVisitors(count) {
  const wrap = document.getElementById('visitorsWrap');
  if (!wrap) return;
  const max = 5;
  let html = '';
  const show = Math.min(count, max);
  const colors = ['linear-gradient(135deg,#3b82f6,#8b5cf6)','linear-gradient(135deg,#06b6d4,#3b82f6)','linear-gradient(135deg,#10b981,#06b6d4)','linear-gradient(135deg,#f59e0b,#ef4444)','linear-gradient(135deg,#ec4899,#8b5cf6)'];
  for (let i = 0; i < show; i++) {
    const isMe = i === 0;
    html += `<div class="visitor-dot ${isMe?'self':''}" style="background:${colors[i%colors.length]};z-index:${show-i};" title="${isMe?'나':'접속자 '+(i+1)}">👤</div>`;
  }
  if (count > max) html += `<div class="visitor-more" title="${count}명 접속 중">+${count-max}</div>`;
  wrap.innerHTML = html;
  wrap.title = `현재 ${count}명 접속 중`;
}

updateVisitors();
setInterval(updateVisitors, 30000);

const _sm = document.getElementById('staticMainLoading');
if (_sm) _sm.innerHTML = makeMatrixLoading('데이터 불러오는 중...');
const _sv = document.getElementById('staticVersionLoading');
if (_sv) _sv.innerHTML = makeMatrixLoading('버전 데이터 불러오는 중...');

async function loadProjects() {
  const res = await fetch('/api/projects');
  const data = await res.json();
  const sel = document.getElementById('projectSelect');
  data.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.identifier; opt.textContent = p.name;
    if (p.identifier === 'ds_project') opt.selected = true;
    sel.appendChild(opt);
  });
}

function getDday(due_date, status) {
  if (!due_date || CLOSED_SET.has(status)) return '';
  const diff = Math.round((new Date(due_date) - new Date()) / 86400000);
  if (diff < 0)  return `<span class="dday dday-over">D+${Math.abs(diff)}</span>`;
  if (diff === 0) return `<span class="dday dday-soon">D-Day</span>`;
  if (diff <= 3)  return `<span class="dday dday-soon">D-${diff}</span>`;
  return `<span class="dday dday-ok">D-${diff}</span>`;
}

function getBadgeClass(status) {
  if (CLOSED_SET.has(status))   return 'badge-closed';
  if (RESOLVED_SET.has(status)) return 'badge-resolved';
  if (PROGRESS_SET.has(status)) return 'badge-progress';
  return 'badge-new';
}

function shortName(name) { return name.includes('_') ? name.split('_').slice(1).join('_') : name; }
const DEPT_NORMALIZE_JS = { '1기획':'기획', '1PM':'PM', '1클라':'클라', '1서버':'서버', '1UI':'UI' };
function deptName(name) {
  const raw = name.includes('_') ? name.split('_')[0] : name;
  return DEPT_NORMALIZE_JS[raw] || raw;
}

function renderUsers(data) {
  const grid = document.getElementById('usersGrid');
  const today = new Date().toISOString().slice(0,10);
  const depts = new Set();
  let html = '';

  const sorted = Object.entries(data).sort((a,b) => {
    const aOver = a[1].issues.filter(i => i.due_date && i.due_date < today && !CLOSED_SET.has(i.status)).length;
    const bOver = b[1].issues.filter(i => i.due_date && i.due_date < today && !CLOSED_SET.has(i.status)).length;
    return bOver - aOver || b[1].issues.length - a[1].issues.length;
  });

  sorted.forEach(([name, ud]) => {
    const dept = deptName(name);
    depts.add(dept);
    const issues  = ud.issues;
    const total   = issues.length;
    const open    = issues.filter(i => !CLOSED_SET.has(i.status)).length;
    const overdue = issues.filter(i => i.due_date && i.due_date < today && !CLOSED_SET.has(i.status) && !HOLD_SET_JS.has(i.status)).length;
    const resolved = issues.filter(i => RESOLVED_SET.has(i.status)).length;
    const initials = shortName(name).slice(0,2);

    html += `
    <div class="user-card" data-name="${name.toLowerCase()}" data-dept="${dept}" data-fullname="${name.replace(/"/g,'&quot;')}" onclick="openUserDetail(this.dataset.fullname, this.dataset.dept)" style="cursor:pointer;">
      <div class="user-card-header">
        <div class="avatar">${initials}</div>
        <div>
          <div class="user-name">${shortName(name)}</div>
          <div class="user-dept">${dept}</div>
        </div>
      </div>
      <div class="user-stats">
        <div class="stat"><div class="stat-val">${total}</div><div class="stat-lbl">전체</div></div>
        <div class="stat"><div class="stat-val" style="color:var(--accent2);">${open}</div><div class="stat-lbl">오픈</div></div>
        <div class="stat"><div class="stat-val red">${overdue}</div><div class="stat-lbl">초과</div></div>
        <div class="stat"><div class="stat-val green">${resolved}</div><div class="stat-lbl">해결</div></div>
      </div>
      <div class="issue-list">
        ${renderGroupedIssues(issues)}
      </div>
    </div>`;
  });

  grid.innerHTML = html || '<div class="empty">데이터가 없습니다</div>';

  const deptSel = document.getElementById('deptFilter');
  deptSel.innerHTML = '<option value="">전체 그룹</option>';
  [...depts].sort().forEach(d => {
    const opt = document.createElement('option');
    opt.value = d; opt.textContent = d;
    deptSel.appendChild(opt);
  });

  const chartDeptSel = document.getElementById('chartDeptFilter');
  chartDeptSel.innerHTML = '<option value="">── 그룹 선택 ──</option>';
  [...depts].sort().forEach(d => {
    const opt = document.createElement('option');
    opt.value = d; opt.textContent = d;
    chartDeptSel.appendChild(opt);
  });

  // ★ 참여 사용자 툴팁 업데이트
  updateUserTooltip(data);

  updateVisibleCount();
}

function filterUsers(search) {
  const dept = document.getElementById('deptFilter').value;
  const q = search.toLowerCase();
  document.querySelectorAll('.user-card').forEach(card => {
    const name = card.dataset.name;
    const cardDept = card.dataset.dept;
    const matchSearch = !q || name.includes(q);
    const matchDept   = !dept || cardDept === dept;
    card.style.display = matchSearch && matchDept ? '' : 'none';
  });
  updateVisibleCount();
}

function updateVisibleCount() {
  const total   = document.querySelectorAll('.user-card').length;
  const visible = document.querySelectorAll('.user-card:not([style*="none"])').length;
  document.getElementById('visibleCount').textContent = `${visible} / ${total}명`;
}

function renderGroupCharts(dept) {
  const section = document.getElementById('groupChartSection');
  const title   = document.getElementById('groupChartTitle');
  if (!dept) { section.innerHTML = '<div class="empty" style="padding:60px;">🏢 위에서 그룹을 선택해주세요</div>'; title.textContent = ''; return; }
  title.textContent = `${dept} 그룹`;
  const filtered = Object.fromEntries(Object.entries(allUsersData).filter(([name]) => deptName(name) === dept));
  const statusCount = {}, priorityCount = {}, userTotal = {}, userOpen = {};
  Object.entries(filtered).forEach(([name, ud]) => {
    const sName = shortName(name);
    ud.issues.forEach(i => {
      statusCount[i.status]     = (statusCount[i.status]||0) + 1;
      priorityCount[i.priority] = (priorityCount[i.priority]||0) + 1;
      userTotal[sName] = (userTotal[sName]||0) + 1;
      if (!CLOSED_SET.has(i.status)) userOpen[sName] = (userOpen[sName]||0) + 1;
    });
  });
  section.innerHTML = `
    <div class="charts-grid">
      <div class="chart-card"><div class="chart-title">상태별 이슈 분포</div><canvas id="gChartStatus" height="250"></canvas></div>
      <div class="chart-card"><div class="chart-title">우선순위별 분포</div><canvas id="gChartPriority" height="250"></canvas></div>
      <div class="chart-card"><div class="chart-title">담당자별 전체 이슈</div><canvas id="gChartUserTotal" height="250"></canvas></div>
      <div class="chart-card"><div class="chart-title">담당자별 오픈 이슈</div><canvas id="gChartUserOpen" height="250"></canvas></div>
    </div>`;
  const gChartDefs = [
    { id:'gChartStatus',    type:'doughnut', labels:Object.keys(statusCount),   data:Object.values(statusCount),   colors:['#3b82f6','#f59e0b','#10b981','#94a3b8','#ef4444','#8b5cf6','#06b6d4'] },
    { id:'gChartPriority',  type:'doughnut', labels:Object.keys(priorityCount), data:Object.values(priorityCount), colors:['#ef4444','#f59e0b','#3b82f6','#94a3b8'] },
    { id:'gChartUserTotal', type:'bar', labels:Object.entries(userTotal).sort((a,b)=>b[1]-a[1]).map(e=>e[0]), data:Object.entries(userTotal).sort((a,b)=>b[1]-a[1]).map(e=>e[1]), colors:['#8b5cf6'] },
    { id:'gChartUserOpen',  type:'bar', labels:Object.entries(userOpen).sort((a,b)=>b[1]-a[1]).map(e=>e[0]),  data:Object.entries(userOpen).sort((a,b)=>b[1]-a[1]).map(e=>e[1]),  colors:['#f59e0b'] },
  ];
  gChartDefs.forEach(def => {
    if (charts[def.id]) charts[def.id].destroy();
    const ctx = document.getElementById(def.id).getContext('2d');
    charts[def.id] = new Chart(ctx, {
      type: def.type,
      data: { labels:def.labels, datasets:[{ data:def.data, backgroundColor:def.type==='doughnut'?def.colors:def.colors[0], borderWidth:0, borderRadius:def.type==='bar'?6:0 }] },
      options: { responsive:true, plugins:{ legend:{display:def.type==='doughnut',labels:{color:'#94a3b8',font:{size:11}}}, tooltip:{callbacks:{label:ctx=>` ${ctx.label}: ${ctx.raw}건`}} }, scales: def.type==='bar'?{x:{ticks:{color:'#94a3b8',font:{size:11}},grid:{color:'#1e2d45'}},y:{ticks:{color:'#94a3b8'},grid:{color:'#1e2d45'}}}:{}, cutout: def.type==='doughnut'?'60%':undefined }
    });
  });
}

function renderCharts(data) {
  const statusCount = {}, priorityCount = {}, deptOpen = {}, userIssues = {};
  Object.entries(data).forEach(([name, ud]) => {
    const dept = deptName(name);
    ud.issues.forEach(i => {
      statusCount[i.status] = (statusCount[i.status]||0) + 1;
      priorityCount[i.priority] = (priorityCount[i.priority]||0) + 1;
      if (!CLOSED_SET.has(i.status)) {
        deptOpen[dept] = (deptOpen[dept]||0) + 1;
        userIssues[name] = (userIssues[name]||0) + 1;
      }
    });
  });
  const chartDefs = [
    { id:'chartStatus',   type:'doughnut', labels:Object.keys(statusCount),   data:Object.values(statusCount),   colors:['#3b82f6','#f59e0b','#10b981','#94a3b8','#ef4444','#8b5cf6','#06b6d4'] },
    { id:'chartPriority', type:'doughnut', labels:Object.keys(priorityCount), data:Object.values(priorityCount), colors:['#ef4444','#f59e0b','#3b82f6','#94a3b8'] },
    { id:'chartDept', type:'bar', labels:Object.entries(deptOpen).sort((a,b)=>b[1]-a[1]).slice(0,10).map(e=>e[0]), data:Object.entries(deptOpen).sort((a,b)=>b[1]-a[1]).slice(0,10).map(e=>e[1]), colors:['#3b82f6'] },
    { id:'chartUser', type:'bar', labels:Object.entries(userIssues).sort((a,b)=>b[1]-a[1]).slice(0,10).map(e=>shortName(e[0])), data:Object.entries(userIssues).sort((a,b)=>b[1]-a[1]).slice(0,10).map(e=>e[1]), colors:['#06b6d4'] },
  ];
  chartDefs.forEach(def => {
    if (charts[def.id]) charts[def.id].destroy();
    const ctx = document.getElementById(def.id).getContext('2d');
    charts[def.id] = new Chart(ctx, {
      type: def.type,
      data: { labels:def.labels, datasets:[{ data:def.data, backgroundColor:def.type==='doughnut'?def.colors:def.colors[0], borderWidth:0, borderRadius:def.type==='bar'?6:0 }] },
      options: { responsive:true, plugins:{ legend:{display:def.type==='doughnut',labels:{color:'#94a3b8',font:{size:11}}}, tooltip:{callbacks:{label:ctx=>` ${ctx.label}: ${ctx.raw}건`}} }, scales: def.type==='bar'?{x:{ticks:{color:'#94a3b8',font:{size:11}},grid:{color:'#1e2d45'}},y:{ticks:{color:'#94a3b8'},grid:{color:'#1e2d45'}}}:{}, cutout: def.type==='doughnut'?'60%':undefined }
    });
  });
}

async function loadData(force = false) {
  const project = document.getElementById('projectSelect').value;
  const after   = document.getElementById('updatedAfter').value;

  document.getElementById('usersGrid').innerHTML = makeMatrixLoading('Redmine에서 새로 불러오는 중...');
  ['statUsers','statTotal','statOpen','statOverdue','statOverduePlanning','statOverdueServer','statOverdueClient','statPendingPlanning','statPendingServer','statPendingClient'].forEach(id => {
    document.getElementById(id).textContent = '-';
  });
  if (force) { allVersionData = []; _overdueDeptBuilt = false; _pendingDeptBuilt = false; }

  loadVersions(force);

  try {
    const res  = await fetch(`/api/data?project_id=${project}&updated_after=${after}&force=${force}`);
    const data = await res.json();

    document.getElementById('statUsers').textContent           = data.users.toLocaleString();
    document.getElementById('statTotal').textContent           = data.total_issues.toLocaleString();
    document.getElementById('statOpen').textContent            = data.open_issues.toLocaleString();
    document.getElementById('statOverdue').textContent         = data.overdue.toLocaleString();
    document.getElementById('statOverduePlanning').textContent = (data.overdue_planning ?? 0).toLocaleString();
    document.getElementById('statOverdueServer').textContent   = (data.overdue_server ?? 0).toLocaleString();
    document.getElementById('statOverdueClient').textContent   = (data.overdue_client ?? 0).toLocaleString();
    document.getElementById('statPendingPlanning').textContent = (data.pending_planning ?? 0).toLocaleString();
    document.getElementById('statPendingServer').textContent   = (data.pending_server ?? 0).toLocaleString();
    document.getElementById('statPendingClient').textContent   = (data.pending_client ?? 0).toLocaleString();

    allUsersData = data.users_data;
    renderUsers(allUsersData);
    renderCharts(allUsersData);
    buildPendingData(data.users_data);
    renderRisk(data.project_risk || []);

    document.getElementById('lastUpdated').textContent = '마지막 업데이트: ' + new Date().toLocaleTimeString('ko-KR');
    // 연결 정상
    const liveDot = document.getElementById('liveDot');
    liveDot.classList.remove('offline');
    liveDot.textContent = 'LIVE';

    const cacheEl = document.getElementById('cacheStatus');
    if (data.cached) { cacheEl.textContent = `💾 캐시 (${data.cache_age})`; cacheEl.style.color = 'var(--success)'; }
    else { cacheEl.textContent = '🌐 실시간'; cacheEl.style.color = 'var(--accent2)'; }

    const today = new Date().toISOString().slice(0,10);
    const overdueAll = [];
    Object.entries(allUsersData).forEach(([name, ud]) => {
      ud.issues.forEach(i => {
        if (i.due_date && i.due_date < today && !CLOSED_SET.has(i.status) && !HOLD_SET_JS.has(i.status)) {
          overdueAll.push({...i, assignee: name});
        }
      });
    });
    overdueAll.sort((a,b) => a.due_date > b.due_date ? 1 : -1);
    renderOverdueList(overdueAll);

  } catch(e) {
    document.getElementById('usersGrid').innerHTML = '<div class="empty">❌ 데이터 로드 실패. 서버 연결을 확인하세요.</div>';
    const liveDot = document.getElementById('liveDot');
    liveDot.classList.add('offline');
    const lastTime = document.getElementById('lastUpdated').textContent;
    const timeStr = lastTime.includes('업데이트') ? lastTime.replace('마지막 업데이트: ', '') : '-';
    liveDot.textContent = `연결 끊김 (${timeStr})`;
  }
}

// ==================== 버전 기능 ====================
let allVersionData = [];
let currentVersionIssues = [];

async function loadVersions(force = false) {
  const project = document.getElementById('projectSelect').value;
  const vTab = document.getElementById('tab-versions');
  if (vTab.classList.contains('active')) {
    document.getElementById('versionCards').innerHTML = '<div class="loading" id="staticVersionLoading"></div>';
    document.getElementById('versionDetail').style.display = 'none';
    document.getElementById('versionCards').style.display = '';
  }
  try {
    const res = await fetch(`/api/versions?project_id=${project}&force=${force}`);
    allVersionData = await res.json();
    renderVersionCards(allVersionData);
    if (vTab.classList.contains('active')) document.getElementById('versionCards').style.display = '';
  } catch(e) {
    document.getElementById('versionCards').innerHTML = '<div class="empty">❌ 버전 데이터 로드 실패</div>';
  }
}

function renderVersionCards(versions) {
  const today = new Date().toISOString().slice(0,10);
  document.getElementById('statVersions').textContent = versions.length;
  if (!versions.length) { document.getElementById('versionCards').innerHTML = '<div class="empty">버전 정보가 없습니다</div>'; return; }
  const html = versions.map(v => {
    const due = v.due_date || '';
    const diff = due ? Math.round((new Date(due) - new Date()) / 86400000) : null;
    let dueClass = '', dueText = due || '마감일 없음', cardClass = '';
    if (due) {
      if (diff < 0)       { dueClass = 'overdue'; dueText = `${due} (D+${Math.abs(diff)})`; cardClass = 'overdue-version'; }
      else if (diff <= 14){ dueClass = 'soon';    dueText = `${due} (D-${diff})`;           cardClass = 'near-version'; }
      else                { dueText = `${due} (D-${diff})`; }
    }
    return `
    <div class="version-card ${cardClass}" onclick="openVersionDetail(${v.id})">
      <div class="version-name">🏷 ${v.name}</div>
      <div class="version-due ${dueClass}">📅 ${dueText}</div>
      <div class="version-progress-bar"><div class="version-progress-fill" style="width:${v.done_pct}%"></div></div>
      <div style="font-size:11px;color:var(--text3);margin-bottom:10px;font-family:'JetBrains Mono',monospace;">완료율 ${v.done_pct}%</div>
      <div class="version-stats">
        <div class="version-stat"><div class="version-stat-val" style="color:var(--accent2)">${v.total}</div><div class="version-stat-lbl">전체</div></div>
        <div class="version-stat"><div class="version-stat-val" style="color:var(--warn)">${v.progress}</div><div class="version-stat-lbl">진행</div></div>
        <div class="version-stat"><div class="version-stat-val" style="color:var(--success)">${v.resolved + v.closed}</div><div class="version-stat-lbl">완료</div></div>
        <div class="version-stat" onclick="event.stopPropagation(); openVersionDetailFiltered(${v.id}, 'overdue')" style="cursor:pointer;">
          <div class="version-stat-val" style="color:var(--danger);${v.overdue > 0 ? 'text-shadow:0 0 8px rgba(248,113,113,.5);' : ''}">${v.overdue}</div>
          <div class="version-stat-lbl" style="color:${v.overdue > 0 ? 'var(--danger)' : ''}">초과 ↗</div>
        </div>
      </div>
    </div>`;
  }).join('');
  document.getElementById('versionCards').innerHTML = html;
  const totalOverdue = versions.reduce((s, v) => s + (v.overdue || 0), 0);
  const badge = document.getElementById('tabVersionOverdueCnt');
  if (badge) { if (totalOverdue > 0) { badge.textContent = totalOverdue; badge.style.display = ''; } else { badge.style.display = 'none'; } }
}

function openVersionDetail(versionId) {
  const v = allVersionData.find(x => x.id === versionId);
  if (!v) return;
  currentVersionIssues = v.issues;
  document.getElementById('versionCards').style.display = 'none';
  document.getElementById('versionDetail').style.display = '';
  document.getElementById('versionDetailTitle').textContent = '🏷 ' + v.name;
  document.getElementById('versionDetailMeta').textContent = `마감일: ${v.due_date || '없음'} · 전체 ${v.total}건 · 완료율 ${v.done_pct}%`;
  document.getElementById('versionSearch').value = '';
  document.getElementById('versionStatusFilter').value = 'open';
  filterVersionIssues();
}

function openVersionDetailFiltered(versionId, filter) { openVersionDetail(versionId); document.getElementById('versionStatusFilter').value = filter; filterVersionIssues(); }
function closeVersionDetail() { document.getElementById('versionDetail').style.display = 'none'; document.getElementById('versionCards').style.display = ''; }

function filterVersionIssues() {
  const q    = document.getElementById('versionSearch').value.toLowerCase();
  const sf   = document.getElementById('versionStatusFilter').value;
  const today = new Date().toISOString().slice(0,10);
  const filtered = currentVersionIssues.filter(i => {
    const matchQ = !q || i.subject.toLowerCase().includes(q) || i.assignee.toLowerCase().includes(q);
    let matchS = true;
    if (sf === 'open')    matchS = !CLOSED_SET.has(i.status) && !HOLD_SET_JS.has(i.status);
    if (sf === 'overdue') matchS = i.due_date && i.due_date < today && !CLOSED_SET.has(i.status) && !HOLD_SET_JS.has(i.status);
    return matchQ && matchS;
  });
  renderVersionIssueTable(filtered);
}

function renderVersionIssueTable(issues) {
  const today = new Date().toISOString().slice(0,10);
  document.getElementById('versionIssueCount').textContent = `${issues.length}건`;
  if (!issues.length) { document.getElementById('versionIssueList').innerHTML = '<div class="empty">이슈가 없습니다</div>'; return; }
  const rows = issues.map(i => {
    const diff = i.due_date ? Math.round((new Date() - new Date(i.due_date)) / 86400000) : null;
    const ddayHtml = !i.due_date ? '-'
      : diff > 0  ? `<span style="color:var(--danger);font-weight:700;font-family:'JetBrains Mono',monospace;">D+${diff}</span>`
      : diff === 0 ? `<span style="color:var(--warn);font-weight:700;">D-Day</span>`
      : `<span style="color:var(--text3);font-family:'JetBrains Mono',monospace;">D-${Math.abs(diff)}</span>`;
    return `<tr>
      <td><span class="overdue-id" onclick="openIssueModal(${i.id})" style="cursor:pointer;">#${i.id}</span></td>
      <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="openIssueModal(${i.id})">${i.subject}</td>
      <td>${shortName(i.assignee)}</td>
      <td>${deptName(i.assignee)}</td>
      <td><span class="badge ${getBadgeClass(i.status)}">${i.status}</span></td>
      <td>${i.tracker}</td>
      <td style="font-family:'JetBrains Mono',monospace;font-size:12px;">${i.due_date || '-'}</td>
      <td>${ddayHtml}</td>
    </tr>`;
  }).join('');
  document.getElementById('versionIssueList').innerHTML = `
  <table class="version-issue-table">
    <thead><tr><th>#</th><th>제목</th><th>담당자</th><th>그룹</th><th>상태</th><th>트래커</th><th>마감일</th><th>D-Day</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

loadProjects().then(() => loadData(false));

</script>
</body>
</html>
"""


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
            if "assigned_to" not in iss:
                continue
            issues.append({
                "id":         iss["id"],
                "subject":    iss["subject"],
                "status":     iss["status"]["name"],
                "priority":   iss["priority"]["name"],
                "project":    iss["project"]["name"],
                "tracker":    iss.get("tracker", {}).get("name", "-"),
                "due_date":   iss.get("due_date", ""),
                "created_on": iss.get("created_on", ""),
                "updated_on": iss.get("updated_on", ""),
                "assignee":   iss["assigned_to"]["name"],
            })
        total    = len(issues)
        closed   = sum(1 for i in issues if i["status"] in CLOSED_SET)
        resolved = sum(1 for i in issues if i["status"] in RESOLVED_SET)
        progress = sum(1 for i in issues if i["status"] in PROGRESS_SET)
        overdue  = sum(1 for i in issues
                       if i["due_date"] and i["due_date"] < today_str
                       and i["status"] not in CLOSED_SET
                       and i["status"] not in HOLD_SET)
        done_pct = round((closed + resolved) / total * 100) if total else 0
        return {
            "id":           v["id"],
            "name":         v["name"],
            "project_name": v.get("project_name", ""),
            "due_date":     v.get("due_date", ""),
            "status":       v.get("status", "open"),
            "total":        total,
            "closed":       closed,
            "resolved":     resolved,
            "progress":     progress,
            "overdue":      overdue,
            "done_pct":     done_pct,
            "issues":       issues,
        }

    result = []
    print(f"  🔀 버전 병렬 로드: {len(active_versions)}개")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_version, v): v for v in active_versions}
        for future in as_completed(futures):
            try:
                result.append(future.result())
            except Exception as e:
                print(f"  ⚠️ 버전 로드 실패: {e}")

    result.sort(key=lambda x: x["due_date"] or "9999")
    return result


# ==================== API 라우터 ====================

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_PAGE


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
            print(f"  ⚡ 캐시 히트: {key} ({age})")
            return {**cached, "cached": True, "cache_age": age}
    print(f"  🌐 Redmine fetch: {key}")
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


# ==================== 실행 ====================

def warmup_cache():
    """서버 시작 직후 백그라운드에서 캐시 미리 채우기"""
    time.sleep(2)  # 서버 완전히 뜬 후 시작
    print("  🔥 캐시 워밍 시작 (백그라운드)...")
    targets = [
        ("", "2026-03-01"),
        ("ds_project", "2026-03-01"),
    ]
    for project_id, updated_after in targets:
        try:
            label = project_id if project_id else "전체"
            print(f"  🔄 워밍 중: {label}")
            data = build_dashboard_data(project_id, updated_after)
            set_cache(project_id, updated_after, data)
            print(f"  ✅ 워밍 완료: {label}")
        except Exception as e:
            print(f"  ⚠️ 캐시 워밍 실패 ({label}): {e}")
    print("  🎉 캐시 워밍 전체 완료 — 첫 방문자 즉시 응답 가능")


if __name__ == "__main__":
    print("=" * 50)
    print("  🚀 RedRisk 시작")
    print("=" * 50)
    print(f"  브라우저에서 열기: http://localhost:8000")
    print(f"  종료: Ctrl+C")
    print(f"  자동갱신 주기: 30분")
    print("=" * 50)

    # 서버 시작과 동시에 백그라운드 캐시 워밍
    warmup_thread = threading.Thread(target=warmup_cache, daemon=True)
    warmup_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=8000)