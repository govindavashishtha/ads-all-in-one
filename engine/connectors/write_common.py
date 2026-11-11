"""Shared pieces for the WriteConnectors (plan §8-§9, §15, §20).

- ``PlatformWriteError``: the ONLY exception a write connector raises for a platform failure.
  ``kind`` is a closed vocabulary the executor understands without importing an SDK, and
  ``applied`` says what we know about the platform state afterwards:
  ``False`` = the platform rejected the request (nothing changed), ``None`` = unknown (a timeout
  or 5xx after the request may have been processed) → the executor leaves the write-ahead row
  ``applying`` and the reconciler resolves it by re-reading platform state (§15).
- ``WriteCredentialRequiredError``: a read credential was handed to a write connector (§20:
  read and write credentials are separate; a read token must never be used for writes).
- Local validation used where a platform has no validate-only mode for an operation
  (§15 step 4, VERIFY-P0 V-11 / V-12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from engine.connectors.base import EntityStatus
from engine.credentials.base import PlatformCredentials
from engine.enums import Access, Platform


class WriteErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"  # throttled before processing (retried with backoff)
    PERMISSION = "permission"  # token lacks write scope / user lacks access (never retried)
    AUTH = "auth"  # invalid / expired credential
    INVALID = "invalid"  # request rejected by validation (bad value, unsupported op)
    NOT_FOUND = "not_found"
    TRANSIENT = "transient"  # platform-side transient error; outcome may be unknown
    PARTIAL = "partial"  # some operations of a multi-step / multi-op write failed
    UNSUPPORTED = "unsupported"  # operation not available on this platform / scope in v1


@dataclass(eq=False)
class PlatformWriteError(RuntimeError):
    platform: Platform
    kind: WriteErrorKind
    message: str
    applied: bool | None = False  # False = nothing changed; None = unknown (reconcile)
    code: str | None = None
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, f"{self.platform.value} {self.kind.value}: {self.message}")

    def as_json(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "kind": self.kind.value,
            "message": self.message[:1000],
            "applied": self.applied,
            "code": self.code,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class WriteCredentialRequiredError(ValueError):
    """A write connector was given a credential that is not ``access=write`` (§20)."""


def require_write_credentials(credentials: PlatformCredentials, platform: Platform) -> None:
    if credentials.access is not Access.WRITE:
        raise WriteCredentialRequiredError(
            f"{platform.value} write connector refused a {credentials.access.value} credential: "
            "writes need the separate write credential from get_credentials(..., access=write)"
        )


# Statuses the engine may set. It never deletes/removes entities (only pause / resume).
SETTABLE_STATUSES = frozenset({EntityStatus.ACTIVE, EntityStatus.PAUSED})


def local_budget_problem(amount: Decimal) -> str | None:
    """Local validation where validate-only is unavailable (§15 step 4): type and range only.
    Platform minimum budgets are VERIFY-P0 and enforced by the guardrail's min_daily_budget."""
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return f"budget must be a finite Decimal, got {amount!r}"
    if amount <= 0:
        return f"budget must be > 0, got {amount}"
    if amount != amount.quantize(Decimal("0.01")):
        return f"budget must have at most 2 decimals, got {amount}"
    return None
