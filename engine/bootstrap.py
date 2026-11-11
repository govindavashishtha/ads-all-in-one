"""First-start seeding (plan §7.1).

If ``clients`` has no row for ``CLIENT_ID``: insert it (mode = INITIAL_MODE) and version 1 of
rules and limits from the YAML seeds. If the row exists: change nothing. Every start also
re-checks invariants that must hold before any job runs (fail fast):

- HARD_DAILY_CAP / HARD_MONTHLY_CAP set (INPUT I-2);
- seed YAML validates; unconfirmed placeholder inputs are refused outside development;
- seed caps do not exceed HARD_* ceilings;
- all known ad accounts share one currency equal to the client currency (§8.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import AdAccount, AuditLog, Client, ConfigVersion
from engine.enums import ConfigKind
from engine.errors import ConfigError, MissingInputError
from engine.guardrails.limits_schema import LimitsConfig, parse_limits
from engine.rules.schema import RulesConfig, parse_rules

log = structlog.get_logger(__name__)

ENGINE_DIR = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = ENGINE_DIR / "rules" / "rules.yaml"
DEFAULT_LIMITS_PATH = ENGINE_DIR / "guardrails" / "limits.yaml"
_BOOTSTRAP_LOCK_KEY = 0x0AD5_0001  # pg advisory lock key shared by engine + api starts


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    created: bool
    client_id: str


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def validate_seeds(
    settings: Settings, rules_body: dict[str, Any], limits_body: dict[str, Any]
) -> tuple[RulesConfig, LimitsConfig]:
    hard_daily, hard_monthly = settings.require_hard_caps()
    rules = parse_rules(rules_body)
    limits = parse_limits(limits_body)
    if settings.app_env != "development":
        if not rules.inputs_confirmed:
            raise MissingInputError("I-1", "rules.yaml still has placeholder targets")
        if not limits.inputs_confirmed:
            raise MissingInputError("I-2", "limits.yaml still has placeholder caps")
        if not rules.targets.meta_conversion_action:
            raise MissingInputError("I-1", "targets.meta_conversion_action is empty")
    limits.check_hard_ceilings(hard_daily, hard_monthly)
    return rules, limits


def check_account_currencies(session: Session, client_id: str, client_currency: str) -> None:
    """§8.1: both ad accounts must use the same currency (and match the client's)."""
    currencies = set(
        session.scalars(select(AdAccount.currency).where(AdAccount.client_id == client_id))
    )
    if currencies and currencies != {client_currency}:
        raise ConfigError(
            f"ad account currencies {sorted(currencies)} differ from client currency "
            f"{client_currency!r}; v1 requires one currency across platforms"
        )


def bootstrap(
    session: Session,
    settings: Settings,
    rules_path: Path = DEFAULT_RULES_PATH,
    limits_path: Path = DEFAULT_LIMITS_PATH,
) -> BootstrapResult:
    """Seed the DB on first start. Caller provides the session; this commits nothing itself
    except via the caller's transaction (use ``with session.begin():``)."""
    rules_body = load_yaml(rules_path)
    limits_body = load_yaml(limits_path)
    rules, _limits = validate_seeds(settings, rules_body, limits_body)

    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})
    existing = session.get(Client, settings.client_id)
    if existing is not None:
        check_account_currencies(session, existing.client_id, existing.currency)
        log.info("bootstrap.skip", client_id=settings.client_id, reason="client exists")
        return BootstrapResult(created=False, client_id=settings.client_id)

    cid = settings.client_id
    session.add(
        Client(
            client_id=cid,
            name=settings.client_name,
            timezone=settings.timezone,
            currency=rules.targets.currency,
            mode=settings.initial_mode.value,
            kill_switch=False,  # env KILL_SWITCH is a separate hard override (§7.1)
        )
    )
    session.flush()
    for kind, body in ((ConfigKind.RULES, rules_body), (ConfigKind.LIMITS, limits_body)):
        session.add(
            ConfigVersion(
                client_id=cid,
                kind=kind.value,
                version=1,
                body=body,
                active=True,
                changed_by="bootstrap",
                reason="seed from YAML on first start",
            )
        )
    session.add(
        AuditLog(
            client_id=cid,
            actor="bootstrap",
            action="bootstrap.seed",
            target=f"client:{cid}",
            payload={
                "mode": settings.initial_mode.value,
                "rules_inputs_confirmed": rules.inputs_confirmed,
                "limits_inputs_confirmed": bool(limits_body.get("inputs_confirmed", False)),
            },
        )
    )
    session.flush()
    log.info("bootstrap.seeded", client_id=cid, mode=settings.initial_mode.value)
    return BootstrapResult(created=True, client_id=cid)


def main() -> None:
    """`python -m engine.bootstrap` — seed using DATABASE_URL from the environment."""
    from engine.config import get_settings
    from engine.db.session import make_engine, make_sessionmaker

    settings = get_settings()
    factory = make_sessionmaker(make_engine(settings.database_url))
    with factory() as session, session.begin():
        result = bootstrap(session, settings)
    print(f"bootstrap: client={result.client_id} created={result.created}")


if __name__ == "__main__":
    main()
