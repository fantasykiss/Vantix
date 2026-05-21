"""
insights.py — Rule-based insight engine for Vantix
9가지 룰을 실행해서 Insight 리스트를 반환합니다. AI API 불필요.
"""

from dataclasses import dataclass
from datetime import date, datetime
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
                body=f"<strong>{v.get('name','?')} 마감 {due}</strong>인데 종속 이슈 완료일이 <strong>{max_due}</strong> — 버전 일정 초과 확실.",
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
                title="테스트 여유 부족",
                body=f"<strong>{v.get('name','?')} 마감 {due}</strong>, 마지막 이슈 완료 <strong>{last_dev}</strong> — 테스트 여유 <strong>{buffer}일</strong>. 일정 조정 권고.",
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
            results.append(Insight(
                rule="ASSIGNEE_DELAY_PATTERN", level="warning",
                title="담당자 지연 패턴",
                body=f"<strong>{name}</strong> 오픈 이슈 {total}건 중 <strong>{len(overdue)}건 초과</strong> ({round(ratio*100)}%) — 지속적 지연 패턴. 업무량 점검 필요.",
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
                body=f"<strong>{dept}팀</strong> 오픈 이슈 <strong>{cnt}건</strong>" +
                     (f" vs {compare}팀 <strong>{compare_cnt}건</strong>" if compare else "") +
                     " — 업무 쏠림 감지. 분산 검토 필요.",
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
                body=f"<strong>{v.get('name','?')}</strong> 오픈 이슈 <strong>{total - closed}건</strong>, 마감까지 <strong>{days_left}일</strong> — 현재 완료율 <strong>{pct}%</strong>.",
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
                body=f"<strong>#{i['id']} {i.get('subject','')[:30]}</strong> — <strong>{stale_days}일째</strong> 상태 변경 없음. 진행 여부 확인 필요.",
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
        body=f"담당자 미배정 이슈 <strong>{len(urgent)}건</strong>, 최단 마감까지 <strong>{days_left}일</strong> — 즉시 담당자 배정 필요.",
        target=f"{len(urgent)}건",
    )]


# ── Rule 8: DEADLINE_CLUSTER ─────────────────────────────────────
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
                body=f"<strong>{due_date}</strong>에 마감 이슈 <strong>{cnt}건</strong> 집중 (D-{days_left}) — 당일 리뷰 병목 및 QA 불가 가능성.",
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
        body=f"보류 상태 이슈 <strong>{len(pending)}건</strong> 평균 <strong>{avg_days}일째</strong> 미해결 — 의사결정 또는 취소 처리 필요.",
        target=f"보류 {len(pending)}건",
    )]


# ── 전체 실행 ────────────────────────────────────────────────────
RULES = [
    rule_version_overrun,
    rule_unassigned_urgent,
    rule_deadline_cluster,
    rule_test_buffer,
    rule_assignee_delay_pattern,
    rule_dept_bottleneck,
    rule_stale_issue,
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
    return results
