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
        sections = ["signal", "metrics", "critical", "risk", "versions", "assignee"]

    # ── 디자인 토큰 ──
    FONT_URL  = ""
    F_SANS    = "font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:12px;"
    F_MONO    = "font-family:'SF Mono','Menlo','Courier New',monospace;font-size:11px;"
    BG        = "#f7f5f2"
    WHITE     = "#ffffff"
    BORDER    = "1px solid #e8e6e2"
    BORDER_H  = "0.5px solid #f0eeea"
    RED       = "#B40023"
    AMBER     = "#a0600a"
    GREEN     = "#1a5c2e"
    NAVY      = "#1a3a6e"
    MUTED     = "#888888"
    LABEL_CSS = "font-size:9px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#999999;"
    TH_CSS    = f"padding:9px 14px;background:{WHITE};{LABEL_CSS}border-bottom:{BORDER};text-align:left;"

    def _td(val, color="#111111", align="left", mono=False, bold=False):
        fw = "700" if bold else "400"
        fm = F_MONO if mono else ""
        return (f"<td style='padding:9px 14px;border-bottom:{BORDER_H};"
                f"font-size:12px;color:{color};font-weight:{fw};{fm}"
                f"text-align:{align};'>{val}</td>")

    def _badge(text, bg, color):
        return (f"<span style='font-size:9px;letter-spacing:0.1em;"
                f"padding:2px 7px;background:{bg};color:{color};'>{text}</span>")

    def _section(head_html, body_html):
        return (f"<div style='background:{WHITE};border:{BORDER};"
                f"margin-bottom:16px;overflow:hidden;'>"
                f"{head_html}{body_html}</div>")

    def _head(title, sub=""):
        sub_html = (f"<span style='font-size:10px;color:#bbbbbb;"
                    f"letter-spacing:0.05em;margin-left:10px;'>{sub}</span>") if sub else ""
        return (f"<div style='padding:13px 20px;border-bottom:{BORDER};'>"
                f"<span style='font-size:11px;font-weight:700;"
                f"letter-spacing:0.12em;color:#111111;{F_SANS}'>{title}</span>"
                f"{sub_html}</div>")

    # ── 데이터 추출 헬퍼 ──
    def _get(key, default):
        val = getattr(report, key, None)
        return val if val is not None else default

    proj_label   = _get("project_label", "프로젝트")
    period_label = _get("period_label",  "")
    gen_ts       = _get("generated_at",  datetime.now().strftime("%Y-%m-%d %H:%M"))
    ai_text      = _get("ai_summary",    "")
    total_i      = _get("total_issues",  0)
    open_i       = _get("open_issues",   0)
    over_i       = _get("overdue_total", 0)
    members      = len(_get("users",     []))
    overdue_list = _get("overdue_issues",  [])
    risk_list    = _get("top_risk",      [])
    snap_ts      = _get("risk_snapshot_ts", "")
    version_rows = _get("versions",      [])
    assignee_list= _get("assignee_stats",[])

    snap_label = f"기준: {snap_ts} 스냅샷" if snap_ts else "기준: 실시간"

    # ── 1. WEEKLY SIGNAL ──
    s_signal = ""
    if "signal" in sections and ai_text:
        s_signal = f"""
<div style='background:{WHITE};border-left:3px solid {RED};border-top:{BORDER};border-right:{BORDER};border-bottom:{BORDER};padding:16px 20px;margin-bottom:16px;'>
  <div style='{LABEL_CSS}{F_SANS}margin-bottom:7px;'>WEEKLY SIGNAL / AI 주간 요약</div>
  <div style='{F_SANS}font-size:13px;line-height:1.75;color:#111111;'>{ai_text}</div>
</div>"""

    # ── 2. KEY METRICS ──
    s_metrics = ""
    if "metrics" in sections:
        def _mcard(num, label, sub, color):
            return (f"<td style='width:25%;padding:0 5px;'>"
                    f"<div style='background:{WHITE};border:{BORDER};padding:16px;text-align:center;'>"
                    f"<div style='font-size:28px;font-weight:700;color:{color};line-height:1;{F_MONO}'>{num}</div>"
                    f"<div style='{LABEL_CSS}{F_SANS}margin-top:5px;'>{label}</div>"
                    f"<div style='font-size:10px;color:#bbbbbb;{F_SANS}margin-top:2px;'>{sub}</div>"
                    f"</div></td>")
        s_metrics = f"""
<table width='100%' cellpadding='0' cellspacing='0' style='margin-bottom:16px;'>
  <tr>
    {_mcard(total_i, 'OPEN',    '전체 이슈', NAVY)}
    {_mcard(over_i,  'OVERDUE', '마감 초과', RED)}
    {_mcard(open_i,  'ISSUES',  '오픈 이슈', AMBER)}
    {_mcard(members, 'MEMBERS', '참여 인원', GREEN)}
  </tr>
</table>"""

    # ── 3. CRITICAL ITEMS ──
    s_critical = ""
    if "critical" in sections and overdue_list:
        rows = "".join(
            f"<tr>"
            f"{_td('#'+str(i.get('id','')), NAVY, mono=True)}"
            f"{_td(i.get('subject',''))}"
            f"{_td(i.get('assignee_short',''), MUTED)}"
            f"{_td(i.get('status',''),   MUTED)}"
            f"{_td(i.get('due_date',''), MUTED, mono=True)}"
            f"{_td(str(i.get('dday','')), RED,  align='right', mono=True, bold=True)}"
            f"</tr>"
            for i in overdue_list
        )
        cnt_badge = _badge(f"{len(overdue_list)} OVERDUE", "#fdecea", RED)
        s_critical = _section(
            _head("CRITICAL ITEMS / 즉시 처리"),
            f"<div style='padding:8px 20px;text-align:right;border-bottom:{BORDER_H};'>{cnt_badge}</div>"
            f"<table width='100%' cellpadding='0' cellspacing='0'>"
            f"<thead><tr>"
            f"<th style='{TH_CSS}'>#</th>"
            f"<th style='{TH_CSS}'>제목</th>"
            f"<th style='{TH_CSS}'>담당</th>"
            f"<th style='{TH_CSS}'>상태</th>"
            f"<th style='{TH_CSS}'>마감일</th>"
            f"<th style='{TH_CSS}text-align:right;'>D-DAY</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )

    # ── 4. RISK PROJECTS ──
    s_risk = ""
    if "risk" in sections and risk_list:
        lv_map = {
            "Critical": ("#fdecea", RED),
            "High":     ("#fff3e0", AMBER),
            "Medium":   ("#f5f3f3", "#555555"),
            "Low":      ("#e8f5e9", GREEN),
        }
        def _norm_score(p):
            raw = p.get('risk_score', p.get('score', 0))
            try: return min(round(float(raw) * 100 / 60), 100)
            except: return 0
        rows = "".join(
            f"<tr>"
            f"{_td(p.get('name',''), bold=True)}"
            f"{_td(_badge(p.get('risk_level',p.get('level','')).upper(), *lv_map.get(p.get('risk_level',p.get('level','')), ('#f5f3f3','#555'))))}"
            f"{_td(str(_norm_score(p)), RED if p.get('risk_level',p.get('level',''))=='Critical' else AMBER, align='right', mono=True, bold=True)}"
            f"{_td(str(p.get('overdue',0)), RED, align='center', bold=True)}"
            f"{_td(str(p.get('open',0)), MUTED, align='center')}"
            f"</tr>"
            for p in risk_list
        )
        s_risk = _section(
            _head("RISK PROJECTS / 위험 프로젝트", snap_label),
            f"<table width='100%' cellpadding='0' cellspacing='0'>"
            f"<thead><tr>"
            f"<th style='{TH_CSS}'>프로젝트</th>"
            f"<th style='{TH_CSS}'>레벨</th>"
            f"<th style='{TH_CSS}text-align:right;'>SCORE</th>"
            f"<th style='{TH_CSS}text-align:center;'>초과</th>"
            f"<th style='{TH_CSS}text-align:center;'>오픈</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )

    # ── 5. VERSION PROGRESS ──
    s_versions = ""
    if "versions" in sections and version_rows:
        badge_styles = {
            "CLOSED":  ("#111111", "#ffffff"),
            "OVERDUE": ("#fdecea", RED),
            "LOCKED":  ("#f5f3f3", "#888888"),
            "OPEN":    ("#e8f5e9", GREEN),
        }
        rows_html = ""
        for v in version_rows:
            bb, bc = badge_styles.get(v["badge"], ("#f5f3f3","#555"))
            due_color = RED if v["badge"] == "OVERDUE" else "#bbbbbb"
            overdue_color = RED if v["overdue"] > 0 else "#bbbbbb"
            rows_html += f"""
<div style='padding:9px 20px;border-bottom:{BORDER_H};display:flex;align-items:center;gap:12px;'>
  <div style='{F_SANS}font-size:11px;font-weight:500;color:#111111;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{v["name"]}</div>
  <div style='{F_MONO}font-size:9px;color:{due_color};min-width:64px;'>{v["due"]}</div>
  <div style='font-size:9px;letter-spacing:0.08em;padding:1px 6px;background:{bb};color:{bc};min-width:44px;text-align:center;'>{v["badge"]}</div>
  <div style='width:100px;height:3px;background:#f0eeea;flex-shrink:0;'>
    <div style='height:3px;background:{v["bar_color"]};width:{v["pct"]}%;'></div>
  </div>
  <div style='{F_MONO}font-size:10px;font-weight:700;color:{v["bar_color"]};min-width:28px;text-align:right;'>{v["pct"]}%</div>
  <div style='display:flex;gap:0;flex-shrink:0;'>
    <div style='width:44px;text-align:center;'>
      <div style='font-family:monospace;font-size:8px;color:#bbbbbb;letter-spacing:0.05em;'>완료</div>
      <div style='font-family:monospace;font-size:12px;font-weight:700;color:#111111;'>{v["closed"]}</div>
    </div>
    <div style='width:44px;text-align:center;'>
      <div style='font-family:monospace;font-size:8px;color:#bbbbbb;letter-spacing:0.05em;'>오픈</div>
      <div style='font-family:monospace;font-size:12px;font-weight:700;color:{MUTED};'>{v["total"]-v["closed"]}</div>
    </div>
    <div style='width:44px;text-align:center;'>
      <div style='font-family:monospace;font-size:8px;color:#bbbbbb;letter-spacing:0.05em;'>초과</div>
      <div style='font-family:monospace;font-size:12px;font-weight:700;color:{overdue_color};'>{v["overdue"]}</div>
    </div>
  </div>
</div>"""
        s_versions = _section(
            _head(f"VERSION PROGRESS / 버전별 진행상태", f"{len(version_rows)} VERSIONS"),
            rows_html
        )

    # ── 6. ASSIGNEE LOAD ──
    s_assignee = ""
    if "assignee" in sections and assignee_list:
        max_open = max((a.get("open", 0) for a in assignee_list), default=1) or 1
        rows = ""
        for a in assignee_list:
            pct = round(a.get("open", 0) / max_open * 100)
            has_over = a.get("overdue", 0) > 0
            bar_c = RED if has_over else "#111111"
            stat_c = RED if has_over else MUTED
            rows += f"""
<div style='padding:10px 20px;border-bottom:{BORDER_H};display:flex;align-items:center;gap:12px;'>
  <div style='{F_SANS}font-size:12px;color:#111111;min-width:100px;'>{a.get("group","")}&nbsp;{a.get("name","")}</div>
  <div style='flex:1;height:3px;background:#f0eeea;'>
    <div style='height:3px;background:{bar_c};width:{pct}%;'></div>
  </div>
  <div style='{F_SANS}font-size:10px;color:{stat_c};min-width:120px;text-align:right;'>오픈 {a.get("open",0)} · 초과 {a.get("overdue",0)}</div>
</div>"""
        s_assignee = _section(
            _head("ASSIGNEE LOAD / 담당자 부하", "초과 많은 순"),
            rows
        )

    # ── 7. PM MEMO ──
    s_memo = ""
    if memo and memo.strip():
        s_memo = f"""
<div style='background:{WHITE};border:{BORDER};border-left:3px solid #111111;padding:16px 20px;margin-bottom:16px;'>
  <div style='{LABEL_CSS}{F_SANS}margin-bottom:7px;'>PM COMMENT / 리포트 메모</div>
  <div style='{F_SANS}font-size:13px;line-height:1.75;color:#111111;white-space:pre-wrap;'>{memo}</div>
</div>"""

    # ── 최종 반환 ──
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{proj_label} 주간 리포트</title>
<style>*{{font-size:12px;box-sizing:border-box;}}</style>
</head>
<body style="margin:0;padding:0;background:{BG};{F_SANS}color:#111111;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">

  <div style="background:#111111;padding:24px 32px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
    <div>
      <div style="font-size:18px;font-weight:700;letter-spacing:0.05em;color:#ffffff;{F_SANS}">{proj_label}</div>
      <div style="font-size:11px;color:#aaaaaa;margin-top:4px;{F_SANS}">{period_label}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:9px;letter-spacing:0.15em;color:#666666;{F_MONO}">WEEKLY REPORT</div>
      <div style="font-size:10px;color:#666666;margin-top:3px;{F_MONO}">생성: {gen_ts}</div>
    </div>
  </div>

  {s_signal}
  {s_metrics}
  {s_critical}
  {s_risk}
  {s_versions}
  {s_assignee}
  {s_memo}

  <div style="text-align:center;padding:20px 0 8px;border-top:1px solid #e8e6e2;margin-top:8px;">
    <div style="font-size:10px;letter-spacing:0.2em;color:#bbbbbb;{F_MONO}">VANTIX</div>
    <div style="font-size:10px;color:#cccccc;margin-top:3px;{F_SANS}">자동 생성된 리포트 · {gen_ts}</div>
  </div>

</div>
</body>
</html>"""


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