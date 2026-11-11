"""Active config_versions rows (§7.1: the DB is the runtime source of truth)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import TargetsOut
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


def active_targets(db: Session, client_id: str) -> TargetsOut | None:
    """``targets`` of the active rules version, read leniently (display only)."""
    row = active_version(db, client_id, ConfigKind.RULES)
    if row is None or not isinstance(row.body, dict):
        return None
    raw = row.body.get("targets")
    if not isinstance(raw, dict):
        return None
    objective = raw.get("objective")
    return TargetsOut(
        currency=raw.get("currency") if isinstance(raw.get("currency"), str) else None,
        objective=objective if objective in ("conversions", "value", "traffic") else None,
        target_cpa=_money(raw.get("target_cpa")),
        target_roas=_money(raw.get("target_roas")),
        rules_version=row.version,
    )


def _money(v: object) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d > 0 else None
