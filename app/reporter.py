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

    return ReportData(
        generated_at  = datetime.now().strftime("%Y-%m-%d %H:%M"),
        period_label  = period_label,
        project_label = project_label,
        total_issues  = dashboard.get("total_issues", 0),
        open_issues   = dashboard.get("open_issues",  0),
        overdue_total = dashboard.get("overdue",       0),
        users         = user_summaries,
        overdue_issues= all_overdue,
        top_risk      = top_risk,
    )


def render_html_report(data: ReportData) -> str:
    C = {
        "bg":      "#f7f5f2",
        "surface": "#ffffff",
        "border":  "#e8e6e2",
        "accent":  "#1a3a6e",
        "danger":  "#8b1a1a",
        "warn":    "#a0600a",
        "success": "#1a5c2e",
        "purple":  "#5c3a1a",
        "text":    "#111111",
        "text2":   "#666666",
        "text3":   "#999999",
    }

    def td(val, color=None, bold=False, mono=False, align="left"):
        c = color or C["text2"]
        b = "font-weight:700;" if bold else ""
        m = "font-family:monospace;" if mono else ""
        return (
            f"<td style='padding:10px 14px;border-bottom:1px solid {C['border']};"
            f"font-size:12px;color:{c};{b}{m}text-align:{align};'>{val}</td>"
        )

    def th(label):
        return (
            f"<th style='padding:10px 14px;background:{C['surface']};"
            f"color:{C['text3']};font-size:9px;font-weight:700;"
            f"text-transform:uppercase;letter-spacing:0.2em;"
            f"border-bottom:1px solid {C['border']};text-align:left;'>{label}</th>"
        )

    def dday_html(due_date):
        if not due_date:
            return "-"
        diff = (datetime.strptime(due_date, "%Y-%m-%d").date() - date.today()).days
        danger = C["danger"]
        warn   = C["warn"]
        text3  = C["text3"]
        if diff < 0:
            return f"<span style='color:{danger};font-weight:700;font-family:monospace;'>D+{abs(diff)}</span>"
        if diff == 0:
            return f"<span style='color:{warn};font-weight:700;'>D-Day</span>"
        return f"<span style='color:{text3};font-family:monospace;'>D-{diff}</span>"

    def summary_card(label, value, color):
        bg      = C["surface"]
        border  = C["border"]
        text2   = C["text2"]
        return (
            f"<td style='width:25%;padding:0 8px;'>"
            f"<div style='background:{bg};border:1px solid {border};"
            f"border-radius:0;padding:18px;text-align:center;'>"
            f"<div style='font-size:32px;font-weight:700;color:{color};"
            f"line-height:1;'>{value}</div>"
            f"<div style='font-size:12px;color:{text2};margin-top:6px;'>{label}</div>"
            f"</div></td>"
        )

    # 담당자 행
    user_rows = ""
    for u in data.users:
        row_opacity    = "" if u.overdue_cnt > 0 else "opacity:0.6;"
        overdue_color  = C["danger"] if u.overdue_cnt > 0 else C["text3"]
        overdue_bold   = u.overdue_cnt > 0
        user_rows += (
            f"<tr style='{row_opacity}'>"
            + td(u.dept,        color=C["accent"],  bold=True)
            + td(u.name,        color=C["text"],    bold=True)
            + td(u.total,       align="center")
            + td(u.open_cnt,    color=C["warn"],    bold=True,         align="center")
            + td(u.overdue_cnt, color=overdue_color,bold=overdue_bold, mono=True, align="center")
            + td(u.resolved_cnt,color=C["success"],                    align="center")
            + "</tr>"
        )

    # 초과 이슈 행
    overdue_rows = ""
    for iss in data.overdue_issues[:30]:
        diff = (datetime.now() - datetime.strptime(iss["due_date"], "%Y-%m-%d")).days
        danger = C["danger"]
        overdue_rows += (
            "<tr>"
            + td(f"#{iss['id']}",                                          color=C["accent"], mono=True)
            + td(iss["subject"][:55] + ("…" if len(iss["subject"]) > 55 else ""), color=C["text"])
            + td(iss.get("assignee_short", "-"))
            + td(iss.get("dept", "-"),                                     color=C["purple"])
            + td(iss["status"])
            + td(iss.get("due_date", "-"),                                 mono=True)
            + td(f"<span style='color:{danger};font-weight:700;font-family:monospace;'>D+{diff}</span>")
            + "</tr>"
        )

    if not overdue_rows:
        success = C["success"]
        overdue_rows = (
            f"<tr><td colspan='7' style='padding:24px;text-align:center;"
            f"color:{success};font-size:14px;'>✅ 마감 초과 이슈가 없습니다</td></tr>"
        )

    # 위험 프로젝트 행
    risk_rows = ""
    for p in data.top_risk:
        risk_rows += (
            "<tr>"
            + td(p.get("risk_icon","") + " " + p.get("risk_level",""), color=p.get("risk_color", C["text2"]), bold=True)
            + td(p["name"],               color=C["text"])
            + td(p.get("risk_score","-"), color=p.get("risk_color", C["text2"]), bold=True, mono=True, align="center")
            + td(p.get("overdue",0),      color=C["danger"], align="center")
            + td(p.get("urgent",0),       color=C["warn"],   align="center")
            + td(p.get("open",0),         color=C["accent"], align="center")
            + "</tr>"
        )

    bg      = C["bg"]
    surface = C["surface"]
    border  = C["border"]
    text2   = C["text2"]
    text3   = C["text3"]
    danger  = C["danger"]
    accent  = C["accent"]
    overdue_cnt_label = f"— 상위 30건만 표시" if len(data.overdue_issues) > 30 else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vantix 주간 리포트</title>
