"""Main-system auth (AUTH_MODE=main_system) — Phase 6 STUB (plan §24 seam 1, INPUT I-11).

Contract the Phase 6 implementation must satisfy (nothing else in the API changes):

1. ``authenticate(request, db) -> User`` reads the main system's credential from the request
   (``Authorization: Bearer <JWT>`` or the main system's session cookie — format is INPUT I-11).
2. Verify the JWT signature against ``MAIN_SYSTEM_JWKS_URL`` (cache keys, honour ``kid``),
   and ``exp`` / ``nbf`` / ``iss`` / ``aud`` (values from the main-system team).
3. Map the main system's role names to ``viewer`` / ``approver`` / ``admin`` with
   :func:`map_role` and a mapping supplied by the main-system team (never guessed here).
   No mapped role -> 403.
4. Return ``User(username=<sub or email claim>, role=<mapped>, session_kind="main_system")``.
5. The env login, ``/auth/login`` / ``/auth/token`` and the ``sessions`` table are bypassed
   (those endpoints answer 409 in this mode). CSRF for cookie-based main-system sessions is
   the main system's scheme (INPUT I-11); bearer JWTs need none.

Until then every request in this mode gets 501, so a misconfigured deployment fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from api.auth.adapter import User
from engine.config import Settings
from engine.enums import Role

_ORDER = (Role.VIEWER, Role.APPROVER, Role.ADMIN)


def map_role(external_roles: Iterable[str], role_map: Mapping[str, Role]) -> Role | None:
    """Highest local role granted by any of the external roles; None if none map."""
    granted = [role_map[r] for r in external_roles if r in role_map]
    return max(granted, key=_ORDER.index) if granted else None


class MainSystemAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(self, request: Request, db: Session) -> User:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "not_enabled",
                "phase": "6",
                "message": "AUTH_MODE=main_system is a Phase 6 deliverable (needs INPUT I-11: "
                "JWT/JWKS format and role names). Use AUTH_MODE=env.",
            },
        )
