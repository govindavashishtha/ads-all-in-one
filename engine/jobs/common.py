"""Helpers shared by the ingestion jobs: active config, enabled platforms, time windows."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.connectors.base import ReadConnector
from engine.db.models import Client, ConfigVersion
from engine.enums import ConfigKind, Mode, Platform, Severity
from engine.errors import CredentialsUnavailableError, MissingInputError
from engine.guardrails.limits_schema import LimitsConfig, parse_limits
from engine.ingest.alerts import raise_alert
from engine.jobs.runner import JobContext
from engine.rules.schema import RulesConfig, parse_rules

log = structlog.get_logger(__name__)


def active_config(session: Session, client_id: str, kind: ConfigKind) -> dict[str, object]:
    body = session.scalar(
        select(ConfigVersion.body).where(
            ConfigVersion.client_id == client_id,
            ConfigVersion.kind == kind.value,
            ConfigVersion.active.is_(True),
        )
    )
    if body is None:
        raise MissingInputError("bootstrap", f"no active {kind.value} config_versions row")
    return dict(body)


def active_rules(session: Session, client_id: str) -> RulesConfig:
    return parse_rules(active_config(session, client_id, ConfigKind.RULES))


def active_limits(session: Session, client_id: str) -> LimitsConfig:
    return parse_limits(active_config(session, client_id, ConfigKind.LIMITS))


def client_mode(session: Session, client_id: str) -> Mode:
    mode = session.scalar(select(Client.mode).where(Client.client_id == client_id))
    if mode is None:
        raise MissingInputError("bootstrap", f"clients row {client_id!r} missing")
    return Mode(mode)


@dataclass(frozen=True, slots=True)
class PlatformConnector:
    platform: Platform
    connector: ReadConnector


@dataclass
class Enabled:
    connectors: list[PlatformConnector]
    skipped: dict[Platform, str]


def enabled_connectors(ctx: JobContext, session: Session) -> Enabled:
    """Build read connectors; a platform without credentials/inputs is skipped + warn alert."""
    try:
        conversion_action: str | None = active_rules(
            session, ctx.client_id
        ).targets.meta_conversion_action
    except MissingInputError:
        conversion_action = None
    out: list[PlatformConnector] = []
    skipped: dict[Platform, str] = {}
    for platform in (Platform.META, Platform.GOOGLE):
        try:
            conn = ctx.connectors.build(platform, conversion_action=conversion_action)
        except (MissingInputError, CredentialsUnavailableError) as exc:
            skipped[platform] = str(exc)
            raise_alert(
                session,
                ctx.client_id,
                dedupe_key=f"credentials_missing:{platform.value}",
                severity=Severity.WARN,
                kind="credential_invalid",
                message=f"{platform.value} read credentials unavailable: {exc}",
            )
            continue
        out.append(PlatformConnector(platform, conn))
    return Enabled(out, skipped)


def local_now(now: dt.datetime, tz: str) -> dt.datetime:
    return now.astimezone(ZoneInfo(tz))


def hour_floor(t: dt.datetime) -> dt.datetime:
    """Naive account-local start of the hour."""
    return t.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def client_window_key_hour(now: dt.datetime, tz: str) -> str:
    return local_now(now, tz).strftime("%Y-%m-%dT%H")


def client_yesterday(now: dt.datetime, tz: str) -> dt.date:
    return local_now(now, tz).date() - dt.timedelta(days=1)
