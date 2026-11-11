"""Shared HTTP error helpers."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import HTTPException

from api.schemas import NotEnabledResponse

NOT_ENABLED_RESPONSES: dict[int | str, dict[str, Any]] = {
    501: {"description": "Not enabled in this phase", "model": NotEnabledResponse},
}


def not_enabled(phase: str, message: str) -> NoReturn:
    """Raise 501 for a §17 route whose backing logic ships in a later phase (§23).

    Never fake the action: callers must not change any state before raising this.
    """
    raise HTTPException(
        status_code=501,
        detail={"code": "not_enabled", "phase": phase, "message": message},
    )


def not_found(what: str, ident: object) -> NoReturn:
    raise HTTPException(status_code=404, detail=f"{what} {ident} not found")
