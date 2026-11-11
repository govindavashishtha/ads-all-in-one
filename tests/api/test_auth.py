"""Auth (§17, §20): login, logout, revocation, expiry, CSRF, bearer, roles, rate limit."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth.env_auth import CSRF_COOKIE, SESSION_COOKIE, hash_token
from api.auth.main_system_auth import map_role
from engine.config import Settings
from engine.db.models import AuditLog
from engine.db.models import Session as SessionRow
from engine.enums import Role
from tests.api.conftest import (
    BASE,
    METRICS_TOKEN,
    PASSWORD,
    Clock,
    bearer,
    browser_login,
    build_app,
)

pytestmark = pytest.mark.integration

V1 = "/api/v1"


def _set_cookie(client: TestClient, name: str, value: str) -> None:
    """Replace a cookie in the client jar (drop any existing cookie of that name first)."""
    for c in list(client.cookies.jar):
        if c.name == name:
            client.cookies.jar.clear(c.domain, c.path, c.name)
    client.cookies.set(name, value)


def test_health_needs_no_auth(client: TestClient) -> None:
    assert client.get(f"{V1}/health").json() == {"status": "ok"}


def test_unauthenticated_is_401(client: TestClient) -> None:
    r = client.get(f"{V1}/overview")
    assert r.status_code == 401
    assert "Bearer" in r.headers["WWW-Authenticate"]
    assert client.get(f"{V1}/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(f"{V1}/auth/me", headers={"Authorization": "Basic x"}).status_code == 401


def test_browser_login_sets_secure_cookies(client: TestClient, seeded: Session) -> None:
    r = client.post(f"{V1}/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    set_cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in set_cookies if c.startswith(f"{SESSION_COOKIE}="))
    csrf = next(c for c in set_cookies if c.startswith(f"{CSRF_COOKIE}="))
    for c in (sess, csrf):
        low = c.lower()
        assert "secure" in low and "samesite=lax" in low and "path=/" in low
    assert "httponly" in sess.lower()
    assert "httponly" not in csrf.lower()  # readable by the dashboard for double-submit
    assert client.cookies.get(CSRF_COOKIE) == body["csrf_token"]
    token = client.cookies.get(SESSION_COOKIE)
    assert token
    row = seeded.scalars(select(SessionRow).where(SessionRow.token_hash == hash_token(token))).one()
    assert (row.kind, row.user, row.role, row.revoked_at) == ("browser", "admin", "admin", None)
    assert token not in str(row.token_hash)  # only the hash is stored
    me = client.get(f"{V1}/auth/me").json()
    assert me == {
        "username": "admin",
        "role": "admin",
        "session_kind": "browser",
        "auth_mode": "env",
    }


def test_wrong_password_and_user(client: TestClient, seeded: Session) -> None:
    r = client.post(f"{V1}/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    r = client.post(f"{V1}/auth/token", json={"username": "root", "password": PASSWORD})
    assert r.status_code == 401
    n = seeded.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.login_failed")
    )
    assert n == 2


def test_csrf_required_for_cookie_writes(client: TestClient) -> None:
    csrf = browser_login(client)
    url = f"{V1}/control/kill-switch"
    assert client.post(url, json={"on": True}).status_code == 403
    assert (
        client.post(url, json={"on": True}, headers={"X-CSRF-Token": "x" * 64}).status_code == 403
    )
    r = client.post(url, json={"on": True}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert r.json()["effective"] is True
    # header must also match the cookie (double-submit)
    _set_cookie(client, CSRF_COOKIE, "tampered")
    assert client.post(url, json={"on": False}, headers={"X-CSRF-Token": csrf}).status_code == 403


def test_csrf_token_is_bound_to_its_session(app, seeded: Session) -> None:
    with TestClient(app, base_url=BASE) as a, TestClient(app, base_url=BASE) as b:
        csrf_a = browser_login(a)
        browser_login(b)
        _set_cookie(b, CSRF_COOKIE, csrf_a)
        r = b.post(f"{V1}/control/kill-switch", json={"on": True}, headers={"X-CSRF-Token": csrf_a})
        assert r.status_code == 403


def test_bearer_skips_csrf_and_kinds_cannot_be_swapped(client: TestClient) -> None:
    h = bearer(client)
    assert client.post(f"{V1}/control/kill-switch", json={"on": True}, headers=h).status_code == 200
    # a CLI token presented as a cookie is refused, and vice versa
    cli_token = h["Authorization"].split()[1]
    with TestClient(client.app, base_url=BASE) as other:
        _set_cookie(other, SESSION_COOKIE, cli_token)
        assert other.get(f"{V1}/auth/me").status_code == 401
    browser_login(client)
    browser_token = client.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    r = client.get(f"{V1}/auth/me", headers={"Authorization": f"Bearer {browser_token}"})
    assert r.status_code == 401


def test_logout_revokes_session(client: TestClient, seeded: Session) -> None:
    csrf = browser_login(client)
    token = client.cookies.get(SESSION_COOKIE)
    assert token
    assert client.post(f"{V1}/auth/logout").status_code == 403  # CSRF applies to logout too
    r = client.post(f"{V1}/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    row = seeded.scalars(select(SessionRow).where(SessionRow.token_hash == hash_token(token))).one()
    assert row.revoked_at is not None
    _set_cookie(client, SESSION_COOKIE, token)  # replay the old cookie
    assert client.get(f"{V1}/auth/me").status_code == 401


def test_cli_logout_and_admin_revocation(client: TestClient) -> None:
    h1, h2 = bearer(client), bearer(client)
    assert client.post(f"{V1}/auth/logout", headers=h1).status_code == 204
    assert client.get(f"{V1}/auth/me", headers=h1).status_code == 401
    sessions = client.get(f"{V1}/auth/sessions", headers=h2).json()
    assert [s["current"] for s in sessions].count(True) == 1
    victim = bearer(client, "viewer")
    vid = next(
        s["id"]
        for s in client.get(f"{V1}/auth/sessions", headers=h2).json()
        if s["role"] == "viewer"
    )
    assert client.post(f"{V1}/auth/sessions/{vid}/revoke", headers=h2).status_code == 204
    assert client.get(f"{V1}/auth/me", headers=victim).status_code == 401
    assert client.post(f"{V1}/auth/sessions/999999/revoke", headers=h2).status_code == 404
    assert client.get(f"{V1}/auth/sessions", headers=bearer(client, "approver")).status_code == 403


def test_session_expiry(client: TestClient, clock: Clock) -> None:
    h = bearer(client)
    assert client.get(f"{V1}/auth/me", headers=h).status_code == 200
    clock.advance(minutes=61)  # TTL is 60 in the fixture
    assert client.get(f"{V1}/auth/me", headers=h).status_code == 401


def test_session_invalid_if_admin_user_renamed(
    seeded: Session, api_settings_factory: Callable[..., Settings], clock: Clock
) -> None:
    app1 = build_app(seeded, api_settings_factory(), clock)
    with TestClient(app1, base_url=BASE) as c:
        h = bearer(c)
    app2 = build_app(seeded, api_settings_factory(admin_user="someone_else"), clock)
    with TestClient(app2, base_url=BASE) as c:
        assert c.get(f"{V1}/auth/me", headers=h).status_code == 401


def test_rate_limit_per_user(client: TestClient, clock: Clock) -> None:
    for _ in range(5):
        assert (
            client.post(f"{V1}/auth/token", json={"username": "admin", "password": "x"}).status_code
            == 401
        )
    r = client.post(f"{V1}/auth/token", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "900"
    clock.advance(minutes=16)
    assert (
        client.post(
            f"{V1}/auth/token", json={"username": "admin", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_rate_limit_per_ip(client: TestClient) -> None:
    for i in range(5):
        client.post(f"{V1}/auth/login", json={"username": f"u{i}", "password": "x"})
    r = client.post(f"{V1}/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429


@pytest.mark.parametrize(
    ("role", "get_ok", "approve_ok", "admin_ok"),
    [("viewer", True, False, False), ("approver", True, True, False), ("admin", True, True, True)],
)
def test_roles(
    client: TestClient, world, role: str, get_ok: bool, approve_ok: bool, admin_ok: bool
) -> None:
    h = bearer(client, role)
    assert (client.get(f"{V1}/overview", headers=h).status_code == 200) is get_ok
    r = client.post(f"{V1}/proposals/{world.proposal_waiting}/approve", headers=h, json={})
    assert (r.status_code == 200) is approve_ok, r.text
    if not approve_ok:
        assert r.status_code == 403
    r = client.post(f"{V1}/control/mode", headers=h, json={"mode": "suggest"})
    assert (r.status_code == 200) is admin_ok
    if not admin_ok:
        assert r.status_code == 403


def test_auth_not_configured_is_503(
    seeded: Session, api_settings_factory: Callable[..., Settings], clock: Clock
) -> None:
    for overrides in (
        {"admin_password_hash": None},
        {"secret_key": None},
        {"admin_password_hash": "plain"},
    ):
        app = build_app(seeded, api_settings_factory(**overrides), clock)
        with TestClient(app, base_url=BASE) as c:
            r = c.post(f"{V1}/auth/login", json={"username": "admin", "password": PASSWORD})
            assert r.status_code == 503, overrides


def test_main_system_mode_is_a_fail_closed_stub(
    seeded: Session, api_settings_factory: Callable[..., Settings], clock: Clock
) -> None:
    app = build_app(seeded, api_settings_factory(auth_mode="main_system"), clock)
    with TestClient(app, base_url=BASE) as c:
        assert c.get(f"{V1}/health").status_code == 200
        r = c.get(f"{V1}/overview", headers={"Authorization": "Bearer some.jwt.value"})
        assert r.status_code == 501
        assert r.json()["detail"]["phase"] == "6"
        r = c.post(f"{V1}/auth/login", json={"username": "admin", "password": PASSWORD})
        assert r.status_code == 409


def test_map_role_contract() -> None:
    role_map = {"ads-viewer": Role.VIEWER, "ads-approver": Role.APPROVER, "ads-admin": Role.ADMIN}
    assert map_role(["x", "ads-viewer", "ads-approver"], role_map) is Role.APPROVER
    assert map_role(["ads-admin"], role_map) is Role.ADMIN
    assert map_role(["x"], role_map) is None
    assert map_role([], {}) is None


def test_prometheus_metrics_token(
    client: TestClient,
    seeded: Session,
    api_settings_factory: Callable[..., Settings],
    clock: Clock,
) -> None:
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
    assert r.status_code == 200
    assert "api_requests_total" in r.text
    app = build_app(seeded, api_settings_factory(metrics_token=None), clock)
    with TestClient(app, base_url=BASE) as c:
        assert c.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 404
