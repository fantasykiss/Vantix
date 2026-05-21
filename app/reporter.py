import json
import os
import smtplib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.constants import CLOSED_SET, RESOLVED_SET, HOLD_SET, dept_name, short_name

logger = logging.getLogger(__name__)


@dataclass
class UserSummary:
    name:           str
    dept:           str
    total:          int
    open_cnt:       int
    overdue_cnt:    int
    resolved_cnt:   int
    overdue_issues: list = field(default_factory=list)


@dataclass
class ReportData:
    generated_at:   str
    period_label:   str
    project_label:  str
    total_issues:   int
    open_issues:    int
    overdue_total:  int
    users:          list
    overdue_issues: list
    top_risk:       list
    versions:           list  = field(default_factory=list)
    risk_snapshot_ts:   str   = ""
    memo:               str   = ""
    sections:           list  = field(default_factory=list)
    ai_summary:         str   = ""



def build_report_data(dashboard: dict, project_label: str = "전체 프로젝트") -> ReportData:
    today_str  = date.today().strftime("%Y-%m-%d")
    users_data = dashboard.get("users_data", {})

    user_summaries = []
    all_overdue    = []

    for uname, ud in users_data.items():
        issues      = ud.get("issues", [])
        total       = len(issues)
        open_cnt    = sum(1 for i in issues if i["status"] not in CLOSED_SET)
        resolved    = sum(1 for i in issues if i["status"] in RESOLVED_SET)
        overdue_issues = [
            i for i in issues
            if i.get("due_date") and i["due_date"] < today_str
            and i["status"] not in CLOSED_SET
            and i["status"] not in HOLD_SET
        ]

        for iss in overdue_issues:
            all_overdue.append({
                **iss,
                "assignee_short": short_name(uname),
                "dept":           dept_name(uname),
            })

        user_summaries.append(UserSummary(
            name           = short_name(uname),
            dept           = dept_name(uname),
            total          = total,
            open_cnt       = open_cnt,
            overdue_cnt    = len(overdue_issues),
            resolved_cnt   = resolved,
            overdue_issues = overdue_issues,
        ))

    user_summaries.sort(key=lambda u: (-u.overdue_cnt, -u.open_cnt))
    all_overdue.sort(key=lambda i: i.get("due_date", ""))

    top_risk = sorted(
        dashboard.get("project_risk", []),
        key=lambda p: -p.get("risk_score", 0)
    )[:5]

    today      = date.today()
    iso        = today.isocalendar()
    week_start = date.fromisocalendar(iso[0], iso[1], 1)
    week_end   = date.fromisocalendar(iso[0], iso[1], 7)
    period_label = f"{week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}"

    # ── 버전별 진행상태 계산 ──
    version_rows = []
    raw_versions = dashboard.get("versions", [])
    # dashboard에 issues 키가 없으면 users_data에서 추출
    all_issues_raw = dashboard.get("issues") or []
    if not all_issues_raw:
        users_data = dashboard.get("users_data", {})
        seen = set()
        for ud in users_data.values():
            for i in ud.get("issues", []):
                iid = i.get("id")
                if iid not in seen:
                    seen.add(iid)
                    all_issues_raw.append(i)

    for v in sorted(raw_versions, key=lambda x: x.get("due_date") or "9999"):
        # main.py에서 미리 계산된 값 우선 사용, 없으면 0
        total_v     = v.get("_total",   0)
        closed      = v.get("_closed",  0)
        overdue_cnt = v.get("_overdue", 0)
        pct = round(closed / total_v * 100) if total_v else 0
        due = v.get("due_date", "")
        v_status = v.get("status", "open")

        if v_status == "closed":
            badge_text = "CLOSED"
        elif due and due < today_str and v_status != "closed":
            badge_text = "OVERDUE"
        elif v_status == "locked":
            badge_text = "LOCKED"
        else:
            badge_text = "OPEN"

        version_rows.append({
            "name":      v.get("name", ""),
            "due":       due,
            "badge":     badge_text,
            "pct":       pct,
            "total":     total_v,
            "closed":    closed,
            "overdue":   overdue_cnt,
            "bar_color": "#B40023" if overdue_cnt > 0 else "#111111",
        })

    # ── 리스크 스냅샷 최신 타임스탬프 추출 ──
    snapshot_ts = ""
    try:
        hist_path = os.path.join(os.path.dirname(__file__), "..", "risk_history.json")
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        all_ts = []
        for entries in hist.values():
            if isinstance(entries, list):
                for e in entries:
                    if e.get("ts"):
                        all_ts.append(e["ts"])
        if all_ts:
            snapshot_ts = max(all_ts)[:16]  # "2026-05-14 23:00"
    except Exception:
        pass

    return ReportData(
        generated_at     = datetime.now().strftime("%Y-%m-%d %H:%M"),
        period_label     = period_label,
        project_label    = project_label,
        total_issues     = dashboard.get("total_issues", 0),
        open_issues      = dashboard.get("open_issues",  0),
        overdue_total    = dashboard.get("overdue",       0),
        users            = user_summaries,
        overdue_issues   = all_overdue,
        top_risk         = top_risk,
        versions         = version_rows,
        risk_snapshot_ts = snapshot_ts,
        ai_summary       = dashboard.get("ai_summary", ""),
    )


