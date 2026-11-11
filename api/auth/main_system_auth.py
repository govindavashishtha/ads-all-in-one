"""Main-system auth (AUTH_MODE=main_system, Phase 6): verify the main system's JWT via
MAIN_SYSTEM_JWKS_URL and map its roles to viewer/approver/admin (§24, INPUT I-11)."""

from __future__ import annotations

from fastapi import Request

from api.auth.adapter import User


class MainSystemAuth:
    def authenticate(self, request: Request) -> User:
        raise NotImplementedError("main-system auth is a Phase 6 deliverable (§24)")
