"""
RedmineClient 생성 팩토리.

REFACTOR_PLAN.md Phase B. 커넥션 정보(전역 기본값 vs 세션별 override)를
어디서 가져올지 결정하는 책임을 한 곳에 모은다.
"""
from config import BASE_URL, API_KEY

from app.redmine.client import RedmineClient


def build_client(redmine_url: str | None = None, api_key: str | None = None) -> RedmineClient:
    """redmine_url/api_key가 없으면 config의 전역 기본값(.env의 BASE_URL/API_KEY)을 사용."""
    return RedmineClient(redmine_url or BASE_URL, api_key or API_KEY)


def client_from_session(session: dict) -> RedmineClient:
    """세션 dict({"url":..., "key":...})로부터 클라이언트 생성 — 멀티테넌트 요청 처리용."""
    return build_client(session.get("url"), session.get("key"))