def render_html_report(report, sections=None, memo="") -> str:
    if sections is None:
        sections = ["signal", "risk", "forecast", "metrics", "versions", "critical", "assignee"]

    # ── 데이터 추출 ──
    def _get(key, default):
        val = getattr(report, key, None)
        return val if val is not None else default

    proj_label    = _get("project_label", "프로젝트")
    period_label  = _get("period_label",  "")
    gen_ts        = _get("generated_at",  datetime.now().strftime("%Y-%m-%d %H:%M"))
    ai_text       = _get("ai_summary",    "")
    total_i       = _get("total_issues",  0)
    open_i        = _get("open_issues",   0)
    over_i        = _get("overdue_total", 0)
    members       = len(_get("users",     []))
    overdue_list  = _get("overdue_issues", [])
    risk_list     = _get("top_risk",      [])
    version_rows  = _get("versions",      [])
    users_list    = _get("users",         [])

    # ── forecast 계산 ──
    fc_delay      = len(overdue_list)
    fc_milestones = sum(1 for v in version_rows if v.get("badge") == "OVERDUE")

    # 담당자 부하 데이터
    assignee_list = []
    for u in sorted(users_list, key=lambda x: -getattr(x, 'overdue_cnt', 0)):
        assignee_list.append({
            "name":    getattr(u, "name",        ""),
            "group":   getattr(u, "dept",        ""),
            "open":    getattr(u, "open_cnt",    0),
            "overdue": getattr(u, "overdue_cnt", 0),
        })

    # ── 색상 상수 ──
    RED   = "#B40023"
    AMBER = "#c47a00"
    GREEN = "#2a7a4a"
    NAVY  = "#3a6ea8"

    def _sec_label(title):
        return (f"<div style='font-family:\"JetBrains Mono\",monospace;font-size:9px;font-weight:700;"
                f"letter-spacing:0.2em;color:#444;text-transform:uppercase;margin-bottom:14px;"
                f"display:flex;align-items:center;gap:10px;'>"
                f"{title}"
                f"<span style='flex:1;height:1px;background:rgba(255,255,255,0.05);display:block;'></span>"
                f"</div>")

    def _badge_dark(text, bg, color):
        return (f"<span style='font-family:\"JetBrains Mono\",monospace;font-size:8px;"
                f"font-weight:700;letter-spacing:0.1em;padding:3px 8px;"
                f"background:{bg};color:{color};'>{text}</span>")

    def _th(label, align="left"):
        return (f"<th style='font-family:\"JetBrains Mono\",monospace;font-size:8px;"
                f"font-weight:700;letter-spacing:0.15em;color:#333;text-transform:uppercase;"
                f"padding:0 14px 10px;text-align:{align};border-bottom:1px solid rgba(255,255,255,0.05);'>"
                f"{label}</th>")

    def _td(val, color="#888", align="left", mono=False, bold=False):
        fm = "\"JetBrains Mono\",monospace" if mono else "\"Plus Jakarta Sans\",sans-serif"
        fw = "700" if bold else "400"
        return (f"<td style='padding:11px 14px;border-bottom:1px solid rgba(255,255,255,0.04);"
                f"font-size:12px;color:{color};font-weight:{fw};font-family:{fm};"
                f"text-align:{align};'>{val}</td>")

    # ── 1. WEEKLY SIGNAL ──
    s_signal = ""
    if "signal" in sections and ai_text:
        s_signal = f"""
<div style='margin-bottom:32px;'>
  {_sec_label("Weekly Signal — AI 주간 요약")}
  <div style='border-left:2px solid {RED};padding:16px 20px;background:rgba(180,0,35,0.05);'>
    <div style='font-family:"Plus Jakarta Sans",sans-serif;font-size:13px;line-height:1.8;color:#aaaaaa;'>{ai_text}</div>
  </div>
</div>"""

    # ── 2. RISK PROJECTS ──
    s_risk = ""
    if "risk" in sections and risk_list:
        lv_map = {
            "Critical": (f"rgba(180,0,35,0.15)",   RED),
            "High":     (f"rgba(196,122,0,0.15)",  AMBER),
            "Medium":   ("rgba(255,255,255,0.06)", "#666"),
            "Low":      (f"rgba(42,122,74,0.15)",  GREEN),
        }
        def _norm_score(p):
            raw = p.get("risk_score", p.get("score", 0))
            try: return min(round(float(raw) * 100 / 60), 100)
            except: return 0
        rows_html = ""
        for p in risk_list:
            lv  = p.get("risk_level", p.get("level", ""))
            bb, bc = lv_map.get(lv, ("rgba(255,255,255,0.06)", "#666"))
            sc  = _norm_score(p)
            sc_color = RED if lv == "Critical" else AMBER
            rows_html += f"""
<div style='display:flex;align-items:center;gap:14px;padding:13px 0;border-bottom:1px solid rgba(255,255,255,0.04);'>
  <div style='font-family:"Plus Jakarta Sans",sans-serif;font-size:13px;font-weight:600;color:#dddddd;flex:1;'>{p.get("name","")}</div>
  {_badge_dark(lv.upper(), bb, bc)}
  <div style='font-family:"JetBrains Mono",monospace;font-size:22px;font-weight:700;color:{sc_color};width:50px;text-align:right;'>{sc}</div>
  <div style='font-family:"JetBrains Mono",monospace;font-size:10px;color:#444;width:80px;text-align:right;'>초과 <span style="color:{RED};font-weight:700;">{p.get("overdue",0)}</span> · 오픈 {p.get("open",0)}</div>
</div>"""
        s_risk = f"""
<div style='margin-bottom:32px;'>
  {_sec_label("Risk Projects — 위험 프로젝트")}
  <div>{rows_html}</div>
</div>"""

    # ── 3. RISK FORECAST ──
    s_forecast = ""
    if "forecast" in sections:
        def _fc_card(num, label, sub, color):
            return (f"<td style='width:33%;padding:0 5px;'>"
                    f"<div style='background:#111111;border:1px solid rgba(255,255,255,0.06);padding:18px 16px;'>"
                    f"<div style='font-family:\"JetBrains Mono\",monospace;font-size:8px;font-weight:700;"
                    f"letter-spacing:0.15em;color:#444;text-transform:uppercase;margin-bottom:10px;'>{label}</div>"
                    f"<div style='font-family:\"JetBrains Mono\",monospace;font-size:32px;font-weight:700;"
                    f"color:{color};line-height:1;'>{num}</div>"
                    f"<div style='font-family:\"Plus Jakarta Sans\",sans-serif;font-size:10px;color:#444;margin-top:6px;'>{sub}</div>"
                    f"</div></td>")
        s_forecast = f"""
<div style='margin-bottom:32px;'>
  {_sec_label("Risk Forecast — 다음 주 예측")}
  <table width='100%' cellpadding='0' cellspacing='0'>
    <tr>
      {_fc_card(fc_delay,      "차주 지연 예상", "이슈",      RED)}
      {_fc_card(fc_milestones, "완료 위험",      "마일스톤",  AMBER)}
      {_fc_card("—",           "리스크 변화",    "전주 대비", "#444")}
    </tr>
  </table>
</div>"""

    # ── 4. KEY METRICS ──
    s_metrics = ""
    if "metrics" in sections:
        def _mc(num, label, sub, color, border_color):
            return (f"<td style='width:25%;padding:0 5px;'>"
                    f"<div style='background:#111111;border:1px solid rgba(255,255,255,0.06);"
                    f"border-top:2px solid {border_color};padding:20px 16px;text-align:center;'>"
                    f"<div style='font-family:\"JetBrains Mono\",monospace;font-size:36px;"
                    f"font-weight:700;color:{color};line-height:1;'>{num}</div>"
                    f"<div style='font-family:\"JetBrains Mono\",monospace;font-size:8px;"
                    f"font-weight:700;letter-spacing:0.15em;color:#444;text-transform:uppercase;margin-top:10px;'>{label}</div>"
                    f"<div style='font-family:\"Plus Jakarta Sans\",sans-serif;font-size:10px;"
                    f"color:#333;margin-top:3px;'>{sub}</div>"
                    f"</div></td>")
        s_metrics = f"""
<div style='margin-bottom:32px;'>
  {_sec_label("Key Metrics")}
  <table width='100%' cellpadding='0' cellspacing='0'>
    <tr>
      {_mc(total_i, "Open",    "전체 이슈",  NAVY,  NAVY)}
      {_mc(over_i,  "Overdue", "마감 초과",  RED,   RED)}
      {_mc(open_i,  "Issues",  "오픈 이슈",  AMBER, AMBER)}
      {_mc(members, "Members", "참여 인원",  GREEN, GREEN)}
    </tr>
  </table>
</div>"""

    # ── 5. VERSION PROGRESS ──
    s_versions = ""
    if "versions" in sections and version_rows:
        vbadge = {
            "CLOSED":  ("rgba(255,255,255,0.06)", "#666"),
            "OVERDUE": (f"rgba(180,0,35,0.15)",   RED),
            "LOCKED":  ("rgba(255,255,255,0.04)", "#444"),
            "OPEN":    (f"rgba(42,122,74,0.15)",  GREEN),
        }
        rows_html = ""
        for v in version_rows:
            bb, bc   = vbadge.get(v["badge"], ("rgba(255,255,255,0.06)", "#666"))
            due_c    = RED if v["badge"] == "OVERDUE" else "#444"
            over_c   = RED if v["overdue"] > 0 else "#444"
            open_cnt = v["total"] - v["closed"]
            rows_html += f"""
<div style='padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.04);'>
  <div style='display:flex;align-items:center;gap:10px;margin-bottom:10px;'>
    <div style='font-family:"Plus Jakarta Sans",sans-serif;font-size:13px;font-weight:600;color:#cccccc;flex:1;'>{v["name"]}</div>
    {_badge_dark(v["badge"], bb, bc)}
    <div style='font-family:"JetBrains Mono",monospace;font-size:10px;color:{due_c};'>{v["due"]}</div>
  </div>
  <div style='display:flex;align-items:center;gap:12px;'>
    <div style='flex:1;height:3px;background:rgba(255,255,255,0.06);'>
      <div style='height:3px;background:{v["bar_color"]};width:{v["pct"]}%;'></div>
    </div>
    <div style='font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:700;color:{v["bar_color"]};width:36px;text-align:right;'>{v["pct"]}%</div>
    <div style='display:flex;gap:1px;padding-left:12px;border-left:1px solid rgba(255,255,255,0.05);'>
      <div style='width:46px;text-align:center;'><div style='font-family:"JetBrains Mono",monospace;font-size:8px;color:#333;margin-bottom:3px;'>완료</div><div style='font-family:"JetBrains Mono",monospace;font-size:14px;font-weight:700;color:#cccccc;'>{v["closed"]}</div></div>
      <div style='width:46px;text-align:center;'><div style='font-family:"JetBrains Mono",monospace;font-size:8px;color:#333;margin-bottom:3px;'>오픈</div><div style='font-family:"JetBrains Mono",monospace;font-size:14px;font-weight:700;color:#666;'>{open_cnt}</div></div>
      <div style='width:46px;text-align:center;'><div style='font-family:"JetBrains Mono",monospace;font-size:8px;color:#333;margin-bottom:3px;'>초과</div><div style='font-family:"JetBrains Mono",monospace;font-size:14px;font-weight:700;color:{over_c};'>{v["overdue"]}</div></div>
    </div>
  </div>
</div>"""
        s_versions = f"""
<div style='margin-bottom:32px;'>
  {_sec_label(f"Version Progress — {len(version_rows)}개 버전")}
  {rows_html}
</div>"""

    # ── 6. CRITICAL ITEMS ──
    s_critical = ""
    if "critical" in sections and overdue_list:
        def _critical_row(i):
            iid   = i.get("id", "")
            link  = f'<a href="#" style="color:{NAVY};font-family:JetBrains Mono,monospace;font-size:11px;text-decoration:none;">#{iid}</a>'
            return (
                f"<tr>"
                + _td(link, NAVY)
                + _td(i.get("subject", ""), "#cccccc")
                + _td(i.get("assignee_short", ""))
                + _td(i.get("status", ""))
                + _td(i.get("due_date", ""), "#666", mono=True)
                + _td(str(i.get("dday", "")), RED, align="right", mono=True, bold=True)
                + "</tr>"
            )
        rows_html = "".join(_critical_row(i) for i in overdue_list)
        s_critical = f"""
<div style='margin-bottom:32px;'>
  <div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;'>
    {_sec_label("Critical Items — 즉시 처리").replace("margin-bottom:14px","margin-bottom:0")}
    <span style='font-family:"JetBrains Mono",monospace;font-size:9px;font-weight:700;
    letter-spacing:0.1em;color:{RED};background:rgba(180,0,35,0.12);padding:3px 9px;white-space:nowrap;'>{len(overdue_list)} OVERDUE</span>
  </div>
  <table width='100%' cellpadding='0' cellspacing='0'>
    <thead><tr>
      {_th("#", "left")}{_th("제목")}{_th("담당")}{_th("상태")}{_th("마감일")}{_th("D-DAY", "right")}
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""

    # ── 7. ASSIGNEE LOAD ──
    s_assignee = ""
    if "assignee" in sections and assignee_list:
        max_open = max((a.get("open", 0) for a in assignee_list), default=1) or 1
        rows_html = ""
        for a in assignee_list:
            pct    = round(a.get("open", 0) / max_open * 100)
            has_ov = a.get("overdue", 0) > 0
            bar_c  = RED if has_ov else GREEN
            stat_c = RED if has_ov else "#666"
            rows_html += f"""
