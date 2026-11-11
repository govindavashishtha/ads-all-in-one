"""Env admin auth (AUTH_MODE=env): argon2id password hash, server-side `sessions` rows,
httpOnly cookie for browsers + double-submit CSRF, bearer tokens for the CLI, login rate
limit 5/15 min per IP and per user (§17, §20). NOT IMPLEMENTED — API agent."""

from __future__ import annotations

from fastapi import Request

from api.auth.adapter import User


class EnvAuth:
    def authenticate(self, request: Request) -> User:
        raise NotImplementedError("env auth is implemented by the API agent (§17)")
