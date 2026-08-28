#!/usr/bin/env python3
"""README·문서용 스크린샷 자동 생성.

데모 세션(`POST /api/connect/demo`)으로 로그인해 주요 화면을 캡처한다.
결과물은 docs/screenshots/*.png.

실행:
    source venv/bin/activate
    python scripts/capture_screenshots.py [BASE_URL]

BASE_URL 기본값은 배포된 데모. 로컬 확인 시 http://localhost:8000 전달.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://web-production-cdd14.up.railway.app"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORT = {"width": 1440, "height": 900}


def _settle(page, ms: int = 3000):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()

        # 1. 랜딩 페이지 (라이트)
        page.goto(f"{BASE}/connect", wait_until="networkidle")
        _settle(page)
        page.screenshot(path=OUT / "01-landing.png")
        print("saved 01-landing.png")

        # 데모 세션 시작
        resp = page.request.post(f"{BASE}/api/connect/demo")
        assert resp.ok, f"demo 연결 실패: {resp.status}"

        # 2. 대시보드 — 리스크 브리핑 (Critical 프로젝트, 다크)
        page.goto(f"{BASE}/?p=vantix_ch&v=overview", wait_until="networkidle")
        _settle(page, 4000)
        page.screenshot(path=OUT / "02-dashboard.png")
        print("saved 02-dashboard.png")

        # 3. 리포트 내보내기 모달 (섹션 편집기)
        clicked = page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('a,button,div')]
                    .find(e => e.textContent.trim() === '리포트' && e.children.length < 3);
                if (el) { el.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            page.wait_for_timeout(1500)
            page.screenshot(path=OUT / "03-report.png")
            print("saved 03-report.png")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