<div style='display:flex;align-items:center;gap:14px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04);'>
  <div style='font-family:"Plus Jakarta Sans",sans-serif;font-size:12px;color:#cccccc;min-width:110px;'>{a.get("name","")}</div>
  <div style='flex:1;height:3px;background:rgba(255,255,255,0.05);'>
    <div style='height:3px;background:{bar_c};width:{pct}%;'></div>
  </div>
  <div style='font-family:"JetBrains Mono",monospace;font-size:10px;color:{stat_c};min-width:100px;text-align:right;'>오픈 {a.get("open",0)} · 초과 {a.get("overdue",0)}</div>
</div>"""
        s_assignee = f"""
<div style='margin-bottom:32px;'>
  {_sec_label("Assignee Load — 담당자 부하")}
  {rows_html}
</div>"""

    # ── PM MEMO ──
    s_memo = ""
    if memo and memo.strip():
        s_memo = f"""
<div style='margin-bottom:32px;'>
  <div style='border-left:2px solid #333;padding:14px 18px;background:#111111;'>
    <div style='font-family:"JetBrains Mono",monospace;font-size:8px;font-weight:700;
    letter-spacing:0.18em;color:#333;text-transform:uppercase;margin-bottom:8px;'>PM Comment</div>
    <div style='font-family:"Plus Jakarta Sans",sans-serif;font-size:12px;line-height:1.8;
    color:#666;white-space:pre-wrap;'>{memo}</div>
  </div>
