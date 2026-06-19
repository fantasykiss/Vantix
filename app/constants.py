# ==================== 요금제 (Phase 4 SaaS) ====================
# value-metric = 모니터링 프로젝트 수. -1 = 무제한.
# AI 3종·이메일 리포트는 Pro 이상에서만 해금.
PLANS = {
    "free":     {"label": "Free",     "project_limit": 1,  "ai": True,  "report": True},
    "pro":      {"label": "Pro",      "project_limit": 5,  "ai": True,  "report": True},
    "business": {"label": "Business", "project_limit": -1, "ai": True,  "report": True},
}
DEFAULT_PLAN = "free"
PLAN_ORDER = ["free", "pro", "business"]  # 등급 비교용


def plan_info(plan: str) -> dict:
    """알 수 없는 플랜은 free로 폴백."""
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


def plan_allows(plan: str, feature: str) -> bool:
    """feature: 'ai' | 'report'. 해당 플랜이 그 기능을 쓸 수 있는지."""
    return bool(plan_info(plan).get(feature, False))


def plan_project_limit(plan: str) -> int:
    """프로젝트 개수 제한. -1 = 무제한."""
    return plan_info(plan).get("project_limit", 1)


PROGRESS_SET = {"진행", "진행대기", "In Progress"}
RESOLVED_SET = {"해결", "해결됨", "Resolved"}
CLOSED_SET   = {"완료", "완료(잔땡처리)", "Closed", "반려", "Rejected", "해결"}
HOLD_SET     = {"보류", "보류(스펙아웃)", "스펙아웃"}

DEPT_NORMALIZE = {
    "1기획": "기획",
    "1PM":   "PM",
    "1클라": "클라",
    "1서버": "서버",
    "1UI":   "UI",
}


def dept_name(name: str) -> str:
    """'기획_홍길동' → '기획', '홍길동 기획' → '기획', '1기획_ TEST1' → '기획'"""
    if "_" in name:
        raw = name.split("_")[0].strip()
    else:
        parts = name.strip().split()
        raw = parts[-1] if len(parts) > 1 else name.strip()
    return DEPT_NORMALIZE.get(raw, raw)


def short_name(name: str) -> str:
    """'기획_ TEST1' → 'TEST1'"""
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name.strip()
