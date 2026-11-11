"""Build ReadConnectors for jobs. Real mode uses ``get_credentials`` + SDK transports;
fixture mode uses fake transports over a ``FixtureSet`` (no credentials, no network).

The Meta conversion action (§8.4, INPUT I-1) is passed in by the caller from the active rules
``config_versions`` row. In fixture mode the fixture's own (synthetic) action type is used.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from engine.config import Settings
from engine.connectors.base import ReadConnector
from engine.connectors.fakes import (
    FakeGoogleTransport,
    FakeMetaTransport,
    FixtureSet,
    fixture_google_credentials,
    fixture_meta_credentials,
)
from engine.connectors.google_read import GoogleReadConnector
from engine.connectors.meta_read import MetaReadConnector
from engine.connectors.resilience import OperationCounter, TokenBucket
from engine.credentials import get_credentials
from engine.enums import Access, Platform

# One counter per platform per process so jobs share today's operation count (§4.3).
COUNTERS: dict[Platform, OperationCounter] = {
    Platform.META: OperationCounter(Platform.META, daily_limit=None),
    Platform.GOOGLE: OperationCounter(Platform.GOOGLE, daily_limit=2_880),  # VERIFY-P0 V-1
}


def _no_throttle() -> TokenBucket:
    """Fixtures need no client-side rate limit."""
    return TokenBucket(rate=1e9, capacity=1e9)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class ConnectorFactory:
    settings: Settings
    fixtures: FixtureSet | None = None
    clock: Callable[[], dt.datetime] = _utcnow

    def build(self, platform: Platform, *, conversion_action: str | None) -> ReadConnector:
        """Raises MissingInputError / CredentialsUnavailableError when creds are absent."""
        if self.fixtures is not None:
            if platform is Platform.META:
                return MetaReadConnector(
                    fixture_meta_credentials(str(self.fixtures.meta["account"]["id"])),
                    conversion_action=self.fixtures.conversion_action,
                    transport=FakeMetaTransport(self.fixtures.meta),
                    clock=self.clock,
                    bucket=_no_throttle(),
                    counter=COUNTERS[platform],
                )
            return GoogleReadConnector(
                fixture_google_credentials(
                    str(self.fixtures.google["customer"][0]["customer"]["id"])
                ),
                transport=FakeGoogleTransport(self.fixtures.google),
                clock=self.clock,
                bucket=_no_throttle(),
                counter=COUNTERS[platform],
            )
        creds = get_credentials(
            self.settings.client_id, platform, Access.READ, settings=self.settings
        )
        if platform is Platform.META:
            return MetaReadConnector(
                creds,
                conversion_action=conversion_action,
                clock=self.clock,
                counter=COUNTERS[platform],
            )
        return GoogleReadConnector(creds, clock=self.clock, counter=COUNTERS[platform])