</div>"""

    # ── 최종 반환 ──
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{proj_label} 주간 리포트</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #0a0a0a; color: #f0f0f0; font-family: 'Plus Jakarta Sans', sans-serif;
  font-size: 13px; -webkit-font-smoothing: antialiased; padding: 40px 0; }}
a {{ color: {NAVY}; text-decoration: none; }}
</style>
</head>
<body>
<div style="max-width:780px;margin:0 auto;padding:0 24px;">

  <!-- 헤더 -->
  <div style="border-bottom:1px solid rgba(255,255,255,0.07);padding-bottom:28px;margin-bottom:32px;display:flex;justify-content:space-between;align-items:flex-end;">
    <div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;letter-spacing:0.22em;color:#444;text-transform:uppercase;margin-bottom:10px;">Vantix · Weekly Report</div>
      <div style="font-size:26px;font-weight:800;letter-spacing:-0.02em;color:#ffffff;line-height:1;">{proj_label}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#555;margin-top:8px;">{period_label}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#333;letter-spacing:0.15em;text-transform:uppercase;">Generated</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#444;margin-top:5px;">{gen_ts}</div>
    </div>
  </div>

  {s_signal}
  {s_risk}
  {s_forecast}
  {s_metrics}
  {s_versions}
  {s_critical}
  {s_assignee}
  {s_memo}

  <!-- 푸터 -->
  <div style="border-top:1px solid rgba(255,255,255,0.05);margin-top:40px;padding-top:20px;display:flex;justify-content:space-between;align-items:center;">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;letter-spacing:0.25em;color:#222;">VANTIX</div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#222;">자동 생성 · {gen_ts}</div>
  </div>

</div>
</body>
</html>"""


