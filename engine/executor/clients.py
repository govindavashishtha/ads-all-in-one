"""Write-side platform clients for the executor (plan §3 credentials, §15, §20).

``PlatformClients`` pairs a state reader (a ReadConnector on the **read** credential: before /
after / reconcile reads) with a ``WriteConnector`` on the separate **write** credential. A
read-only credential is refused by the connector constructor (``WriteCredentialRequiredError``)
and a Meta token without ``ads_management`` by ``verify_write_access``.

``RealWriteClients`` is the production provider; ``FakeWriteClients`` serves tests and the demo
(in-memory ``FakePlatform``s, no network).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from engine.config import Settings
from engine.connectors.base import EntityState, KeywordRecord, WriteConnector
from engine.connectors.factory import COUNTERS, ConnectorFactory
from engine.connectors.fakes_write import FakePlatform, FakeStateReader, FakeWriteConnector
from engine.connectors.write_common import PlatformWriteError, WriteCredentialRequiredError
from engine.enums import Access, EntityType, Platform
from engine.errors import CredentialsUnavailableError, MissingInputError


class StateReader(Protocol):
    def read_state(self, entity_type: EntityType, external_id: str) -> EntityState: ...

    def list_keywords(self) -> list[KeywordRecord]: ...


@dataclass
class PlatformClients:
    platform: Platform
    reader: StateReader
    writer: WriteConnector


class WriteUnavailableError(RuntimeError):
    """No usable write path for a platform (missing/read-only credential, missing scope)."""


class WriteClientProvider(Protocol):
    def get(self, platform: Platform, *, conversion_action: str | None) -> PlatformClients: ...


@dataclass
class RealWriteClients:
    """SDK connectors: reader via ``ConnectorFactory`` (read token), writer via
    ``get_credentials(..., Access.WRITE)``. Built lazily per platform and cached."""

    settings: Settings
    connectors: ConnectorFactory
    _cache: dict[Platform, PlatformClients] = field(default_factory=dict)

    def get(self, platform: Platform, *, conversion_action: str | None) -> PlatformClients:
        if platform in self._cache:
            return self._cache[platform]
        from engine.connectors.google_write import GoogleWriteConnector
        from engine.connectors.meta_write import MetaWriteConnector
        from engine.credentials import get_credentials

        try:
            reader = self.connectors.build(platform, conversion_action=conversion_action)
            creds = get_credentials(
                self.settings.client_id, platform, Access.WRITE, settings=self.settings
            )
            writer: WriteConnector
            if platform is Platform.META:
                meta = MetaWriteConnector(creds, counter=COUNTERS[platform])
                meta.verify_write_access()
                writer = meta
            else:
                writer = GoogleWriteConnector(creds, counter=COUNTERS[platform])
        except (
            CredentialsUnavailableError,
            MissingInputError,
            WriteCredentialRequiredError,
            PlatformWriteError,
        ) as exc:
            raise WriteUnavailableError(f"{platform.value} write path unavailable: {exc}") from exc
        clients = PlatformClients(platform, reader, writer)
        self._cache[platform] = clients
        return clients


@dataclass
class FakeWriteClients:
    """In-memory platforms (tests, demo). A platform missing from the map is unavailable."""

    platforms: Mapping[Platform, FakePlatform]
    access: Access = Access.WRITE  # Access.READ simulates a read-only credential (refused)

    def get(self, platform: Platform, *, conversion_action: str | None) -> PlatformClients:
        fake = self.platforms.get(platform)
        if fake is None:
            raise WriteUnavailableError(f"{platform.value}: no fake platform configured")
        from engine.connectors.fakes_write import fake_credentials

        try:
            writer = FakeWriteConnector(fake, fake_credentials(platform, self.access))
        except WriteCredentialRequiredError as exc:
            raise WriteUnavailableError(str(exc)) from exc
        return PlatformClients(platform, FakeStateReader(fake), writer)
