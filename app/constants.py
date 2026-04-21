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
    """'기획_홍길동' → '기획', '1기획_ TEST1' → '기획' (정규화 + strip 포함)"""
    raw = name.split("_")[0].strip() if "_" in name else name.strip()
    return DEPT_NORMALIZE.get(raw, raw)


def short_name(name: str) -> str:
    """'기획_ TEST1' → 'TEST1'"""
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name.strip()
