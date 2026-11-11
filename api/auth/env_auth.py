"""Env admin auth (AUTH_MODE=env, plan §17, §20).

- One admin from env: ``ADMIN_USER`` + ``ADMIN_PASSWORD_HASH`` (argon2id, never plain text).
- Server-side, revocable sessions in ``sessions``; only ``sha256(token)`` is stored.
- Browser: opaque session id in an ``httpOnly; Secure; SameSite=Lax`` cookie + double-submit
  CSRF (readable ``aa_csrf`` cookie must equal the ``X-CSRF-Token`` header, and both must equal
  ``HMAC(SECRET_KEY, session)`` so a CSRF value cannot be forged or reused across sessions).
- CLI: ``Authorization: Bearer <token>`` (``sessions.kind = cli``); bearer requests skip CSRF.
- Sessions are checked on every request (revocation and expiry take effect immediately).
- Login rate limit: 5 failed attempts per 15 minutes per IP and per user, counted from the
  append-only ``audit_log`` (survives restarts, works across API workers).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth.adapter import User
from engine.config import Settings
from engine.db.models import AuditLog, Client
from engine.db.models import Session as SessionRow
from engine.enums import Role, SessionKind

SESSION_COOKIE = "aa_session"
CSRF_COOKIE = "aa_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
COOKIE_PATH = "/"

RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW = timedelta(minutes=15)
LOGIN_FAILED = "auth.login_failed"

_hasher = PasswordHasher()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _secret(settings: Settings) -> bytes:
    if settings.secret_key is None or len(settings.secret_key.get_secret_value()) < 32:
        raise HTTPException(
            status_code=503, detail="auth not configured: SECRET_KEY (32+ chars) is not set"
        )
    return settings.secret_key.get_secret_value().encode()


def csrf_for(settings: Settings, token_hash: str) -> str:
    return hmac.new(_secret(settings), f"csrf:{token_hash}".encode(), hashlib.sha256).hexdigest()


def _unauthorized(detail: str = "not authenticated") -> HTTPException:
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": 'Bearer realm="api"'}
    )


class EnvAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(self, request: Request, db: Session) -> User:
        clock: Callable[[], datetime] = request.app.state.clock
        token, via = _extract(request)
        if token is None:
            raise _unauthorized()
        token_hash = hash_token(token)
        row = db.scalars(
            select(SessionRow).where(
                SessionRow.client_id == self.settings.client_id,
                SessionRow.token_hash == token_hash,
            )
        ).one_or_none()
        if row is None or row.revoked_at is not None or row.expires_at <= clock():
            raise _unauthorized("session invalid, expired or revoked")
        if row.user != self.settings.admin_user:
            raise _unauthorized("session user no longer configured")
        expected_kind = SessionKind.CLI if via == "bearer" else SessionKind.BROWSER
        if row.kind != expected_kind.value:
            raise _unauthorized(f"a {row.kind} session cannot be used via {via}")
        if via == "cookie" and request.method.upper() not in SAFE_METHODS:
            header = request.headers.get(CSRF_HEADER, "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            expected = csrf_for(self.settings, token_hash)
            if not (
                header
                and hmac.compare_digest(header, cookie)
                and hmac.compare_digest(header, expected)
            ):
                raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
        request.state.session_id = row.id
        return User(username=row.user, role=Role(row.role), session_kind=row.kind)


def _extract(request: Request) -> tuple[str | None, str]:
    auth = request.headers.get("Authorization", "")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise _unauthorized("malformed Authorization header")
        return value.strip(), "bearer"
    cookie = request.cookies.get(SESSION_COOKIE)
    return (cookie, "cookie") if cookie else (None, "none")


# --------------------------------------------------------------------------- login


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str
    row: SessionRow


def _failed_attempts(
    db: Session, client_id: str, since: datetime, *, ip: str | None, user: str
) -> tuple[int, int]:
    base = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.client_id == client_id, AuditLog.action == LOGIN_FAILED, AuditLog.at >= since
        )
    )
    by_ip = db.scalar(base.where(AuditLog.ip == ip)) if ip else 0
    by_user = db.scalar(base.where(AuditLog.target == f"user:{user}"))
    return int(by_ip or 0), int(by_user or 0)


def _verify_password(settings: Settings, password: str) -> bool:
    if settings.admin_password_hash is None:
        raise HTTPException(
            status_code=503, detail="auth not configured: ADMIN_PASSWORD_HASH is not set"
        )
    stored = settings.admin_password_hash.get_secret_value()
    if not stored.startswith("$argon2id$"):
        raise HTTPException(
            status_code=503, detail="auth not configured: ADMIN_PASSWORD_HASH must be argon2id"
        )
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def login(
    db: Session,
    settings: Settings,
    now: datetime,
    *,
    username: str,
    password: str,
    ip: str | None,
    kind: SessionKind,
    role: Role | None = None,
) -> IssuedSession:
    """Verify credentials and create a ``sessions`` row. Commits (also on failure, so the
    failed attempt counts toward the rate limit)."""
    if settings.auth_mode != "env":
        raise HTTPException(
            status_code=409, detail="login is handled by the main system (AUTH_MODE=main_system)"
        )
    _secret(settings)  # fail fast (503) before touching anything if CSRF can't be signed
    if db.get(Client, settings.client_id) is None:
        raise HTTPException(
            status_code=503, detail=f"client {settings.client_id!r} is not bootstrapped yet"
        )
    by_ip, by_user = _failed_attempts(
        db, settings.client_id, now - RATE_LIMIT_WINDOW, ip=ip, user=username
    )
    if by_ip >= RATE_LIMIT_ATTEMPTS or by_user >= RATE_LIMIT_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="too many failed login attempts; try again later",
            headers={"Retry-After": str(int(RATE_LIMIT_WINDOW.total_seconds()))},
        )
    # Always run the hash check (constant-ish time whether or not the username matches).
    password_ok = _verify_password(settings, password)
    user_ok = hmac.compare_digest(username.encode(), settings.admin_user.encode())
    if not (password_ok and user_ok):
        db.add(
            AuditLog(
                client_id=settings.client_id,
                actor="anonymous",
                action=LOGIN_FAILED,
                target=f"user:{username}",
                payload={"kind": kind.value},
                ip=ip,
                at=now,
            )
        )
        db.commit()
        raise _unauthorized("invalid username or password")

    user_role = Role.ADMIN  # the env admin is admin (§17)
    granted = role or user_role
    if not user_role.includes(granted):
        raise HTTPException(status_code=403, detail="cannot grant a role above your own")
    token = secrets.token_urlsafe(32)
    row = SessionRow(
        client_id=settings.client_id,
        token_hash=hash_token(token),
        kind=kind.value,
        user=settings.admin_user,
        role=granted.value,
        expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
    )
    db.add(row)
    db.flush()
    db.add(
        AuditLog(
            client_id=settings.client_id,
            actor=settings.admin_user,
            action="auth.login",
            target=f"session:{row.id}",
            payload={"kind": kind.value, "role": granted.value},
            ip=ip,
            at=now,
        )
    )
    db.commit()
    return IssuedSession(token=token, row=row)


def set_browser_cookies(response: Response, settings: Settings, issued: IssuedSession) -> str:
    csrf = csrf_for(settings, issued.row.token_hash)
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=True,
        httponly=False,
        samesite="lax",
    )
    return csrf


def clear_browser_cookies(response: Response) -> None:
    for name, http_only in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(
            name, path=COOKIE_PATH, secure=True, httponly=http_only, samesite="lax"
        )


def revoke(
    db: Session, settings: Settings, session_id: int, now: datetime, *, actor: str, ip: str | None
) -> bool:
    row = db.get(SessionRow, session_id)
    if row is None or row.client_id != settings.client_id:
        return False
    if row.revoked_at is None:
        row.revoked_at = now
        db.add(
            AuditLog(
                client_id=settings.client_id,
                actor=actor,
                action="auth.revoke",
                target=f"session:{row.id}",
                payload={"kind": row.kind},
                ip=ip,
                at=now,
            )
        )
    db.commit()
    return True