</head>
<body style="margin:0;padding:0;background:{bg};font-family:'Apple SD Gothic Neo','Noto Sans KR',Arial,sans-serif;color:{C['text']};">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">

  <div style="background:#111111;border:1px solid {border};border-radius:0;padding:28px 32px;margin-bottom:20px;text-align:center;">
    <div style="font-size:22px;font-weight:700;letter-spacing:0.32em;color:#ffffff;">Vantix 주간 리포트</div>
    <div style="font-size:13px;color:#aaaaaa;margin-top:8px;">{data.period_label} &nbsp;·&nbsp; {data.project_label}</div>
    <div style="font-size:11px;color:{text3};margin-top:4px;font-family:monospace;">생성: {data.generated_at}</div>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
    <tr>
      {summary_card("전체 이슈",  data.total_issues, C['accent'])}
      {summary_card("오픈 이슈",  data.open_issues,  C['warn'])}
      {summary_card("마감 초과",  data.overdue_total,C['danger'])}
      {summary_card("참여 인원",  len(data.users),   C['purple'])}
    </tr>
  </table>

  <div style="background:{surface};border:1px solid {border};border-radius:0;overflow:hidden;margin-bottom:20px;">
    <div style="padding:16px 20px;border-bottom:1px solid {border};">
      <div style="font-size:15px;font-weight:700;color:{C['text']};">위험 프로젝트 Top 5</div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <thead><tr>{th("위험도")}{th("프로젝트")}{th("점수")}{th("초과")}{th("임박")}{th("오픈")}</tr></thead>
      <tbody>{risk_rows}</tbody>
    </table>
  </div>

  <div style="background:#ffffff;border:1px solid #e8e6e2;border-radius:0;overflow:hidden;margin-bottom:20px;">
    <div style="padding:16px 20px;border-bottom:1px solid #e8e6e2;">
      <div style="font-size:15px;font-weight:700;color:{danger};">마감 초과 이슈 ({len(data.overdue_issues)}건) {overdue_cnt_label}</div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <thead><tr>{th("#")}{th("제목")}{th("담당자")}{th("그룹")}{th("상태")}{th("마감일")}{th("경과")}</tr></thead>
      <tbody>{overdue_rows}</tbody>
    </table>
  </div>

  <div style="background:{surface};border:1px solid {border};border-radius:0;overflow:hidden;margin-bottom:20px;">
    <div style="padding:16px 20px;border-bottom:1px solid {border};">
      <div style="font-size:15px;font-weight:700;color:{C['text']};">담당자별 이슈 현황</div>
      <div style="font-size:12px;color:{text3};margin-top:2px;">초과 많은 순 정렬</div>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <thead><tr>{th("그룹")}{th("담당자")}{th("전체")}{th("오픈")}{th("초과")}{th("해결")}</tr></thead>
      <tbody>{user_rows}</tbody>
    </table>
  </div>

  <div style="text-align:center;padding:16px;font-size:11px;color:{text3};">
    Vantix · 자동 생성된 리포트입니다 · {data.generated_at}
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