"""
insights.py — Rule-based insight engine for Vantix
14가지 룰을 실행해서 Insight 리스트를 반환합니다. AI API 불필요.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from app.constants import CLOSED_SET, HOLD_SET, dept_name, short_name


@dataclass
class Insight:
    rule:   str   # "VERSION_OVERRUN" 등
    level:  str   # "critical" | "warning" | "info"
    title:  str   # 한글 제목
    body:   str   # 상세 메시지
    target: str   # "v1.2 · Vantix_KR" 등


def _all_issues(dashboard: dict) -> list:
    """users_data에서 모든 이슈를 중복 없이 추출"""
    seen, result = set(), []
    for ud in dashboard.get("users_data", {}).values():
        for i in ud.get("issues", []):
            iid = i.get("id")
            if iid not in seen:
                seen.add(iid)
                result.append(i)
    return result


def _today() -> str:
    return date.today().isoformat()


def _days_diff(date_str: str) -> int:
    """오늘 기준 일수 차이 (양수=미래, 음수=과거)"""
    try:
        return (date.fromisoformat(date_str) - date.today()).days
    except Exception:
        return 0


def _is_open(issue: dict) -> bool:
    return issue.get("status", "") not in CLOSED_SET


def _is_hold(issue: dict) -> bool:
    return issue.get("status", "") in HOLD_SET


def _is_overdue(issue: dict) -> bool:
    dd = issue.get("due_date", "")
    return bool(dd and dd < _today() and _is_open(issue) and not _is_hold(issue))


# ── Rule 1: VERSION_OVERRUN ──────────────────────────────────────
def rule_version_overrun(dashboard: dict) -> list[Insight]:
    """버전 마감일보다 늦은 완료일을 가진 이슈 존재"""
    results = []
    today = _today()
    for v in dashboard.get("versions", []):
        due = v.get("due_date", "")
        if not due or v.get("status") == "closed":
            continue
        v_issues = v.get("issues", [])
        if not v_issues:
            continue
        late = [i for i in v_issues
                if i.get("due_date", "") and i["due_date"] > due and _is_open(i)]
        if late:
            max_due = max(i["due_date"] for i in late)
            results.append(Insight(
                rule="VERSION_OVERRUN", level="critical",
                title="버전 일정 초과",
                body=f"<strong>{v.get('name','?')}</strong> 마감({due}) 대비 이슈 완료 예정일이 <strong>{max_due}</strong>까지 밀려 있습니다. 일정 초과가 이미 확정된 상태로, 마감 기준 재조정 또는 스코프 축소가 필요합니다.",
                target=f"{v.get('name','?')}",
            ))
    return results


# ── Rule 2: TEST_BUFFER ──────────────────────────────────────────
def rule_test_buffer(dashboard: dict) -> list[Insight]:
    """버전 마감일과 마지막 이슈 완료일 사이 여유 < 3일"""
    results = []
    for v in dashboard.get("versions", []):
        due = v.get("due_date", "")
        if not due or v.get("status") == "closed":
            continue
        v_issues = v.get("issues", [])
        if not v_issues:
            continue
        open_dues = [i["due_date"] for i in v_issues
                     if i.get("due_date") and _is_open(i) and i["due_date"] <= due]
        if not open_dues:
            continue
        last_dev = max(open_dues)
        buffer = _days_diff(due) - _days_diff(last_dev)
        if 0 < buffer < 3:
            results.append(Insight(
                rule="TEST_BUFFER", level="warning",
                title="QA 버퍼 부족",
                body=f"<strong>{v.get('name','?')}</strong> 개발 완료({last_dev}) 후 마감({due})까지 QA 버퍼가 <strong>{buffer}일</strong>뿐입니다. 예기치 않은 결함 발견 시 릴리즈 지연이 불가피하며, 일정 조정 또는 사전 QA 착수를 권고합니다.",
                target=f"{v.get('name','?')}",
            ))
    return results


# ── Rule 3: ASSIGNEE_DELAY_PATTERN ──────────────────────────────
def rule_assignee_delay_pattern(dashboard: dict) -> list[Insight]:
    """담당자 초과 비율 > 40% (최소 3개 이슈)"""
    results = []
    today = _today()
    for uname, ud in dashboard.get("users_data", {}).items():
        issues = ud.get("issues", [])
        total = len([i for i in issues if _is_open(i)])
        if total < 3:
            continue
        overdue = [i for i in issues if _is_overdue(i)]
        ratio = len(overdue) / total if total else 0
        if ratio >= 0.4:
            name = short_name(uname)
            dept = dept_name(uname)
            level = "critical" if ratio >= 0.7 else "warning"
            title = "담당자 지연 위험" if level == "critical" else "담당자 지연 패턴"
            if level == "critical":
                body = (f"<strong>{name}</strong>의 지연율이 <strong>{round(ratio*100)}%</strong>로 임계치를 크게 초과했습니다. "
                        f"오픈 이슈 {total}건 중 {len(overdue)}건이 기한을 넘긴 상태로, 연관 마일스톤에 연쇄 영향이 예상됩니다.")
            else:
                body = (f"<strong>{name}</strong>의 업무 패턴에서 반복적 지연 신호가 감지됩니다. "
                        f"오픈 이슈 {total}건 중 {round(ratio*100)}%({len(overdue)}건)가 기한 초과 상태로, 업무량 또는 블로커 점검이 필요합니다.")
            results.append(Insight(
                rule="ASSIGNEE_DELAY_PATTERN", level=level,
                title=title, body=body,
                target=f"{name} · {dept}" if dept else name,
            ))
    return results


# ── Rule 4: DEPT_BOTTLENECK ──────────────────────────────────────
def rule_dept_bottleneck(dashboard: dict) -> list[Insight]:
    """특정 부서 오픈 이슈가 전체 평균의 2.5배 초과"""
    results = []
    dept_open: dict[str, int] = {}
    for uname, ud in dashboard.get("users_data", {}).items():
        d = dept_name(uname) or "기타"
        cnt = sum(1 for i in ud.get("issues", []) if _is_open(i))
        dept_open[d] = dept_open.get(d, 0) + cnt

    if len(dept_open) < 2:
        return results

    avg = sum(dept_open.values()) / len(dept_open)
    for dept, cnt in sorted(dept_open.items(), key=lambda x: -x[1]):
        if cnt >= avg * 2.5 and cnt >= 5:
            others = {d: c for d, c in dept_open.items() if d != dept}
            compare = min(others, key=others.get) if others else ""
            compare_cnt = others.get(compare, 0) if compare else 0
            results.append(Insight(
                rule="DEPT_BOTTLENECK", level="warning",
                title="부서 병목",
                body=f"<strong>{dept}팀</strong>에 오픈 이슈 <strong>{cnt}건</strong>이 집중되어 있습니다" +
                     (f" (비교: {compare}팀 {compare_cnt}건)" if compare else "") +
                     ". 팀 간 업무 불균형이 지속될 경우 해당 팀의 처리 지연이 전체 납기에 영향을 줄 수 있습니다.",
                target=f"{dept}팀",
            ))
    return results


# ── Rule 5: HIGH_RISK_VERSION ────────────────────────────────────
def rule_high_risk_version(dashboard: dict) -> list[Insight]:
    """마감 30일 이내, 완료율 < 40%"""
    results = []
    for v in dashboard.get("versions", []):
        due = v.get("due_date", "")
        if not due or v.get("status") == "closed":
            continue
        days_left = _days_diff(due)
        if not (0 < days_left <= 30):
            continue
        total = v.get("total", 0)
        closed = v.get("closed", 0)
        if total < 3:
            continue
        pct = round(closed / total * 100)
        if pct < 40:
            results.append(Insight(
                rule="HIGH_RISK_VERSION", level="info",
                title="고위험 버전",
                body=f"마감 <strong>{days_left}일</strong> 전, <strong>{v.get('name','?')}</strong> 완료율이 <strong>{pct}%</strong>에 불과합니다. 잔여 이슈 {total - closed}건을 기간 내 처리하려면 현재보다 빠른 처리 속도가 필요하며, 지금 개입하지 않으면 지연이 확정될 수 있습니다.",
                target=f"{v.get('name','?')}",
            ))
    return results


# ── Rule 6: STALE_ISSUE ──────────────────────────────────────────
def rule_stale_issue(dashboard: dict) -> list[Insight]:
    """14일 이상 상태 변경 없는 오픈 이슈"""
    results = []
    for i in _all_issues(dashboard):
        if not _is_open(i) or _is_hold(i):
            continue
        updated = i.get("updated_on", "")[:10]
        if not updated:
            continue
        stale_days = -_days_diff(updated)
        if stale_days >= 14:
            name = short_name(i.get("assignee", ""))
            results.append(Insight(
                rule="STALE_ISSUE", level="warning",
                title="이슈 방치",
                body=f"<strong>#{i['id']} {i.get('subject','')[:30]}</strong>이(가) <strong>{stale_days}일</strong>째 상태 변화 없이 정체되어 있습니다. 블로커 존재 또는 담당자 인지 부재 가능성이 있으며, 즉각 확인이 필요합니다.",
                target=f"#{i['id']} · {name}" if name else f"#{i['id']}",
            ))
    # 최대 3개만
    results.sort(key=lambda x: int(x.target.split('·')[0].strip().lstrip('#') or 0))
    return results[:3]


# ── Rule 7: UNASSIGNED_URGENT ────────────────────────────────────
def rule_unassigned_urgent(dashboard: dict) -> list[Insight]:
    """담당자 없는 이슈 중 마감 7일 이내"""
    urgent = []
    for uname, ud in dashboard.get("users_data", {}).items():
        if uname.strip():
            continue  # 담당자 있음
        for i in ud.get("issues", []):
            dd = i.get("due_date", "")
            if dd and 0 <= _days_diff(dd) <= 7 and _is_open(i):
                urgent.append(i)

    # 미배정 이슈는 별도로 전체 이슈에서도 체크
    for i in _all_issues(dashboard):
        assignee = i.get("assignee", "").strip()
        if assignee:
            continue
        dd = i.get("due_date", "")
        if dd and 0 <= _days_diff(dd) <= 7 and _is_open(i):
            if i not in urgent:
                urgent.append(i)

    if not urgent:
        return []
    min_due = min(i["due_date"] for i in urgent)
    days_left = _days_diff(min_due)
    return [Insight(
        rule="UNASSIGNED_URGENT", level="critical",
        title="미배정 긴급 이슈",
        body=f"마감 <strong>{days_left}일</strong> 이내인 이슈 <strong>{len(urgent)}건</strong>에 담당자가 없습니다. 배정 없이는 아무도 책임지지 않는 구간이 생기며, 마감 누락 리스크가 매우 높은 상태입니다.",
        target=f"{len(urgent)}건",
    )]


# ── Rule 8: MASS_OVERDUE ────────────────────────────────────────
def rule_mass_overdue(dashboard: dict) -> list[Insight]:
    """전체 오픈 이슈 중 초과 비율 70% 이상 (최소 5건)"""
    all_issues = _all_issues(dashboard)
    open_issues = [i for i in all_issues if _is_open(i) and not _is_hold(i)]
    overdue     = [i for i in open_issues if _is_overdue(i)]
    total = len(open_issues)
    if total < 5:
        return []
    ratio = len(overdue) / total
    if ratio < 0.7:
        return []
    return [Insight(
        rule="MASS_OVERDUE", level="critical",
        title="프로젝트 전반 일정 초과",
        body=f"오픈 이슈의 <strong>{round(ratio*100)}%({len(overdue)}/{total}건)</strong>가 기한을 초과했습니다. 단순 지연이 아닌 프로젝트 일정 체계 자체의 문제가 감지되며, 전면적인 재조정이 필요합니다.",
        target=f"초과 {len(overdue)}/{total}건",
    )]


# ── Rule 9: DEADLINE_CLUSTER ─────────────────────────────────────
def rule_deadline_cluster(dashboard: dict) -> list[Insight]:
    """특정 날짜에 마감 이슈 5건 이상 집중"""
    from collections import Counter
    results = []
    today = _today()
    due_dates = [i["due_date"] for i in _all_issues(dashboard)
                 if i.get("due_date") and i["due_date"] >= today and _is_open(i)]
    counts = Counter(due_dates)
    for due_date, cnt in sorted(counts.items()):
        if cnt >= 5:
            days_left = _days_diff(due_date)
            results.append(Insight(
                rule="DEADLINE_CLUSTER", level="critical",
                title="마감일 집중",
                body=f"<strong>{due_date}(D-{days_left})</strong>에 마감 이슈 <strong>{cnt}건</strong>이 집중되어 있습니다. 당일 리뷰·QA 처리 용량을 초과할 가능성이 높으며, 사전 분산 처리 또는 마감일 조정을 권고합니다.",
                target=f"{due_date}",
            ))
    return results[:2]


# ── Rule 9: LONG_PENDING ─────────────────────────────────────────
def rule_long_pending(dashboard: dict) -> list[Insight]:
    """보류 상태 14일 이상 미해결"""
    pending = []
    for i in _all_issues(dashboard):
        if not _is_hold(i):
            continue
        updated = i.get("updated_on", "")[:10]
        if not updated:
            continue
        days = -_days_diff(updated)
        if days >= 14:
            pending.append((days, i))

    if not pending:
        return []
    pending.sort(reverse=True)
    avg_days = round(sum(d for d, _ in pending) / len(pending))
    return [Insight(
        rule="LONG_PENDING", level="info",
        title="장기 보류",
        body=f"보류 이슈 <strong>{len(pending)}건</strong>이 평균 <strong>{avg_days}일</strong>째 방치되어 있습니다. 장기 보류는 백로그를 왜곡하고 실제 진행률 파악을 어렵게 만듭니다. 재검토 후 진행·취소 결정이 필요합니다.",
        target=f"보류 {len(pending)}건",
    )]


# ── Rule: RESOLUTION_VELOCITY ───────────────────────────────────
def rule_resolution_velocity(dashboard: dict) -> list[Insight]:
    """완료 이슈 평균 처리일 vs 오픈 이슈 평균 나이 비교"""
    all_iss = _all_issues(dashboard)
    closed  = [i for i in all_iss if i.get("status", "") in CLOSED_SET]
    open_iss = [i for i in all_iss if _is_open(i) and not _is_hold(i)]
    today = date.today()

    resolve_days = []
    for i in closed:
        c = i.get("created_on", "")[:10]
        u = i.get("updated_on", "")[:10]
        if c and u:
            try:
                d = (date.fromisoformat(u) - date.fromisoformat(c)).days
                if 0 < d < 365:
                    resolve_days.append(d)
            except ValueError:
                pass

    if len(resolve_days) < 3:
        return []
    avg_resolve = round(sum(resolve_days) / len(resolve_days))

    open_ages = []
    for i in open_iss:
        c = i.get("created_on", "")[:10]
        if c:
            try:
                open_ages.append((today - date.fromisoformat(c)).days)
            except ValueError:
                pass
    if not open_ages:
        return []
    avg_age = round(sum(open_ages) / len(open_ages))

    if avg_age <= avg_resolve * 1.5 or avg_age < 14:
        return []
    ratio = round(avg_age / max(avg_resolve, 1), 1)
    level = "critical" if ratio >= 3 else "warning"
    return [Insight(
        rule="RESOLUTION_VELOCITY", level=level,
        title="처리 속도 저하",
        body=f"이슈 해결 속도(평균 <strong>{avg_resolve}일</strong>)보다 오픈 이슈 체류 기간(평균 <strong>{avg_age}일</strong>, {ratio}배)이 현저히 깁니다. 처리 용량 부족 또는 블로커 증가로 인해 백로그가 지속적으로 누적되고 있음을 나타냅니다.",
        target=f"평균 {avg_age}일",
    )]


# ── Rule: NO_DUE_DATE ────────────────────────────────────────────
def rule_no_due_date(dashboard: dict) -> list[Insight]:
    """마감일 없는 오픈 이슈 50% 이상"""
    open_iss = [i for i in _all_issues(dashboard) if _is_open(i) and not _is_hold(i)]
    if len(open_iss) < 5:
        return []
    no_due = [i for i in open_iss if not i.get("due_date", "")]
    ratio = len(no_due) / len(open_iss)
    if ratio < 0.5:
        return []
    return [Insight(
        rule="NO_DUE_DATE", level="info",
        title="마감일 미설정 과다",
        body=f"오픈 이슈 <strong>{round(ratio*100)}%({len(no_due)}건)</strong>에 마감일이 설정되어 있지 않습니다. "
             f"기한 없는 이슈는 우선순위 판단을 어렵게 하고, 리스크 탐지 정확도를 낮춥니다. 일괄 설정을 권고합니다.",
        target=f"{len(no_due)}건 미설정",
    )]


# ── Rule: AGED_ISSUES ────────────────────────────────────────────
def rule_aged_issues(dashboard: dict) -> list[Insight]:
    """30일 이상 된 오픈 이슈가 전체의 40% 이상"""
    open_iss = [i for i in _all_issues(dashboard) if _is_open(i) and not _is_hold(i)]
    if len(open_iss) < 5:
        return []
    today = date.today()
    aged = []
    for i in open_iss:
        c = i.get("created_on", "")[:10]
        if not c:
            continue
        try:
            if (today - date.fromisoformat(c)).days >= 30:
                aged.append((today - date.fromisoformat(c)).days)
        except ValueError:
            pass
    if not aged or len(aged) / len(open_iss) < 0.4:
        return []
    ratio = len(aged) / len(open_iss)
    avg_age = round(sum(aged) / len(aged))
    level = "critical" if ratio >= 0.6 else "warning"
    return [Insight(
        rule="AGED_ISSUES", level=level,
        title="고령 이슈 누적",
        body=f"오픈 이슈 중 <strong>{round(ratio*100)}%({len(aged)}건)</strong>이 30일 이상 체류 중이며, 평균 체류 기간은 <strong>{avg_age}일</strong>입니다. "
             f"장기 이슈 비중이 높을수록 실질적인 프로젝트 진행률이 과소평가되는 경향이 있습니다.",
        target=f"{len(aged)}건 · 평균 {avg_age}일",
    )]


# ── Rule: BUG_CONCENTRATION ──────────────────────────────────────
def rule_bug_concentration(dashboard: dict) -> list[Insight]:
    """버그 tracker 이슈가 전체 오픈의 50% 이상"""
    open_iss = [i for i in _all_issues(dashboard) if _is_open(i)]
    if len(open_iss) < 5:
        return []
    bug_kw = {"버그", "bug", "결함", "오류", "error"}
    bugs = [i for i in open_iss
            if any(kw in (i.get("tracker", "") or "").lower() for kw in bug_kw)]
    if not bugs or len(bugs) / len(open_iss) < 0.5:
        return []
    ratio = round(len(bugs) / len(open_iss) * 100)
    return [Insight(
        rule="BUG_CONCENTRATION", level="warning",
        title="버그 이슈 집중",
        body=f"전체 오픈 이슈의 <strong>{ratio}%({len(bugs)}건)</strong>가 버그 유형입니다. "
             f"이 비율은 기능 개발보다 품질 부채 해소에 더 많은 리소스가 요구되는 상태임을 나타내며, 릴리즈 품질에 직접 영향을 줄 수 있습니다.",
        target=f"버그 {len(bugs)}건",
    )]


# ── Rule: DEADLINE_ASSIGNEE_SKEW ─────────────────────────────────
def rule_deadline_assignee_skew(dashboard: dict) -> list[Insight]:
    """마감 7일 이내 이슈가 특정 담당자에 3건 이상 집중"""
    skew: dict[str, list] = {}
    for uname, ud in dashboard.get("users_data", {}).items():
        for i in ud.get("issues", []):
            dd = i.get("due_date", "")
            if dd and 0 <= _days_diff(dd) <= 7 and _is_open(i):
                skew.setdefault(uname, []).append(i)

    results = []
    for uname, issues in skew.items():
        if len(issues) < 3:
            continue
        name = short_name(uname)
        dept = dept_name(uname)
        min_days = min(_days_diff(i["due_date"]) for i in issues)
        results.append(Insight(
            rule="DEADLINE_ASSIGNEE_SKEW", level="warning",
            title="마감 임박 이슈 편중",
            body=f"<strong>{name}</strong>에게 7일 이내 마감 이슈 <strong>{len(issues)}건</strong>이 집중되어 있습니다 (최단 D-{min_days}). "
                 f"단기간 과부하가 집중되면 품질 저하 또는 마감 누락 리스크가 높아집니다.",
            target=name + (f" · {dept}" if dept else ""),
        ))
    results.sort(key=lambda x: -len(skew.get(
        next((k for k in skew if short_name(k) == x.target.split(' ·')[0].strip()), ''), []
    )))
    return results[:2]


# ── Rule A1: RISK_TREND_UP ───────────────────────────────────────
def rule_risk_trend(dashboard: dict) -> list[Insight]:
    """3주 이상 연속 리스크 점수 상승"""
    history = sorted(dashboard.get("history", []), key=lambda x: x.get("date", ""))
    if len(history) < 3:
        return []
    scores = [h["score"] for h in history[-4:]]
    # 연속 상승 구간 길이 체크
    streak = 1
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] > scores[i - 1]:
            streak += 1
        else:
            break
    if streak < 3:
        return []
    delta = round(scores[-1] - scores[-streak], 1)
    return [Insight(
        rule="RISK_TREND_UP", level="warning",
        title="리스크 연속 상승",
        body=f"리스크 점수가 <strong>{streak}주 연속</strong> 상승하며 <strong>{scores[-streak]} → {scores[-1]}</strong>(+{delta}점)을 기록했습니다. "
             f"연속 상승 추세는 구조적 문제가 해결되지 않고 있음을 시사하며, 조기 개입이 효과적입니다.",
        target=f"{streak}주 연속 상승",
    )]


# ── Rule A2: OVERDUE_TREND ───────────────────────────────────────
def rule_overdue_trend(dashboard: dict) -> list[Insight]:
    """최근 4주 평균 대비 현재 초과 이슈 30% 이상 증가"""
    history = sorted(dashboard.get("history", []), key=lambda x: x.get("date", ""))
    if len(history) < 3:
        return []
    past = history[max(0, len(history) - 5):-1]
    if not past:
        return []
    avg_overdue = sum(h.get("overdue", 0) for h in past) / len(past)
    current = history[-1].get("overdue", 0)
    delta = current - avg_overdue
    if avg_overdue <= 0 or delta < 2 or delta / avg_overdue < 0.3:
        return []
    pct = round(delta / avg_overdue * 100)
    return [Insight(
        rule="OVERDUE_TREND", level="warning",
        title="초과 이슈 증가 추세",
        body=f"초과 이슈가 최근 평균({round(avg_overdue, 1)}건) 대비 <strong>+{round(delta)}건(+{pct}%)</strong> 증가했습니다. "
             f"단발성 급증이 아닌 구조적 지연 신호일 수 있으며, 원인 분석이 필요합니다.",
        target=f"초과 {int(current)}건",
    )]


# ── Rule B: VERSION_DELAY_FORECAST ──────────────────────────────
def rule_version_delay_forecast(dashboard: dict) -> list[Insight]:
    """현재 처리 속도 기반 버전 완료 예상일 계산 → 마감 초과 예측"""
    results = []
    today = date.today()
    for v in dashboard.get("versions", []):
        due = v.get("due_date", "")
        if not due or v.get("status") == "closed":
            continue
        days_left = _days_diff(due)
        if not (1 <= days_left <= 90):
            continue
        total = v.get("total", 0)
        closed = v.get("closed", 0)
        if total < 4 or closed == 0:
            continue
        remaining = total - closed
        # 이슈 created_on 중 가장 오래된 날짜를 시작점으로 추정
        v_issues = v.get("issues", [])
        created_dates = [i.get("created_on", "")[:10] for i in v_issues if i.get("created_on", "")[:10]]
        if not created_dates:
            continue
        try:
            start = date.fromisoformat(min(created_dates))
        except ValueError:
            continue
        elapsed = max((today - start).days, 1)
        daily_rate = closed / elapsed
        if daily_rate <= 0:
            continue
        days_needed = remaining / daily_rate
        predicted = today + timedelta(days=int(days_needed))
        delay = (predicted - date.fromisoformat(due)).days
        if delay >= 3:
            results.append(Insight(
                rule="VERSION_DELAY_FORECAST", level="warning" if delay < 14 else "critical",
                title="버전 지연 예측",
                body=f"현재 이슈 처리 속도가 유지될 경우 <strong>{v.get('name', '?')}</strong>의 완료 예상일은 <strong>{predicted}</strong>입니다. "
                     f"마감 대비 <strong>D+{delay}</strong> 지연이 예측되며, 지금 개입하지 않으면 지연이 확정됩니다.",
                target=v.get("name", "?"),
            ))
    return results


# ── Rule C: OVERDUE_SPIKE ────────────────────────────────────────
def rule_overdue_spike(dashboard: dict) -> list[Insight]:
    """전주 대비 초과 이슈 3건 이상 급증"""
    history = sorted(dashboard.get("history", []), key=lambda x: x.get("date", ""))
    if len(history) < 2:
        return []
    now_overdue  = history[-1].get("overdue", 0)
    prev_overdue = history[-2].get("overdue", 0)
    delta = now_overdue - prev_overdue
    if delta < 3:
        return []
    pct_str = f" (+{round(delta / prev_overdue * 100)}%)" if prev_overdue > 0 else ""
    return [Insight(
        rule="OVERDUE_SPIKE", level="critical",
        title="초과 이슈 주간 급증",
        body=f"초과 이슈가 전주({prev_overdue}건) 대비 <strong>+{delta}건{pct_str}</strong>으로 급증했습니다({now_overdue}건). "
             f"단기간의 급격한 증가는 외부 블로커 또는 팀 가용 인력 변화의 신호일 수 있습니다.",
        target=f"+{delta}건",
    )]


# ── Rule D: BURNOUT_RISK ─────────────────────────────────────────
def rule_burnout_risk(dashboard: dict) -> list[Insight]:
    """이슈 수 평균 1.5배 이상 + 초과율 30% 이상인 담당자"""
    results = []
    counts: dict[str, dict] = {}
    for uname, ud in dashboard.get("users_data", {}).items():
        issues = ud.get("issues", [])
        open_cnt    = sum(1 for i in issues if _is_open(i) and not _is_hold(i))
        overdue_cnt = sum(1 for i in issues if _is_overdue(i))
        counts[uname] = {"open": open_cnt, "overdue": overdue_cnt}

    if not counts:
        return []
    avg_open = sum(v["open"] for v in counts.values()) / len(counts)

    for uname, c in counts.items():
        open_cnt, overdue_cnt = c["open"], c["overdue"]
        if open_cnt < 6:
            continue
        overdue_ratio = overdue_cnt / open_cnt
        load_ratio    = open_cnt / max(avg_open, 1)
        if load_ratio < 1.5 or overdue_ratio < 0.3:
            continue
        name = short_name(uname)
        dept = dept_name(uname)
        level = "critical" if load_ratio >= 2.0 and overdue_ratio >= 0.5 else "warning"
        results.append(Insight(
            rule="BURNOUT_RISK", level=level,
            title="담당자 번아웃 위험",
            body=f"<strong>{name}</strong>의 업무 부하가 팀 평균의 <strong>{round(load_ratio, 1)}배</strong>이며, 초과율이 <strong>{round(overdue_ratio * 100)}%</strong>에 달합니다. "
                 f"과부하와 높은 지연율이 동시에 나타날 경우 처리 속도 저하와 품질 문제로 이어질 수 있습니다.",
            target=f"{name}" + (f" · {dept}" if dept else ""),
        ))

    results.sort(key=lambda x: 0 if x.level == "critical" else 1)
    return results[:2]


# ── 전체 실행 ────────────────────────────────────────────────────
RULES = [
    # 즉각 위험
    rule_mass_overdue,
    rule_unassigned_urgent,
    rule_overdue_spike,
    rule_deadline_cluster,
    rule_version_overrun,
    # 예측·추세
    rule_version_delay_forecast,
    rule_risk_trend,
    rule_overdue_trend,
    # 담당자
    rule_burnout_risk,
    rule_assignee_delay_pattern,
    rule_deadline_assignee_skew,
    rule_dept_bottleneck,
    # 이슈 상태·패턴
    rule_resolution_velocity,
    rule_aged_issues,
    rule_bug_concentration,
    rule_no_due_date,
    rule_stale_issue,
    # 버전
    rule_test_buffer,
    rule_high_risk_version,
    rule_long_pending,
]

LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}


def run_all_insights(dashboard: dict) -> list[Insight]:
    results = []
    for rule_fn in RULES:
        try:
            results.extend(rule_fn(dashboard))
        except Exception:
            pass
    results.sort(key=lambda x: LEVEL_ORDER.get(x.level, 9))

    # ③ fallback: 리스크 Critical인데 CRITICAL insight 없을 때 보완
    project_risks = dashboard.get("project_risk", [])
    if isinstance(project_risks, dict):
        project_risks = list(project_risks.values())
    has_critical_risk = any(
        p.get("risk_level") in ("Critical",) for p in project_risks
    )
    has_critical_insight = any(r.level == "critical" for r in results)
    if has_critical_risk and not has_critical_insight:
        top = next((p for p in project_risks if p.get("risk_level") == "Critical"), None)
        if top:
            results.insert(0, Insight(
                rule="RISK_CRITICAL_FALLBACK", level="critical",
                title="리스크 Critical 감지",
                body=f"<strong>{top.get('name','?')}</strong>의 리스크 점수가 <strong>{round(top.get('risk_score', 0), 1)}</strong>으로 Critical 수준에 도달했습니다. 초과·임박 이슈의 복합적 누적이 감지되며, 즉각적인 개입이 필요합니다.",
                target=top.get("name", "?"),
            ))

    return results
