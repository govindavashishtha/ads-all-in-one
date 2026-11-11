"""Write alerts rows (plan §21). Delivery (Slack/email/WhatsApp, re-send cadence) belongs to
``engine/notify`` — it picks up rows whose ``sent_channels`` is empty / due for re-send.

Dedup: one OPEN alert per ``(client_id, dedupe_key)`` (partial unique index WHERE acked_at IS
NULL). Raising an alert whose key is already open only refreshes ``message``/``severity``
(severity never downgrades) and returns ``created=False``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from engine.enums import EntityType, Severity

_UPSERT = text(
    """
    INSERT INTO alerts (client_id, dedupe_key, severity, kind, entity_type, entity_id, message)
    VALUES (:client_id, :dedupe_key, :severity, :kind, CAST(:entity_type AS entity_type),
            :entity_id, :message)
    ON CONFLICT (client_id, dedupe_key) WHERE acked_at IS NULL
    DO UPDATE SET message = EXCLUDED.message,
                  severity = CASE WHEN (CASE EXCLUDED.severity WHEN 'critical' THEN 2
                                         WHEN 'warn' THEN 1 ELSE 0 END)
                                     > (CASE alerts.severity WHEN 'critical' THEN 2
                                         WHEN 'warn' THEN 1 ELSE 0 END)
                                THEN EXCLUDED.severity ELSE alerts.severity END
    RETURNING id, (xmax = 0) AS created
    """
)


@dataclass(frozen=True, slots=True)
class AlertResult:
    id: int
    created: bool


def raise_alert(
    session: Session,
    client_id: str,
    *,
    dedupe_key: str,
    severity: Severity,
    kind: str,
    message: str,
    entity_type: EntityType | None = None,
    entity_id: int | None = None,
) -> AlertResult:
    row = session.execute(
        _UPSERT,
        {
            "client_id": client_id,
            "dedupe_key": dedupe_key,
            "severity": severity.value,
            "kind": kind,
            "entity_type": None if entity_type is None else entity_type.value,
            "entity_id": entity_id,
            "message": message,
        },
    ).one()
    return AlertResult(id=int(row.id), created=bool(row.created))
