"""
Redmine REST API 클라이언트.

REFACTOR_PLAN.md Phase B — main.py의 fetch()/fetch_all()/get_projects()/get_issues()가
매 호출마다 반복 전파하던 redmine_url/api_key 파라미터 스레딩을 없애기 위한 캡슐화.

RedmineClient는 접속 정보(url, key)를 인스턴스 상태로 들고 있고, main.py는 이 클래스를
감싸는 얇은 호환 래퍼 함수(fetch/fetch_all/get_projects/get_issues)를 유지한다.
main.py의 78개 기존 호출부(redmine_url=..., api_key=... 키워드 인자를 넘기는 곳들)를
한 번에 다 바꾸는 대신, 우선 로직의 단일 소스를 이 클래스로 옮기고 래퍼는 그대로 둬서
호출부를 건드리지 않고도 회귀 위험 없이 캡슐화 이득을 얻는다.
call-site 전체 이관(래퍼 제거, client.method() 직접 호출로 교체)은 REFACTOR_PLAN.md의
Phase B-2로 별도 세션에서 진행 권장 — 실제 Redmine 서버에 붙여서 검증 가능한 환경에서
해야 안전하다 (지금 이 세션은 Redmine에 네트워크로 붙을 수 없어 라이브 검증 불가).
"""
import json
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def _default_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class RedmineClient:
    """하나의 Redmine 서버 접속(url + api key)을 캡슐화한 클라이언트.

    사용 예:
        client = RedmineClient(base_url, api_key)
        projects = client.get_projects()
        issues = client.get_issues(project_id="1", updated_after="2026-03-01")
    """

    def __init__(self, base_url: str, api_key: str, ssl_context: ssl.SSLContext | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.ssl_context = ssl_context or _default_ssl_context()

    def fetch(self, path: str, params: dict | None = None, retries: int = 2) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Redmine-API-Key": self.api_key})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=30, context=self.ssl_context) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                if attempt < retries - 1:
                    print(f"  재시도 ({attempt+1}/{retries-1}): offset {params.get('offset','?') if params else '?'}")
                    time.sleep(1)
                else:
                    print(f"  요청 실패: {url}\n     {e}")
        return {}

    def fetch_all(self, path: str, key: str, base_params: dict | None = None) -> list:
        """첫 페이지로 total_count 파악 후 나머지 페이지를 병렬로 fetch"""
        limit = 100
        first_params = {**(base_params or {}), "limit": limit, "offset": 0}
        first_data = self.fetch(path, first_params)
        items = list(first_data.get(key, []))
        total = first_data.get("total_count", len(items))

        if total <= limit:
            return items

        offsets = list(range(limit, total, limit))
        print(f"  병렬 fetch: {len(offsets)+1}페이지 / 총 {total}건")

        def fetch_page(offset):
            params = {**(base_params or {}), "limit": limit, "offset": offset}
            data = self.fetch(path, params)
            return data.get(key, [])

        pages = [None] * len(offsets)
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {executor.submit(fetch_page, off): i for i, off in enumerate(offsets)}
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    pages[idx] = future.result()
                except Exception as e:
                    print(f"  페이지 fetch 실패 (offset={offsets[idx]}): {e}")
                    pages[idx] = []

        for page in pages:
            if page:
                items += page
        return items

    def get_projects(self) -> list:
        data = self.fetch("/projects.json", {"limit": 100})
        return data.get("projects", [])

    def get_issues(self, project_id: str = "", updated_after: str = "2026-03-01") -> list:
        params = {"status_id": "*"}
        if project_id:
            params["project_id"] = project_id
        if updated_after:
            params["updated_on"] = f">={updated_after}T00:00:00Z"
        return self.fetch_all("/issues.json", "issues", params)
