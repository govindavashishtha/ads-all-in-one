"""Auth adapter (plan §17, §24 seam 1): the single entry point every route depends on.

``get_current_user(request) -> User``. Dispatches on ``AUTH_MODE``:
- ``env``         -> api/auth/env_auth.py (env admin, server-side sessions, CSRF) — API agent
- ``main_system`` -> api/auth/main_system_auth.py (JWT via JWKS) — Phase 6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from engine.config import get_settings
from engine.enums import Role


@dataclass(frozen=True, slots=True)
class User:
    username: str
    role: Role
    session_kind: str  # "browser" | "cli" | "main_system"

    def can(self, required: Role) -> bool:
        return self.role.includes(required)


class AuthBackend(Protocol):
    def authenticate(self, request: Request) -> User:
        """Return the user or raise fastapi.HTTPException(401)."""
        ...


def _backend() -> AuthBackend:
    if get_settings().auth_mode == "main_system":
        from api.auth.main_system_auth import MainSystemAuth

        return MainSystemAuth()
    from api.auth.env_auth import EnvAuth

    return EnvAuth()


def get_current_user(request: Request) -> User:
    """FastAPI dependency: ``user: User = Depends(get_current_user)``."""
    return _backend().authenticate(request)
