"""/auth — sessions (§17). Login/token are the only unauthenticated POSTs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from api.auth import env_auth
from api.auth.adapter import Admin, User, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip
from api.schemas import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    SessionOut,
    TokenRequest,
    TokenResponse,
)
from engine.db.models import Session as SessionRow
from engine.enums import Role, SessionKind

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    *,
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> LoginResponse:
    """Browser login: sets the httpOnly session cookie and the readable CSRF cookie."""
    issued = env_auth.login(
        db,
        settings,
        clock(),
        username=body.username,
        password=body.password,
        ip=client_ip(request),
        kind=SessionKind.BROWSER,
    )
    csrf = env_auth.set_browser_cookies(response, settings, issued)
    return LoginResponse(
        username=issued.row.user,
        role=Role(issued.row.role),
        expires_at=issued.row.expires_at,
        csrf_token=csrf,
    )


@router.post("/token", response_model=TokenResponse)
def token(
    *, body: TokenRequest, request: Request, db: DbDep, settings: SettingsDep, clock: ClockDep
) -> TokenResponse:
    """CLI login: returns an opaque bearer token (``sessions.kind = cli``)."""
    issued = env_auth.login(
        db,
        settings,
        clock(),
        username=body.username,
        password=body.password,
        ip=client_ip(request),
        kind=SessionKind.CLI,
        role=body.role,
    )
    return TokenResponse(
        access_token=issued.token,
        username=issued.row.user,
        role=Role(issued.row.role),
        expires_at=issued.row.expires_at,
    )


@router.post("/logout", status_code=204)
def logout(
    *,
    request: Request,
    response: Response,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> Response:
    """Revoke the current session (cookie or bearer) and clear browser cookies."""
    session_id = getattr(request.state, "session_id", None)
    if session_id is not None:
        env_auth.revoke(
            db, settings, session_id, clock(), actor=user.username, ip=client_ip(request)
        )
    response.status_code = 204
    env_auth.clear_browser_cookies(response)
    return response


@router.get("/me", response_model=MeResponse)
def me(*, user: Viewer, settings: SettingsDep) -> MeResponse:
    return MeResponse(
        username=user.username,
        role=user.role,
        session_kind=user.session_kind,
        auth_mode=settings.auth_mode,
    )


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    *, request: Request, user: Admin, db: DbDep, settings: SettingsDep, clock: ClockDep
) -> list[SessionOut]:
    """Active (unexpired, unrevoked) sessions — admin only."""
    current = getattr(request.state, "session_id", None)
    rows = db.scalars(
        select(SessionRow)
        .where(
            SessionRow.client_id == settings.client_id,
            SessionRow.revoked_at.is_(None),
            SessionRow.expires_at > clock(),
        )
        .order_by(SessionRow.id.desc())
    ).all()
    return [
        SessionOut.model_validate(r).model_copy(update={"current": r.id == current}) for r in rows
    ]


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke_session(
    *,
    session_id: int,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> Response:
    """Revoke any session (e.g. a lost CLI token) — admin only."""
    if not env_auth.revoke(
        db, settings, session_id, clock(), actor=user.username, ip=client_ip(request)
    ):
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return Response(status_code=204)


__all__ = ["User", "router"]
