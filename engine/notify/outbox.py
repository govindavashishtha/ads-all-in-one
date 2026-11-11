"""``notifications`` outbox (plan §5 "one notify() interface", §14 step 2).

Producers call ``enqueue`` inside their own transaction; ``engine.notify.dispatch`` delivers
queued rows later. Idempotent on ``(client_id, dedupe_key)``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import Notification
from engine.enums import NotificationKind, NotificationStatus


def enqueue(
    session: Session,
    client_id: str,
    *,
    kind: NotificationKind,
    dedupe_key: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    payload: dict[str, Any] | None = None,
    proposal_id: int | None = None,
    refresh_if_queued: bool = False,
) -> int | None:
    """Insert a queued notification; returns its id, or None if the key already existed.

    ``refresh_if_queued`` re-renders a row that has not been delivered yet (digest re-run).
    """
    values = {
        "client_id": client_id,
        "dedupe_key": dedupe_key,
        "kind": kind.value,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "payload": payload,
        "proposal_id": proposal_id,
        "status": NotificationStatus.QUEUED.value,
    }
    stmt = insert(Notification).values(**values)
    if refresh_if_queued:
        stmt = stmt.on_conflict_do_update(
            index_elements=["client_id", "dedupe_key"],
            set_={k: stmt.excluded[k] for k in ("subject", "body_text", "body_html", "payload")},
            where=Notification.status == NotificationStatus.QUEUED.value,
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["client_id", "dedupe_key"])
    new_id = session.scalar(stmt.returning(Notification.id))
    return int(new_id) if new_id is not None else None
