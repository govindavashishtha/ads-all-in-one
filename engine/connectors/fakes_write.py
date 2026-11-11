"""In-memory fake ad platform for the write path (plan §22 "Crash safety", "Idempotency").

``FakePlatform`` holds the tracked state of one platform (status / daily budget per entity,
negative keywords, created ads) and serves both sides the executor uses:

- ``FakeStateReader.read_state`` — what a ReadConnector would read back;
- ``FakeWriteConnector`` — a real ``WriteConnector`` subclass (so the executor's types and the
  write-credential check are exercised) whose writes mutate the fake state.

Fault injection for tests (all off by default): validate-only rejection, a rejected or
ambiguous apply (``PlatformWriteError`` with ``applied=False`` / ``None``), a simulated process
crash before or after the platform applied the change (``SimulatedCrash`` is a
``BaseException`` so ordinary ``except Exception`` handlers can't swallow it), and a platform
that applies a different value than requested (read-back mismatch). Nothing here talks to a
network; it is used by tests and the demo only.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import SecretStr

from engine.connectors.base import (
    AdDraft,
    EntityState,
    EntityStatus,
    KeywordRecord,
    WriteConnector,
    WriteResult,
)
from engine.connectors.write_common import (
    SETTABLE_STATUSES,
    PlatformWriteError,
    WriteErrorKind,
    local_budget_problem,
    require_write_credentials,
)
from engine.credentials.base import GoogleCredentials, MetaCredentials, PlatformCredentials
from engine.enums import Access, EntityType, Platform


class SimulatedCrash(BaseException):
    """The executor process "dies" at this point (tests only)."""


def fake_credentials(platform: Platform, access: Access = Access.WRITE) -> PlatformCredentials:
    """Placeholder credentials for fakes (never valid against a platform)."""
    if platform is Platform.META:
        return MetaCredentials(
            access=access,
            app_id="fake",
            app_secret=SecretStr("fake"),
            token=SecretStr("fake"),
            ad_account_id="act_000000000000",
            api_version="fake",
        )
    return GoogleCredentials(
        access=access,
        customer_id="0000000000",
        login_customer_id=None,
        cloud_project_id="fake",
        oauth_client_id="fake",
        oauth_client_secret=SecretStr("fake"),
        refresh_token=SecretStr("fake"),
    )


@dataclass
class FakeEntity:
    status: EntityStatus
    daily_budget: Decimal | None = None


@dataclass
class FakeNegative:
    ad_group_external_id: str
    text: str
    match_type: str
    status: EntityStatus = EntityStatus.ACTIVE


@dataclass
class Faults:
    reject_validate: set[str] = field(default_factory=set)  # op names
    reject_apply: dict[str, PlatformWriteError] = field(default_factory=dict)
    unknown_after_apply: set[str] = field(default_factory=set)  # applied, then ambiguous error
    crash_before_apply: set[str] = field(default_factory=set)
    crash_after_apply: set[str] = field(default_factory=set)
    budget_applied_as: Decimal | None = None  # platform stores a different value


@dataclass
class FakePlatform:
    platform: Platform
    entities: dict[tuple[EntityType, str], FakeEntity] = field(default_factory=dict)
    negatives: dict[str, FakeNegative] = field(default_factory=dict)  # "ag~crit" -> negative
    faults: Faults = field(default_factory=Faults)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(900_000_001))

    @property
    def applied_writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c["validate_only"] and c.get("applied")]

    def add(
        self,
        entity_type: EntityType,
        external_id: str,
        status: EntityStatus,
        daily_budget: Decimal | None = None,
    ) -> None:
        self.entities[(entity_type, external_id)] = FakeEntity(status, daily_budget)

    def next_id(self) -> str:
        return str(next(self._ids))

    def state(self, entity_type: EntityType, external_id: str) -> EntityState:
        if entity_type is EntityType.KEYWORD and external_id in self.negatives:
            neg = self.negatives[external_id]
            return EntityState(
                self.platform,
                entity_type,
                external_id,
                neg.status,
                None,
                {"negative": True, "text": neg.text, "match_type": neg.match_type},
            )
        ent = self.entities.get((entity_type, external_id))
        if ent is None:
            raise LookupError(f"fake {self.platform.value} {entity_type} {external_id} not found")
        return EntityState(self.platform, entity_type, external_id, ent.status, ent.daily_budget)


class FakeStateReader:
    """Read side (``read_state`` / ``list_keywords``) over a ``FakePlatform``."""

    def __init__(self, platform: FakePlatform) -> None:
        self.fake = platform

    def read_state(self, entity_type: EntityType, external_id: str) -> EntityState:
        return self.fake.state(entity_type, external_id)

    def list_keywords(self) -> list[KeywordRecord]:
        return [
            KeywordRecord(
                external_id=crit,
                ad_group_external_id=n.ad_group_external_id,
                text=n.text,
                match_type=n.match_type,
                status=n.status,
                is_negative=True,
            )
            for crit, n in self.fake.negatives.items()
        ]


class FakeWriteConnector(WriteConnector):
    """A ``WriteConnector`` over a ``FakePlatform`` (requires an ``access=write`` credential)."""

    platform: ClassVar[Platform] = Platform.META  # overridden per instance below

    def __init__(self, fake: FakePlatform, credentials: PlatformCredentials | None = None) -> None:
        creds = credentials or fake_credentials(fake.platform)
        require_write_credentials(creds, fake.platform)  # same refusal as the real ones
        super().__init__(creds)
        self.fake = fake
        self.platform = fake.platform  # type: ignore[misc]

    # ------------------------------------------------------------------ plumbing

    def _run(self, op: str, validate_only: bool, apply: Any, **detail: Any) -> WriteResult:
        call: dict[str, Any] = {"op": op, "validate_only": validate_only, **detail}
        self.fake.calls.append(call)
        f = self.fake.faults
        if validate_only:
            if op in f.reject_validate:
                return WriteResult(ok=False, validate_only=True, error=f"fake: {op} rejected")
            return WriteResult(ok=True, validate_only=True)
        if op in f.crash_before_apply:
            raise SimulatedCrash(f"crash before {op}")
        if op in f.reject_apply:
            raise f.reject_apply[op]
        result: WriteResult = apply()
        call["applied"] = True
        if op in f.crash_after_apply:
            raise SimulatedCrash(f"crash after {op}")
        if op in f.unknown_after_apply:
            raise PlatformWriteError(
                platform=self.fake.platform,
                kind=WriteErrorKind.TRANSIENT,
                message="fake: timeout after the request was sent",
                applied=None,
            )
        return result

    # ------------------------------------------------------------------ writes

    def set_status(
        self,
        entity_type: EntityType,
        external_id: str,
        status: EntityStatus,
        *,
        validate_only: bool,
    ) -> WriteResult:
        if status not in SETTABLE_STATUSES:
            raise PlatformWriteError(
                platform=self.fake.platform,
                kind=WriteErrorKind.UNSUPPORTED,
                message=f"only pause/resume, got {status}",
            )

        def apply() -> WriteResult:
            if entity_type is EntityType.KEYWORD and external_id in self.fake.negatives:
                self.fake.negatives[external_id].status = status
            else:
                ent = self.fake.entities.get((entity_type, external_id))
                if ent is None:
                    raise PlatformWriteError(
                        platform=self.fake.platform,
                        kind=WriteErrorKind.NOT_FOUND,
                        message=f"{entity_type} {external_id} not found",
                    )
                ent.status = status
            return WriteResult(ok=True, validate_only=False)

        return self._run(
            "set_status",
            validate_only,
            apply,
            entity_type=entity_type.value,
            external_id=external_id,
            status=status.value,
        )

    def set_daily_budget(
        self, entity_type: EntityType, external_id: str, amount: Decimal, *, validate_only: bool
    ) -> WriteResult:
        problem = local_budget_problem(amount)
        if problem is not None:
            return WriteResult(ok=False, validate_only=validate_only, error=problem)

        def apply() -> WriteResult:
            ent = self.fake.entities.get((entity_type, external_id))
            if ent is None:
                raise PlatformWriteError(
                    platform=self.fake.platform,
                    kind=WriteErrorKind.NOT_FOUND,
                    message=f"{entity_type} {external_id} not found",
                )
            ent.daily_budget = self.fake.faults.budget_applied_as or amount
            return WriteResult(ok=True, validate_only=False)

        return self._run(
            "set_daily_budget",
            validate_only,
            apply,
            entity_type=entity_type.value,
            external_id=external_id,
            amount=str(amount),
        )

    def add_negative_keyword(
        self, ad_group_external_id: str, text: str, match_type: str, *, validate_only: bool
    ) -> WriteResult:
        if self.fake.platform is not Platform.GOOGLE:
            raise PlatformWriteError(
                platform=self.fake.platform,
                kind=WriteErrorKind.UNSUPPORTED,
                message="no negative keywords on this platform",
            )
        created = f"{ad_group_external_id}~{self.fake.next_id()}"

        def apply() -> WriteResult:
            self.fake.negatives[created] = FakeNegative(ad_group_external_id, text, match_type)
            return WriteResult(ok=True, validate_only=False, created_external_id=created)

        return self._run(
            "add_negative_keyword",
            validate_only,
            apply,
            ad_group_external_id=ad_group_external_id,
            text=text,
            match_type=match_type,
        )

    def remove_negative_keyword(
        self, ad_group_external_id: str, criterion_external_id: str, *, validate_only: bool
    ) -> WriteResult:
        def apply() -> WriteResult:
            neg = self.fake.negatives.get(criterion_external_id)
            if neg is None:
                raise PlatformWriteError(
                    platform=self.fake.platform,
                    kind=WriteErrorKind.NOT_FOUND,
                    message=f"negative {criterion_external_id} not found",
                )
            neg.status = EntityStatus.REMOVED
            return WriteResult(ok=True, validate_only=False)

        return self._run(
            "remove_negative_keyword",
            validate_only,
            apply,
            criterion_external_id=criterion_external_id,
        )

    def create_ad(self, draft: AdDraft, *, validate_only: bool) -> WriteResult:
        group = draft.ad_group_external_id
        ad_id = self.fake.next_id()
        created = f"{group}~{ad_id}" if self.fake.platform is Platform.GOOGLE else ad_id

        def apply() -> WriteResult:
            self.fake.add(EntityType.AD, created, EntityStatus.PAUSED)  # always PAUSED
            return WriteResult(ok=True, validate_only=False, created_external_id=created)

        return self._run(
            "create_ad",
            validate_only,
            apply,
            ad_group_external_id=group,
            headline=draft.headline,
            body=draft.body,
        )
