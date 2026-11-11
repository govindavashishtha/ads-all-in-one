"""Active config_versions rows (§7.1: the DB is the runtime source of truth)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import ConfigVersion
from engine.enums import ConfigKind
from engine.guardrails.limits_schema import LimitsConfig, parse_limits


def active_version(db: Session, client_id: str, kind: ConfigKind) -> ConfigVersion | None:
    return db.scalars(
        select(ConfigVersion).where(
            ConfigVersion.client_id == client_id,
            ConfigVersion.kind == kind.value,
            ConfigVersion.active.is_(True),
        )
    ).one_or_none()


def active_limits(db: Session, client_id: str) -> tuple[LimitsConfig | None, int | None]:
    row = active_version(db, client_id, ConfigKind.LIMITS)
    if row is None:
        return None, None
    try:
        return parse_limits(row.body), row.version
    except ValueError:
        return None, row.version
