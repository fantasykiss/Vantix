"""대시보드 주요 기능 흐름 회귀 E2E.

사용자 시나리오: 로그인 → 대시보드 진입 → KPI/리스크 위젯 렌더 확인.
"""


def test_dashboard_renders_kpi_widgets(logged_in_page, base_url):
    page = logged_in_page
    page.goto(f"{base_url}/")
    page.wait_for_selector("#kpi-overdue", state="visible", timeout=15000)

    for sel in ("#kpi-open", "#kpi-users", "#kpi-imminent", "#risk-num"):
        assert page.locator(sel).count() == 1, f"위젯 누락: {sel}"


def test_dashboard_risk_score_is_numeric(logged_in_page, base_url):
    page = logged_in_page
    page.goto(f"{base_url}/")
    page.wait_for_selector("#risk-num", state="visible", timeout=15000)

    text = page.inner_text("#risk-num").strip()
    assert text.replace(".", "", 1).isdigit(), f"리스크 점수가 숫자가 아님: {text!r}"


def test_dashboard_api_data_call_succeeds(logged_in_page, base_url):
    page = logged_in_page
    resp = page.request.get(f"{base_url}/api/data")
    assert resp.ok
    body = resp.json()
    assert "project_risk" in body
