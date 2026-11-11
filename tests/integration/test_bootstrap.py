"""Bootstrap seeds once and only once (§7.1)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.bootstrap import bootstrap
from engine.config import Settings
from engine.db.models import AdAccount, AuditLog, Client, ConfigVersion
from engine.errors import ConfigError

pytestmark = pytest.mark.integration


def test_first_start_seeds_then_noop(db: Session, make_settings: Callable[..., Settings]) -> None:
    s = make_settings()
    assert bootstrap(db, s).created is True
    client = db.get(Client, "client_test")
    assert client is not None
    assert (client.mode, client.kill_switch, client.currency) == ("read_only", False, "INR")
    versions = db.scalars(select(ConfigVersion).order_by(ConfigVersion.kind)).all()
    assert [(v.kind, v.version, v.active) for v in versions] == [
        ("limits", 1, True),
        ("rules", 1, True),
    ]
    assert versions[1].body["targets"]["target_cpa"] == 500

    client.mode = "suggest"  # runtime change in DB must survive a restart
    db.flush()
    assert bootstrap(db, make_settings(initial_mode="read_only")).created is False
    assert db.get(Client, "client_test").mode == "suggest"  # type: ignore[union-attr]
    assert db.scalar(select(func.count()).select_from(ConfigVersion)) == 2
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_currency_mismatch_fails(db: Session, make_settings: Callable[..., Settings]) -> None:
    s = make_settings()
    bootstrap(db, s)
    db.add(
        AdAccount(
            client_id="client_test",
            platform="meta",
            external_id="1",
            currency="USD",
            timezone="Asia/Kolkata",
        )
    )
    db.flush()
    with pytest.raises(ConfigError, match="currenc"):
        bootstrap(db, s)
