"""인증 API — 입력 검증 · rate limit · 로그인 실패 경로.

채용공고: "시스템이 갖추어야 할 기본 품질의 검증".
QA에서 발견된 결함(DEF-001/002 이메일 입력값 검증·길이제한)의 회귀 테스트를 포함한다.
"""
import uuid

import pytest

pytestmark = pytest.mark.api


# --- 입력 검증 (DB 불필요: 검증이 DB 조회보다 먼저 실행됨) --------------------

@pytest.mark.parametrize(
    "payload",
    [
        {"email": "", "password": "longenough1"},
        {"email": "not-an-email", "password": "longenough1"},
        {"email": "a@b", "password": "longenough1"},           # TLD 없음
        {"email": "a@" + "x" * 260 + ".com", "password": "longenough1"},  # 254자 초과
    ],
)
def test_signup_rejects_invalid_email(anon_client, payload):
    r = anon_client.post("/api/auth/signup", json=payload)
    assert r.status_code == 400
    assert "이메일" in r.json()["detail"]


def test_signup_rejects_short_password(anon_client):
    r = anon_client.post(
        "/api/auth/signup",
        json={"email": "valid@example.com", "password": "short"},
    )
    assert r.status_code == 400
    assert "8자" in r.json()["detail"]


# --- Rate limit 순수 로직 ----------------------------------------------------

def test_check_rate_limit_blocks_after_max(app_module):
    main = app_module
    ip = f"203.0.113.{uuid.uuid4().int % 250}"
    main._connect_attempts.pop(ip, None)

    for _ in range(main.RATE_LIMIT_MAX):
        main._check_rate_limit(ip)  # 통과해야 함

    with pytest.raises(main.HTTPException) as exc:
        main._check_rate_limit(ip)
    assert exc.value.status_code == 429
    main._connect_attempts.pop(ip, None)


# --- 로그인 실패 경로 (Postgres 필요) --------------------------------------

@pytest.fixture
def temp_user(app_module, require_db):
    """검증 완료 상태의 임시 유저를 만들고 테스트 후 삭제."""
    main = app_module
    email = f"qa+{uuid.uuid4().hex[:12]}@example.com"
    uid, _token = main._create_user(email, "correct-horse-battery")
    with main._users_db() as conn:
        conn.execute("UPDATE vantix_users SET email_verified=1 WHERE id=?", (uid,))
    yield {"id": uid, "email": email, "password": "correct-horse-battery"}
    with main._users_db() as conn:
        conn.execute("DELETE FROM vantix_users WHERE id=?", (uid,))


def test_login_wrong_password_401(anon_client, temp_user):
    r = anon_client.post(
        "/api/auth/login",
        json={"email": temp_user["email"], "password": "wrong-password"},
    )
    assert r.status_code == 401


def test_login_unknown_user_401(anon_client, require_db):
    r = anon_client.post(
        "/api/auth/login",
        json={"email": f"ghost+{uuid.uuid4().hex}@example.com", "password": "whatever12"},
    )
    assert r.status_code == 401


def test_signup_duplicate_verified_email_409(anon_client, temp_user):
    r = anon_client.post(
        "/api/auth/signup",
        json={"email": temp_user["email"], "password": "another-valid-1"},
    )
    assert r.status_code == 409


def test_login_unverified_email_403(anon_client, app_module, require_db):
    main = app_module
    email = f"qa+{uuid.uuid4().hex[:12]}@example.com"
    uid, _ = main._create_user(email, "correct-horse-battery")
    try:
        r = anon_client.post(
            "/api/auth/login",
            json={"email": email, "password": "correct-horse-battery"},
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "email_not_verified"
    finally:
        with main._users_db() as conn:
            conn.execute("DELETE FROM vantix_users WHERE id=?", (uid,))
