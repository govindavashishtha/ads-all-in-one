"""Request-scoped dependencies. Everything comes from ``app.state`` (set by ``create_app``) so
tests can inject settings, a session factory and a clock."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import Client


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Callable[[], datetime]:
    clock: Callable[[], datetime] = request.app.state.clock
    return clock


def get_db(request: Request) -> Iterator[Session]:
    """One Session per request. Handlers that write call ``db.commit()`` explicitly; anything
    not committed is rolled back on close."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbDep = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]


def load_client(db: Session, settings: Settings) -> Client:
    client = db.get(Client, settings.client_id)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=f"client {settings.client_id!r} is not bootstrapped yet (run make bootstrap)",
        )
    return client
