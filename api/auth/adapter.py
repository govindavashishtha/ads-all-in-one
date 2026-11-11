"""Auth adapter (plan §17, §24 seam 1): the single entry point every route depends on.

``get_current_user(request) -> User``. Dispatches on ``AUTH_MODE``:
- ``env``         -> api/auth/env_auth.py (env admin, server-side sessions, CSRF)
- ``main_system`` -> api/auth/main_system_auth.py (JWT via JWKS) — Phase 6 stub

Routes never look at cookies, headers or the ``sessions`` table themselves; they depend on
``require(Role.X)`` from this module. Swapping the auth source later changes only this seam.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from api.deps import get_db
from engine.config import Settings
from engine.enums import Role


@dataclass(frozen=True, slots=True)
class User:
    username: str
    role: Role
    session_kind: str  # "browser" | "cli" | "main_system"

    def can(self, required: Role) -> bool:
        return self.role.includes(required)


class AuthBackend(Protocol):
    def authenticate(self, request: Request, db: Session) -> User:
        """Return the user or raise fastapi.HTTPException(401/403)."""
        ...


def backend_for(settings: Settings) -> AuthBackend:
    if settings.auth_mode == "main_system":
        from api.auth.main_system_auth import MainSystemAuth

        return MainSystemAuth(settings)
    from api.auth.env_auth import EnvAuth

    return EnvAuth(settings)


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """FastAPI dependency: ``user: User = Depends(get_current_user)``.

    Also enforces CSRF for cookie-authenticated state-changing requests (env mode).
    """
    user = backend_for(request.app.state.settings).authenticate(request, db)
    request.state.user = user
    return user


def require(role: Role) -> Callable[..., User]:
    """Dependency factory: authenticated user with at least ``role`` (§17 role table)."""

    def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not user.can(role):
            raise HTTPException(status_code=403, detail=f"requires role {role.value}")
        return user

    _dep.__name__ = f"require_{role.value}"
    return _dep


Viewer = Annotated[User, Depends(require(Role.VIEWER))]
Approver = Annotated[User, Depends(require(Role.APPROVER))]
Admin = Annotated[User, Depends(require(Role.ADMIN))]
