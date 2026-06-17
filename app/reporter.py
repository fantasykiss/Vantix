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
    name:            str
    dept:            str
    total:           int
    open_cnt:        int
    overdue_cnt:     int
    resolved_cnt:    int
    urgent_cnt:      int  = 0
    in_progress_cnt: int  = 0
    pending_cnt:     int  = 0
    overdue_issues:  list = field(default_factory=list)


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
    insights:           list  = field(default_factory=list)
    risk_score:         int   = 0
    risk_level:         str   = ""
    risk_delta:         float = 0.0
    urgent_total:       int   = 0
    milestone_risk:     int   = 0
    week_history:       list  = field(default_factory=list)
    project_id:         str   = ""
    is_single:          bool  = False



def build_report_data(dashboard: dict, project_label: str = "전체 프로젝트", insights: list = None, project_id: str = "") -> ReportData:
    today_str  = date.today().strftime("%Y-%m-%d")
    users_data = dashboard.get("users_data", {})

    user_summaries = []
    all_overdue    = []

    IN_PROGRESS_STATUSES = {"진행", "In Progress"}
    PENDING_STATUSES     = {"신규", "진행대기", "New", "Pending"}
    d7_str = (date.today().replace(day=date.today().day) + __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")

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
        urgent_cnt = sum(
            1 for i in issues
            if i.get("due_date") and today_str <= i["due_date"] <= d7_str
            and i["status"] not in CLOSED_SET and i["status"] not in HOLD_SET
        )
        in_progress_cnt = sum(1 for i in issues if i["status"] in IN_PROGRESS_STATUSES)
        pending_cnt     = sum(1 for i in issues if i["status"] in PENDING_STATUSES)

        for iss in overdue_issues:
            all_overdue.append({
                **iss,
                "assignee_short": short_name(uname),
                "dept":           dept_name(uname),
            })

        user_summaries.append(UserSummary(
            name            = short_name(uname) or dept_name(uname) or uname,
            dept            = dept_name(uname),
            total           = total,
            open_cnt        = open_cnt,
            overdue_cnt     = len(overdue_issues),
            resolved_cnt    = resolved,
            urgent_cnt      = urgent_cnt,
            in_progress_cnt = in_progress_cnt,
            pending_cnt     = pending_cnt,
            overdue_issues  = overdue_issues,
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

    # ── 리스크 히스토리 & 추가 지표 계산 ──
    snapshot_ts   = ""
    week_history  = []
    risk_delta    = 0.0
    hist_key      = f"project_{project_id}" if project_id else "all"
    try:
        hist_path = os.path.join(os.path.dirname(__file__), "..", "risk_history.json")
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        entries = hist.get(hist_key, hist.get("all", []))
        if entries:
            snapshot_ts  = entries[-1].get("date", "")
            week_history = [
                {"date": e["date"], "score": min(round(e["score"] * 100 / 60), 100), "level": e.get("level", "")}
                for e in entries[-8:]
            ]
            if len(entries) >= 2:
                # 대시보드와 동일: raw score 차이 그대로 사용
                risk_delta = round(entries[-1]["score"] - entries[-2]["score"], 1)
    except Exception:
        pass

    # 전체 리스크 점수 — 대시보드와 동일하게 project_risk[0] (최고위험 프로젝트) 기준
    project_risk_list = dashboard.get("project_risk", [])
    total_i   = dashboard.get("total_issues", 0)
    overdue_i = dashboard.get("overdue", 0)
    urgent_i  = sum(u.urgent_cnt for u in user_summaries)
    if project_risk_list:
        raw_score = project_risk_list[0]["risk_score"]   # 대시보드와 동일: topRisk
    elif total_i:
        raw_score = (overdue_i / total_i * 60) + (urgent_i / total_i * 30)
    else:
        raw_score = 0
    risk_score_norm = min(round(raw_score * 100 / 60), 100)
    risk_level_str  = project_risk_list[0].get("risk_level", (
                       "Critical" if raw_score >= 30 else
                       "High"     if raw_score >= 15 else
                       "Medium"   if raw_score >= 5  else "Low"
                      )) if project_risk_list else "Low"

    milestone_risk = sum(1 for v in version_rows if v["badge"] == "OVERDUE")

    return ReportData(
        generated_at     = datetime.now().strftime("%Y-%m-%d %H:%M"),
        period_label     = period_label,
        project_label    = project_label,
        total_issues     = total_i,
        open_issues      = dashboard.get("open_issues",  0),
        overdue_total    = overdue_i,
        users            = user_summaries,
        overdue_issues   = all_overdue,
        top_risk         = top_risk,
        versions         = version_rows,
        risk_snapshot_ts = snapshot_ts,
        ai_summary       = dashboard.get("ai_summary", ""),
        risk_score       = risk_score_norm,
        risk_level       = risk_level_str,
        risk_delta       = risk_delta,
        urgent_total     = urgent_i,
        milestone_risk   = milestone_risk,
        week_history     = week_history,
        insights         = insights or [],
        project_id       = project_id or "",
        is_single        = bool(project_id),
    )


def render_html_report(report, sections=None, memo="") -> str:

    # ── 데이터 추출 ──
    def _get(key, default):
        val = getattr(report, key, None)
        return val if val is not None else default

    proj_label     = _get("project_label",  "프로젝트")
    period_label   = _get("period_label",   "")
    gen_ts         = _get("generated_at",   datetime.now().strftime("%Y-%m-%d %H:%M"))
    open_i         = _get("open_issues",    0)
    over_i         = _get("overdue_total",  0)
    members        = len(_get("users",      []))
    overdue_list   = _get("overdue_issues", [])
    insights_list  = _get("insights",       [])
    users_list     = _get("users",          [])
    risk_score     = _get("risk_score",     0)
    risk_level     = _get("risk_level",     "")
    risk_delta     = _get("risk_delta",     0.0)
    urgent_total   = _get("urgent_total",   0)
    milestone_risk = _get("milestone_risk", 0)
    week_history   = _get("week_history",   [])
    top_risk_list  = _get("top_risk",       [])
    versions_list  = _get("versions",       [])
    is_single      = _get("is_single",      False)

    # ── 색상 ──
    RED   = "#ef4444"
    AMBER = "#f59e0b"
    GREEN = "#22c55e"
    BLUE  = "#4a8abf"
    MID   = "#9a958e"
    DIM   = "#5a5550"
    GHOST = "#3a3633"

    LEVEL_COLOR = {"Critical": RED, "High": AMBER, "Medium": BLUE, "Low": GREEN}
    risk_color  = LEVEL_COLOR.get(risk_level, MID)

    # ── 게이지 (50 tick, 오버뷰와 동일) ──
    TICKS = 50
    here  = round((min(risk_score, 100) / 100) * TICKS)
    gauge_ticks = ""
    for i in range(TICKS):
        is_now = (i == here)
        if i <= here:
            if   i < TICKS * 0.4: c = "#22c55e"
            elif i < TICKS * 0.7: c = "#f59e0b"
            else:                  c = "#ef4444"
        else:
            c = "rgba(255,255,255,0.06)"
        h = "28px" if is_now else ("20px" if i <= here else "12px")
        gauge_ticks += f"<span style='flex:1;background:{c};height:{h};display:inline-block;align-self:flex-end;'></span>"

    # ── 리스크 코멘트 ──
    delta_sign  = "+" if risk_delta > 0 else ""
    delta_color = RED if risk_delta > 0 else (GREEN if risk_delta < 0 else MID)
    risk_comment = (
        f"마감 초과 <em style='color:{RED};'>{over_i}건</em>, "
        f"D-7 임박 <em style='color:{AMBER};'>{urgent_total}건</em>이 누적되며 "
        f"점수가 <em style='color:{delta_color};'>{delta_sign}{risk_delta}</em> 변동했습니다."
    )

    # ── 트렌드 SVG (grid lines + 컬러 바 + NOW 라벨) ──
    if week_history:
        n        = len(week_history)
        W        = 880          # SVG 전체 폭 (viewBox)
        H_CHART  = 180          # 차트 영역 높이
        H_AXIS   = 22           # x축 레이블 높이
        H_TOTAL  = H_CHART + H_AXIS
        bar_w    = max(18, (W - (n - 1) * 6) // n)
        gap      = 6
        total_w  = n * bar_w + (n - 1) * gap
        x_off    = (W - total_w) // 2
        max_s    = max((e["score"] for e in week_history), default=1) or 1
        max_s    = max(max_s, 75)

        # grid lines at 25 / 50 / 75
        grid = ""
        for gv in (25, 50, 75):
            gy = round(H_CHART * (1 - gv / 100))
            grid += (f"<line x1='0' y1='{gy}' x2='{W}' y2='{gy}' "
                     f"stroke='rgba(255,255,255,0.05)' stroke-dasharray='3,3'/>"
                     f"<text x='{W - 4}' y='{gy - 3}' fill='{GHOST}' font-size='9' "
                     f"text-anchor='end' font-family='DM Mono,monospace'>{gv}</text>")

        svg_bars = ""
        for idx, e in enumerate(week_history):
            x   = x_off + idx * (bar_w + gap)
            bh  = max(4, round((e["score"] / max_s) * (H_CHART - 10)))
            y   = H_CHART - bh
            lv  = e.get("level", "")
            bc  = LEVEL_COLOR.get(lv, "#3a3633")
            op  = "1" if idx == n - 1 else "0.35"
            ds  = e["date"][5:].replace("-", "-") if len(e["date"]) >= 10 else e["date"]
            svg_bars += (f"<rect x='{x}' y='{y}' width='{bar_w}' height='{bh}' "
                         f"fill='{bc}' opacity='{op}' rx='2'/>")
            svg_bars += (f"<text x='{x + bar_w // 2}' y='{H_TOTAL - 4}' fill='{GHOST}' "
                         f"font-size='8' text-anchor='middle' font-family='DM Mono,monospace'>{ds}</text>")
            if idx == n - 1:
                # NOW 라벨
                lx = x + bar_w // 2
                svg_bars += (
                    f"<rect x='{lx - 28}' y='{y - 26}' width='56' height='18' fill='{bc}' rx='2'/>"
                    f"<text x='{lx}' y='{y - 13}' fill='#0c0c0c' font-size='9' font-weight='700' "
                    f"text-anchor='middle' font-family='DM Mono,monospace'>NOW · {e['score']}</text>"
                )

        trend_html = (
            f"<svg viewBox='0 0 {W} {H_TOTAL}' style='width:100%;display:block;overflow:visible;'>"
            f"{grid}{svg_bars}</svg>"
        )
    else:
        trend_html = f"<span style='font-size:12px;color:{DIM};'>트렌드 데이터 없음</span>"

    # ── KPI ribbon 6칸 ──
    kpi_items = [
        ("오픈 이슈",   str(open_i),                "#f2efea", "+0 W/W",           MID),
        ("마감 초과",   str(over_i),                 RED if over_i > 0 else MID,    "건",   MID),
        ("D-7 임박",    str(urgent_total),           AMBER if urgent_total > 0 else MID, "D-7 이내", MID),
        ("완료 위험",   str(milestone_risk),         RED if milestone_risk > 0 else MID,  "마일스톤", MID),
        ("리스크 변화", f"{delta_sign}{risk_delta}", delta_color,                   "전주 대비", delta_color),
        ("투입 인원",   str(members),                "#f2efea",                     "명 진행 중", MID),
    ]
    kpi_cells = ""
    for label, val, val_c, sub, sub_c in kpi_items:
        kpi_cells += (
            f"<div class='kpi-cell'>"
            f"<div class='kpi-k'>{label}</div>"
            f"<div class='kpi-vrow'>"
            f"<span class='kpi-v' style='color:{val_c};'>{val}</span>"
            f"<span class='kpi-s' style='color:{sub_c};'>{sub}</span>"
            f"</div></div>"
        )

    # ── AI Insights ──
    LEVEL_C  = {"critical": RED, "warning": AMBER, "info": BLUE}
    LEVEL_BG = {"critical": "rgba(239,68,68,0.06)", "warning": "rgba(245,158,11,0.06)", "info": "rgba(74,138,191,0.06)"}
    insight_cards = ""
    for ins in insights_list[:6]:
        lv   = ins.get("level", "info")
        body = ins.get("action", ins.get("body", ""))
        who  = ins.get("who", "")
        lc   = LEVEL_C.get(lv, MID)
        lb   = LEVEL_BG.get(lv, "rgba(255,255,255,0.04)")
        who_html = f"<div class='ins-who'>{who}</div>" if who else ""
        insight_cards += (
            f"<div class='ins-card' style='background:{lb};border-color:{lc}22;'>"
            f"<span class='ins-badge' style='color:{lc};'>{lv.upper()}</span>"
            f"<div class='ins-body'>{body}</div>"
            f"{who_html}</div>"
        )
    if not insight_cards:
        insight_cards = f"<div style='color:{DIM};font-size:12px;padding:8px 0;grid-column:1/-1;'>AI 인사이트 없음</div>"

    # ── Issue Matrix ──
    TD  = "padding:10px 14px;border-bottom:1px solid rgba(255,255,255,0.05);text-align:center;"
    TDL = TD + "text-align:left;"
    matrix_rows_html = ""
    totals = {"ov": 0, "ur": 0, "ip": 0, "pe": 0, "re": 0}
    for u in sorted(users_list, key=lambda x: (-getattr(x, "overdue_cnt", 0), -getattr(x, "open_cnt", 0))):
        ov = getattr(u, "overdue_cnt",     0)
        ur = getattr(u, "urgent_cnt",      0)
        ip = getattr(u, "in_progress_cnt", 0)
        pe = getattr(u, "pending_cnt",     0)
        re = getattr(u, "resolved_cnt",    0)
        totals["ov"] += ov; totals["ur"] += ur
        totals["ip"] += ip; totals["pe"] += pe; totals["re"] += re
        matrix_rows_html += (
            f"<tr>"
            f"<td style='{TDL}font-size:10px;color:{MID};white-space:nowrap;'>{u.dept}</td>"
            f"<td style='{TDL}font-size:13px;font-weight:500;color:#f2efea;'>{u.name}</td>"
            f"<td class='mn' style='{TD}color:{RED if ov else DIM};font-weight:{'700' if ov else '400'};'>{ov or '—'}</td>"
            f"<td class='mn' style='{TD}color:{AMBER if ur else DIM};font-weight:{'700' if ur else '400'};'>{ur or '—'}</td>"
            f"<td class='mn' style='{TD}color:{MID};'>{ip or '—'}</td>"
            f"<td class='mn' style='{TD}color:{DIM};'>{pe or '—'}</td>"
            f"<td class='mn' style='{TD}color:{GREEN};'>{re or '—'}</td>"
            f"</tr>"
        )
    matrix_rows_html += (
        f"<tr class='total-row'>"
        f"<td colspan='2' style='{TDL}font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:{GHOST};'>TOTAL</td>"
        f"<td class='mn' style='{TD}color:{RED};font-weight:700;'>{totals['ov'] or '—'}</td>"
        f"<td class='mn' style='{TD}color:{AMBER};font-weight:700;'>{totals['ur'] or '—'}</td>"
        f"<td class='mn' style='{TD}color:{MID};'>{totals['ip'] or '—'}</td>"
        f"<td class='mn' style='{TD}color:{DIM};'>{totals['pe'] or '—'}</td>"
        f"<td class='mn' style='{TD}color:{GREEN};'>{totals['re'] or '—'}</td>"
        f"</tr>"
    )

    # ── PM MEMO ──
    memo_html = ""
    if memo and memo.strip():
        memo_html = (
            f"<section class='report-section'>"
            f"<div class='sec-label'>COMMENT</div>"
            f"<div style='font-size:13px;line-height:1.9;color:{MID};white-space:pre-wrap;margin-top:4px;'>{memo}</div>"
            f"</section>"
        )

    # ══ 컨텍스트 섹션 빌더 ══════════════════════════════════
    # 공통 진행바 헬퍼
    def _bar(pct, color, h=6):
        pct = max(0, min(100, pct))
        return (
            f"<div style='flex:1;height:{h}px;background:rgba(255,255,255,0.06);overflow:hidden;'>"
            f"<div style='width:{pct}%;height:100%;background:{color};'></div></div>"
        )

    # ── AI INSIGHTS (inner) ──
    insights_inner = (
        f"<div class='sec-label'>AI INSIGHTS</div>"
        f"<div class='insights-grid'>{insight_cards}</div>"
    )

    # ── 프로젝트별 현황 (전체 프로젝트 전용) ──
    proj_rows = ""
    for p in top_risk_list:
        pname  = p.get("name", "")
        praw   = p.get("risk_score", 0)
        pnorm  = min(round(praw * 100 / 60), 100)
        plevel = p.get("risk_level", "Low")
        pc     = LEVEL_COLOR.get(plevel, MID)
        pov    = p.get("overdue", 0)
        pur    = p.get("urgent", 0)
        meta   = []
        if pov: meta.append(f"<span style='color:{RED};'>지연 {pov}</span>")
        if pur: meta.append(f"<span style='color:{AMBER};'>임박 {pur}</span>")
        meta_html = " · ".join(meta) if meta else f"<span style='color:{DIM};'>정상</span>"
        proj_rows += (
            f"<div style='display:flex;align-items:center;gap:14px;padding:13px 0;border-bottom:1px solid rgba(255,255,255,0.05);'>"
            f"<span style='width:6px;height:6px;background:{pc};flex-shrink:0;'></span>"
            f"<span style='flex:0 0 200px;font-size:13px;font-weight:500;color:#f2efea;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{pname}</span>"
            f"{_bar(pnorm, pc)}"
            f"<span style='flex:0 0 96px;text-align:right;font-size:10px;color:{MID};'>{meta_html}</span>"
            f"<span class='mn' style='flex:0 0 38px;text-align:right;font-size:15px;font-weight:700;color:{pc};'>{pnorm}</span>"
            f"</div>"
        )
    if not proj_rows:
        proj_rows = f"<div style='color:{DIM};font-size:12px;padding:8px 0;'>프로젝트 데이터 없음</div>"
    projects_inner = (
        f"<div class='sec-label'>프로젝트별 리스크 현황 <span style='color:{GHOST};font-weight:400;'>/ RISK BY PROJECT</span></div>"
        f"{proj_rows}"
    )

    # ── 마일스톤별 현황 (단일 프로젝트 전용) ──
    BADGE_C = {"OVERDUE": RED, "CLOSED": GREEN, "LOCKED": DIM, "OPEN": MID}
    ms_rows = ""
    for v in versions_list:
        badge = v.get("badge", "OPEN")
        bc    = BADGE_C.get(badge, MID)
        pct   = v.get("pct", 0)
        ovc   = v.get("overdue", 0)
        bar_c = RED if ovc > 0 else GREEN
        due   = v.get("due", "")
        due_html = f"<span style='font-size:10px;color:{DIM};' class='mn'>{due}</span>" if due else ""
        ms_rows += (
            f"<div style='display:flex;align-items:center;gap:14px;padding:13px 0;border-bottom:1px solid rgba(255,255,255,0.05);'>"
            f"<span style='flex:0 0 180px;display:flex;flex-direction:column;gap:3px;min-width:0;'>"
            f"<span style='font-size:13px;font-weight:500;color:#f2efea;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{v.get('name','')}</span>"
            f"{due_html}</span>"
            f"<span style='flex:0 0 64px;font-family:\"DM Mono\",monospace;font-size:8px;letter-spacing:0.08em;color:{bc};'>{badge}</span>"
            f"{_bar(pct, bar_c)}"
            f"<span class='mn' style='flex:0 0 96px;text-align:right;font-size:10px;color:{MID};'>"
            f"{v.get('closed',0)}/{v.get('total',0)}"
            + (f" <span style='color:{RED};'>· 지연 {ovc}</span>" if ovc else "")
            + f"</span>"
            f"<span class='mn' style='flex:0 0 38px;text-align:right;font-size:15px;font-weight:700;color:{bar_c};'>{pct}%</span>"
            f"</div>"
        )
    if not ms_rows:
        ms_rows = f"<div style='color:{DIM};font-size:12px;padding:8px 0;'>마일스톤(버전) 데이터 없음</div>"
    milestones_inner = (
        f"<div class='sec-label'>마일스톤별 진행 현황 <span style='color:{GHOST};font-weight:400;'>/ MILESTONES</span></div>"
        f"{ms_rows}"
    )

    # ── 담당자 부하 (단일 프로젝트 전용) ──
    load_users = sorted(users_list, key=lambda u: -getattr(u, "open_cnt", 0))
    max_load   = max((getattr(u, "open_cnt", 0) for u in load_users), default=1) or 1
    wl_rows = ""
    for u in load_users:
        opn = getattr(u, "open_cnt",    0)
        ovd = getattr(u, "overdue_cnt", 0)
        urg = getattr(u, "urgent_cnt",  0)
        pct = round(opn / max_load * 100)
        # 과부하 판정: 부하 상위(80%+) AND 지연 보유 → 과부하 / 지연만 → 주의 / 그 외 정상·여유
        if pct >= 80 and ovd > 0:
            state, sc, bar_c = "과부하", RED, RED
        elif ovd > 0:
            state, sc, bar_c = "지연", AMBER, AMBER
        elif pct >= 50:
            state, sc, bar_c = "정상", MID, BLUE
        else:
            state, sc, bar_c = "여유", DIM, GREEN
        ini = (u.name or "·")[0]
        sub = []
        if ovd: sub.append(f"<span style='color:{RED};'>지연 {ovd}</span>")
        if urg: sub.append(f"<span style='color:{AMBER};'>임박 {urg}</span>")
        sub_html = " · ".join(sub) if sub else ""
        sub_line = (f"<span style='font-size:9px;color:{MID};white-space:nowrap;'>{sub_html}</span>") if sub_html else ""
        wl_rows += (
            f"<div style='display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid rgba(255,255,255,0.05);'>"
            f"<span style='width:26px;height:26px;background:{bar_c};color:#0c0c0c;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;'>{ini}</span>"
            f"<span style='flex:0 0 120px;display:flex;flex-direction:column;gap:2px;min-width:0;'>"
            f"<span style='font-size:13px;font-weight:500;color:#f2efea;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{u.name}</span>"
            + (f"<span style='font-size:9px;color:{DIM};'>{u.dept}</span>" if getattr(u, 'dept', '') else "")
            + f"</span>"
            f"{_bar(pct, bar_c)}"
            f"<span style='flex:0 0 92px;text-align:right;display:flex;flex-direction:column;gap:2px;align-items:flex-end;'>"
            f"<span class='mn' style='font-size:11px;color:{sc};font-weight:700;white-space:nowrap;'>{opn}건 · {state}</span>"
            f"{sub_line}</span>"
            f"</div>"
        )
    if not wl_rows:
        wl_rows = f"<div style='color:{DIM};font-size:12px;padding:8px 0;'>담당자 데이터 없음</div>"
    workload_inner = (
        f"<div class='sec-label'>담당자별 부하 현황 <span style='color:{GHOST};font-weight:400;'>/ ASSIGNEE LOAD</span></div>"
        f"{wl_rows}"
    )

    # ── 이슈 매트릭스 (inner, 전체 프로젝트 전용) ──
    matrix_inner = (
        f"<div class='matrix-head'>"
        f"<div class='sec-label' style='margin-bottom:0;'>ISSUE MATRIX</div>"
        f"<span class='matrix-badge'>{len(overdue_list)} OVERDUE</span></div>"
        f"<table><thead><tr>"
        f"<th class='thl'>Dept</th><th class='thl'>Name</th>"
        f"<th>마감초과</th><th>D-7임박</th><th>진행중</th><th>진행대기</th><th>해결</th>"
        f"</tr></thead><tbody>{matrix_rows_html}</tbody></table>"
    )

    # ── 섹션 조립 (전체/단일 분기 + sections 순서 제어 + 2컬럼 그리드) ──
    if is_single:
        inner_map = {"insights": insights_inner, "milestones": milestones_inner, "workload": workload_inner}
        span_map  = {"milestones": "half", "workload": "half", "insights": "full"}
        default_order = ["milestones", "workload", "insights"]
    else:
        inner_map = {"insights": insights_inner, "projects": projects_inner, "matrix": matrix_inner}
        span_map  = {"projects": "half", "insights": "half", "matrix": "full"}
        default_order = ["projects", "insights", "matrix"]

    if sections:
        order = [s for s in sections if s in inner_map]
    else:
        order = default_order

    # half 섹션은 2개씩 묶어 2컬럼 row, full 섹션은 단독 풀폭 row
    def _half_row(cells):
        return f"<div class='ctx-row ctx-row-2'>" + "".join(
            f"<div class='ctx-cell'>{c}</div>" for c in cells
        ) + "</div>"

    rows_html = ""
    buf = []
    for s in order:
        inner = inner_map.get(s, "")
        if not inner:
            continue
        if span_map.get(s, "full") == "half":
            buf.append(inner)
            if len(buf) == 2:
                rows_html += _half_row(buf); buf = []
        else:
            if buf:
                rows_html += _half_row(buf); buf = []
            rows_html += f"<div class='ctx-row ctx-row-1'><div class='ctx-cell'>{inner}</div></div>"
    if buf:
        rows_html += _half_row(buf)
    context_sections = rows_html

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{proj_label} 리스크 브리핑</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,700;1,9..40,400&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0c0c0c; color:#f2efea; font-family:'DM Sans',sans-serif; -webkit-font-smoothing:antialiased; }}
table {{ border-collapse:collapse; width:100%; }}

.page {{ max-width:1180px; margin:0 auto; padding:0 40px 80px; }}

/* ── header ── */
.report-header {{
  display:flex; justify-content:space-between; align-items:flex-end;
  padding:48px 0 28px;
  border-bottom:1px solid rgba(255,255,255,0.07);
  margin-bottom:0;
}}
.hd-eyebrow {{ font-family:'DM Mono',monospace; font-size:9px; font-weight:500; letter-spacing:0.22em; color:{GHOST}; text-transform:uppercase; margin-bottom:10px; }}
.hd-title   {{ font-size:32px; font-weight:700; letter-spacing:-0.03em; color:#f2efea; line-height:0.95; }}
.hd-period  {{ font-family:'DM Mono',monospace; font-size:11px; color:{DIM}; margin-top:10px; }}
.hd-right   {{ text-align:right; }}
.hd-gen-lbl {{ font-family:'DM Mono',monospace; font-size:9px; color:{GHOST}; letter-spacing:0.15em; text-transform:uppercase; }}
.hd-gen-ts  {{ font-family:'DM Mono',monospace; font-size:11px; color:{DIM}; margin-top:5px; }}

/* ── section ── */
.report-section {{
  padding:32px 0;
  border-bottom:1px solid rgba(255,255,255,0.07);
}}
.sec-label {{
  font-family:'DM Mono',monospace; font-size:9px; font-weight:500;
  letter-spacing:0.22em; color:{GHOST}; text-transform:uppercase;
  margin-bottom:22px;
}}

/* ── HERO ── */
.hero {{ display:grid; grid-template-columns:300px 1fr; gap:0; border-bottom:1px solid rgba(255,255,255,0.07); }}
.hero-risk   {{ padding:32px 32px 32px 0; border-right:1px solid rgba(255,255,255,0.07); }}
.hero-trend  {{ padding:32px 0 32px 32px; }}

/* risk number */
.risk-block {{ display:flex; align-items:flex-start; gap:16px; }}
.risk-num   {{ font-size:96px; font-weight:700; line-height:0.85; letter-spacing:-0.06em; color:#f2efea; }}
.risk-meta  {{ padding-top:10px; }}
.risk-state {{
  display:inline-flex; align-items:center; gap:8px;
  padding:5px 12px; font-size:12px; font-weight:700; letter-spacing:0.08em;
  color:#0c0c0c;
}}
.risk-dot {{ width:6px; height:6px; background:#0c0c0c; border-radius:0; flex-shrink:0; }}

/* gauge */
.gauge {{ margin-top:28px; }}
.gauge-bars {{
  display:flex; align-items:flex-end; gap:2px; height:28px;
}}
.gauge-axis {{
  display:flex; justify-content:space-between; margin-top:6px;
  font-size:9px; font-family:'DM Mono',monospace; color:{DIM}; letter-spacing:0.1em;
}}

/* risk comment */
.risk-comment {{
  margin-top:22px; padding-top:18px;
  border-top:1px solid rgba(255,255,255,0.06);
  display:flex; align-items:flex-start; gap:10px;
}}
.risk-comment .sparkle {{ color:{AMBER}; font-size:13px; flex-shrink:0; line-height:1.6; }}
.risk-comment .body    {{ font-size:12px; color:#f2efea; line-height:1.7; font-weight:500; }}

/* trend head */
.trend-head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:20px; }}
.trend-label {{ font-family:'DM Mono',monospace; font-size:9px; font-weight:500; letter-spacing:0.22em; color:{GHOST}; text-transform:uppercase; }}
.trend-sub   {{ font-family:'DM Mono',monospace; font-size:10px; color:{DIM}; margin-top:5px; }}

/* ── KPI ribbon ── */
.kpi-ribbon {{
  display:grid; grid-template-columns:repeat(6,1fr);
  border-bottom:1px solid rgba(255,255,255,0.07);
}}
.kpi-cell {{
  padding:24px 20px;
  border-right:1px solid rgba(255,255,255,0.07);
}}
.kpi-cell:last-child {{ border-right:none; }}
.kpi-k    {{ font-size:11px; color:{MID}; margin-bottom:10px; }}
.kpi-vrow {{ display:flex; align-items:baseline; gap:7px; flex-wrap:wrap; }}
.kpi-v    {{ font-size:32px; font-weight:700; line-height:1; letter-spacing:-0.04em; }}
.kpi-s    {{ font-size:10px; }}

/* ── AI insights ── */
.insights-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.ins-card  {{ padding:18px 20px; border:1px solid rgba(255,255,255,0.07); }}
.ins-badge {{ font-family:'DM Mono',monospace; font-size:8px; font-weight:500; letter-spacing:0.1em; }}
.ins-body  {{ font-size:12px; font-weight:500; color:#f2efea; line-height:1.65; margin-top:8px; }}
.ins-who   {{ font-size:10px; color:{MID}; margin-top:6px; }}

/* ── matrix ── */
.matrix-head {{
  display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;
}}
.matrix-badge {{
  font-family:'DM Mono',monospace; font-size:8px; color:{RED};
  background:rgba(239,68,68,0.08); padding:3px 10px; letter-spacing:0.08em;
}}
.mn {{ font-family:'DM Mono',monospace; font-size:13px; }}
th  {{
  font-family:'DM Mono',monospace; font-size:8px; font-weight:500;
  letter-spacing:0.12em; color:{GHOST}; text-transform:uppercase;
  border-bottom:1px solid rgba(255,255,255,0.07); padding:9px 14px;
  white-space:nowrap; text-align:center;
}}
th.thl {{ text-align:left; }}
.total-row {{ background:rgba(255,255,255,0.02); }}

/* ── 2컬럼 컨텍스트 그리드 ── */
.ctx-row {{ border-bottom:1px solid rgba(255,255,255,0.07); }}
.ctx-row-2 {{ display:grid; grid-template-columns:1fr 1fr; }}
.ctx-row-2 .ctx-cell {{ padding:32px 0; min-width:0; }}
.ctx-row-2 .ctx-cell:first-child {{ padding-right:40px; border-right:1px solid rgba(255,255,255,0.07); }}
.ctx-row-2 .ctx-cell:last-child {{ padding-left:40px; }}
.ctx-row-1 .ctx-cell {{ padding:32px 0; }}
@media (max-width:760px) {{
  .ctx-row-2 {{ grid-template-columns:1fr; }}
  .ctx-row-2 .ctx-cell:first-child {{ padding-right:0; border-right:none; border-bottom:1px solid rgba(255,255,255,0.07); }}
  .ctx-row-2 .ctx-cell:last-child {{ padding-left:0; }}
}}
</style>
</head>
<body>
<div class="page">

  <!-- HEADER -->
  <header class="report-header">
    <div>
      <div class="hd-eyebrow">Vantix · Risk Briefing</div>
      <div class="hd-title">{proj_label}</div>
      <div class="hd-period">{period_label}</div>
    </div>
    <div class="hd-right">
      <div class="hd-gen-lbl">Generated</div>
      <div class="hd-gen-ts">{gen_ts}</div>
    </div>
  </header>

  <!-- HERO: Risk Score + Trend -->
  <div class="hero">
    <div class="hero-risk">
      <div class="sec-label">CURRENT RISK INDEX <span style="color:{GHOST};font-weight:400;">/100</span></div>
      <div class="risk-block">
        <div class="risk-num" style="color:{risk_color};">{risk_score}</div>
        <div class="risk-meta">
          <div class="risk-state" style="background:{risk_color};">
            <span class="risk-dot"></span>{risk_level.upper()}
          </div>
        </div>
      </div>
      <div class="gauge">
        <div class="gauge-bars">{gauge_ticks}</div>
        <div class="gauge-axis">
          <span>0 LOW</span><span>40</span><span>70 HIGH</span><span>100 CRIT</span>
        </div>
      </div>
      <div class="risk-comment">
        <span class="sparkle">✦</span>
        <div class="body">{risk_comment}</div>
      </div>
    </div>
    <div class="hero-trend">
      <div class="trend-head">
        <div>
          <div class="trend-label">RISK SCORE TRENDS</div>
        </div>
      </div>
      {trend_html}
    </div>
  </div>

  <!-- KPI RIBBON -->
  <div class="kpi-ribbon">
    {kpi_cells}
  </div>

  <!-- CONTEXT SECTIONS (전체/단일 분기) -->
  {context_sections}

  {memo_html}

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