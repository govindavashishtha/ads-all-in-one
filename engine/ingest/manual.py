"""Manual-change lookups for other layers (plan §9.2, §12.3 check 5 cooldown).

Manual changes are recorded by ``engine.ingest.entities`` as ``alerts`` rows with
``kind='manual_change'`` (plus the ``entity_history`` row). The cooldown clock starts at the
alert's ``created_at``; acking the alert does not end the cooldown.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.db.models import Alert
from engine.enums import EntityType

MANUAL_CHANGE_KIND = "manual_change"


def last_manual_change_at(
    session: Session, client_id: str, entity_type: EntityType, entity_id: int
) -> dt.datetime | None:
    return session.scalar(
        select(func.max(Alert.created_at)).where(
            Alert.client_id == client_id,
            Alert.kind == MANUAL_CHANGE_KIND,
            Alert.entity_type == entity_type,
            Alert.entity_id == entity_id,
        )
    )


def manual_changes_since(
    session: Session, client_id: str, since: dt.datetime
) -> dict[tuple[EntityType, int], dt.datetime]:
    """Latest manual change per entity since ``since`` (bulk form for guardrail state)."""
    rows = session.execute(
        select(Alert.entity_type, Alert.entity_id, func.max(Alert.created_at))
        .where(
            Alert.client_id == client_id,
            Alert.kind == MANUAL_CHANGE_KIND,
            Alert.created_at >= since,
            Alert.entity_id.is_not(None),
        )
        .group_by(Alert.entity_type, Alert.entity_id)
    )
    return {
        (EntityType(r[0]), int(r[1])): r[2] for r in rows if r[0] is not None and r[1] is not None
    }
