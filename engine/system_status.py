"""``system_status`` key/value facts written by the engine, read by the API (migration 0003).

The API runs in its own process and never talks to the platforms, so anything only the
engine knows — the pacing job's projection, the heartbeat ping outcome, API operations used
today — is published here (latest value per key).

Keys: ``pacing:<platform>``, ``heartbeat``, ``operations:<platform>``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import SystemStatus

HEARTBEAT = "heartbeat"


def pacing_key(platform: str) -> str:
    return f"pacing:{platform}"


def operations_key(platform: str) -> str:
    return f"operations:{platform}"


def put_status(
    s: Session, client_id: str, key: str, value: dict[str, Any], now: dt.datetime
) -> None:
    """Upsert one key (caller owns the transaction)."""
    stmt = insert(SystemStatus).values(client_id=client_id, key=key, value=value, updated_at=now)
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=["client_id", "key"],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
    )


def get_statuses(
    s: Session, client_id: str, keys: list[str]
) -> dict[str, tuple[dict[str, Any], dt.datetime]]:
    rows = s.execute(
        select(SystemStatus.key, SystemStatus.value, SystemStatus.updated_at).where(
            SystemStatus.client_id == client_id, SystemStatus.key.in_(keys)
        )
    ).all()
    return {k: (v, ts) for k, v, ts in rows}
