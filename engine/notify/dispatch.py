"""Deliver queued ``notifications`` rows (plan §14 step 2, §21).

Each queued row is offered to every *deliverable* channel (configured + transport wired); if
none is, it is written to the log channel. The row records ``sent_channels``, attempts and
the last error. A row fails permanently after ``MAX_ATTEMPTS``.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import Notification
from engine.enums import NotificationStatus
from engine.notify.adapters import Channel, LogChannel, OutboundMessage, default_channels

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 5


def deliver_pending(
    session: Session,
    settings: Settings,
    now: dt.datetime,
    channels: list[Channel] | None = None,
    limit: int = 200,
) -> dict[str, int]:
    chans = [c for c in (channels or default_channels()) if c.deliverable(settings)]
    if not chans:
        chans = [LogChannel()]
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.client_id == settings.client_id,
            Notification.status == NotificationStatus.QUEUED.value,
        )
        .order_by(Notification.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    stats = {"sent": 0, "failed": 0}
    for row in rows:
        msg = OutboundMessage(row.id, row.kind, row.subject, row.body_text, row.body_html)
        sent: list[str] = list(row.sent_channels or [])
        errors: list[str] = []
        for ch in chans:
            if ch.name in sent:
                continue
            try:
                ch.send(msg)
                sent.append(ch.name)
            except Exception as exc:
                errors.append(f"{ch.name}: {type(exc).__name__}: {exc}")
                log.warning("notify.channel_failed", channel=ch.name, notification_id=row.id)
        row.attempts = (row.attempts or 0) + 1
        row.sent_channels = sent
        row.last_error = "; ".join(errors)[:2000] or None
        if sent and not errors:
            row.status = NotificationStatus.SENT.value
            row.sent_at = now
            stats["sent"] += 1
        elif row.attempts >= MAX_ATTEMPTS:
            row.status = NotificationStatus.FAILED.value
            stats["failed"] += 1
    session.flush()
    return stats
