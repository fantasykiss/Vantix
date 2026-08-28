"""대시보드 주요 위젯 렌더 회귀 E2E (데모 세션).

시나리오: 데모 진입 → Critical 프로젝트(vantix_ch) 리스크 브리핑 → KPI·리스크 위젯 확인.
"""

RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
KPI_SELECTORS = ("#kpi-overdue", "#kpi-open", "#kpi-users", "#kpi-imminent", "#kpi-milestone")


def _open_overview(demo_page, base_url):
    demo_page.goto(f"{base_url}/?p=vantix_ch&v=overview")
    demo_page.wait_for_selector("#risk-num", state="visible", timeout=20000)
    # #risk-num 은 placeholder("—")로 먼저 그려지고 /api/data 응답 후 값이 채워진다.
    demo_page.wait_for_function(
        "() => { const t = document.querySelector('#risk-num')?.textContent.trim();"
        " return t && t !== '—'; }",
        timeout=20000,
    )
    return demo_page


def test_kpi_widgets_render(demo_page, base_url):
    page = _open_overview(demo_page, base_url)
    for sel in KPI_SELECTORS:
        assert page.locator(sel).count() == 1, f"위젯 누락: {sel}"


def test_risk_score_is_numeric(demo_page, base_url):
    page = _open_overview(demo_page, base_url)
    text = page.inner_text("#risk-num").strip()
    assert text.replace(".", "", 1).isdigit(), f"리스크 점수가 숫자가 아님: {text!r}"


def test_risk_level_is_valid(demo_page, base_url):
    page = _open_overview(demo_page, base_url)
    level = page.inner_text("#risk-level-text").strip().upper()
    assert level in RISK_LEVELS, f"알 수 없는 리스크 등급: {level!r}"


def test_api_data_contract(demo_page, base_url):
    resp = demo_page.request.get(f"{base_url}/api/data?project_id=vantix_ch")
    assert resp.ok
    body = resp.json()
    assert "project_risk" in body
    assert isinstance(body["project_risk"], list)