def render_tsv_report(report, sections=None) -> str:
    """구글 스프레드시트 붙여넣기용 TSV 생성"""
    if sections is None:
        sections = ["signal", "metrics", "critical", "risk", "versions", "assignee"]

    def _get(key, default):
        val = getattr(report, key, None)
        return val if val is not None else default

    lines = []
    def row(*cols): lines.append("\t".join(str(c) for c in cols))
    def blank(): lines.append("")

    proj_label   = _get("project_label", "프로젝트")
    period_label = _get("period_label", "")
    gen_ts       = _get("generated_at", "")
    ai_text      = _get("ai_summary", "")
    total_i      = _get("total_issues", 0)
    open_i       = _get("open_issues", 0)
    over_i       = _get("overdue_total", 0)
    members      = len(_get("users", []))
    overdue_list = _get("overdue_issues", [])
    risk_list    = _get("top_risk", [])
    version_rows = _get("versions", [])
    assignee_list= _get("assignee_stats", [])

    row(proj_label, period_label, f"생성: {gen_ts}")
    blank()

    if "signal" in sections and ai_text:
        row("【 WEEKLY SIGNAL / AI 주간 요약 】")
        row(ai_text)
        blank()

    if "metrics" in sections:
        row("【 KEY METRICS 】")
        row("OPEN", "OVERDUE", "ISSUES", "MEMBERS")
        row(total_i, over_i, open_i, members)
        blank()

    if "critical" in sections and overdue_list:
        row(f"【 CRITICAL ITEMS 】 — {len(overdue_list)}건 초과")
        row("#", "제목", "담당", "상태", "마감일", "D-DAY")
        for i in overdue_list:
            row(
                f"#{i.get('id','')}",
                i.get("subject", ""),
                i.get("assignee_short", ""),
                i.get("status", ""),
                i.get("due_date", ""),
                i.get("dday", ""),
            )
        blank()

    if "risk" in sections and risk_list:
        row("【 RISK PROJECTS 】")
        row("프로젝트", "레벨", "SCORE", "초과", "오픈")
        for p in risk_list:
            raw = p.get("risk_score", p.get("score", 0))
            try: score = min(round(float(raw) * 100 / 60), 100)
            except: score = 0
            row(p.get("name",""), p.get("risk_level", p.get("level","")), score,
                p.get("overdue", 0), p.get("open", 0))
        blank()

    if "versions" in sections and version_rows:
        row("【 VERSION PROGRESS 】")
        row("버전", "상태", "마감일", "진행률", "완료", "오픈", "초과")
        for v in version_rows:
            open_cnt = v["total"] - v["closed"]
            row(v["name"], v["badge"], v["due"], f"{v['pct']}%",
                v["closed"], open_cnt, v["overdue"])
        blank()

    if "assignee" in sections and assignee_list:
        row("【 ASSIGNEE LOAD 】")
        row("부서", "담당자", "오픈", "초과")
        for a in assignee_list:
            row(a.get("group",""), a.get("name",""),
                a.get("open", 0), a.get("overdue", 0))
        blank()

    return "\n".join(lines)


def send_report_email(html: str, subject: str, email_cfg) -> dict:
    if not email_cfg.enabled:
        logger.warning("이메일 설정 없음")
        return {"ok": False, "error": "SMTP 설정 없음 — .env 파일을 확인하세요"}
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_cfg.sender
        msg["To"]      = ", ".join(email_cfg.recipients)
        msg.attach(MIMEText(html, "html", "utf-8"))

        if email_cfg.port == 465:
            server = smtplib.SMTP_SSL(email_cfg.host, email_cfg.port)
        else:
            server = smtplib.SMTP(email_cfg.host, email_cfg.port)
            server.starttls()

        server.login(email_cfg.user, email_cfg.password)
        server.sendmail(email_cfg.sender, email_cfg.recipients, msg.as_string())
        server.quit()

        logger.info(f"발송 완료 → {email_cfg.recipients}")
        return {"ok": True, "recipients": email_cfg.recipients}
    except Exception as e:
        logger.error(f"발송 실패: {e}")
        return {"ok": False, "error": str(e)}