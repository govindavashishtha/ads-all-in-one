"""Meta WriteConnector (facebook-business SDK, Graph API POSTs) — plan §4.1, §8.2, §15, §20.

Operations (Phase 3): pause / resume (campaign, ad set, ad), daily budget (campaign or ad set,
whichever is the budget unit — the guardrail decides), and new ads created **PAUSED** from an
existing ad's creative with replaced text. Negative keywords do not exist on Meta.

Safety:
- needs the separate WRITE credential (``access=write``, system user with ``ads_management``);
  a read credential is refused in ``__init__`` and ``verify_write_access`` checks the token's
  real scopes via ``debug_token`` (VERIFY-P0 V-25) so a mislabeled read token is refused too;
- ``validate_only`` → the same request with ``execution_options=["validate_only"]``
  (VERIFY-P0 V-12); where unsupported, validated locally (types, ranges);
- throttling (V-20 codes) is retried with backoff (the request was not processed); transient
  errors are retried only for absolute "set" operations (status / budget), never for creates —
  an ambiguous create raises ``PlatformWriteError(applied=None)`` for the reconciler;
- money via ``engine.connectors.money`` only (minor units, V-3).

Every Graph field / parameter here is VERIFY-P0 (docs/VERIFIED.md V-12, V-25, V-28).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

from engine.connectors import money
from engine.connectors.base import AdDraft, EntityStatus, WriteConnector, WriteResult
from engine.connectors.meta_read import RATE_LIMIT_CODES
from engine.connectors.resilience import (
    API_CALLS,
    OperationCounter,
    RetryPolicy,
    TokenBucket,
    call_with_retries,
)
from engine.connectors.write_common import (
    SETTABLE_STATUSES,
    PlatformWriteError,
    WriteErrorKind,
    local_budget_problem,
    require_write_credentials,
)
from engine.credentials.base import MetaCredentials, PlatformCredentials
from engine.enums import EntityType, Platform

# ----------------------------------------------------------------------- VERIFY-P0 constants

VALIDATE_ONLY_OPTION = ("validate_only",)  # V-12: execution_options value
WRITE_SCOPE = "ads_management"  # V-25 / §4.3: system-user write scope
STATUS_VALUES = {EntityStatus.ACTIVE: "ACTIVE", EntityStatus.PAUSED: "PAUSED"}
BUDGET_ENTITY_TYPES = frozenset({EntityType.CAMPAIGN, EntityType.AD_GROUP})
STATUS_ENTITY_TYPES = frozenset({EntityType.CAMPAIGN, EntityType.AD_GROUP, EntityType.AD})
# Template creative fields copied for a new ad (V-28). Text is replaced in link_data /
# video_data; every other part of the story spec (page, link, image/video) is kept.
TEMPLATE_AD_FIELDS = "id,adset_id,creative{id,object_story_spec,url_tags}"
# Graph error codes (V-20, V-28): auth / permission / not found / invalid parameter.
AUTH_CODES = frozenset({102, 190})
PERMISSION_CODES = frozenset({10, 200, 294})
NOT_FOUND_CODES = frozenset({803})
INVALID_CODES = frozenset({100, 1487390, 2635})
TRANSIENT_CODES = frozenset({1, 2})


class MetaWriteTransport(Protocol):
    """POST to / GET one Graph node. ``path`` has no leading slash or API version."""

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...


class SdkMetaWriteTransport:
    """Real transport over ``facebook_business.api.FacebookAdsApi``. Never used in tests."""

    def __init__(self, credentials: MetaCredentials) -> None:
        from facebook_business.api import FacebookAdsApi
        from facebook_business.session import FacebookSession

        # A dedicated API instance (not the SDK's process-wide default, which the read
        # transport sets) so the write token is never used by read code paths.
        session = FacebookSession(
            app_id=credentials.app_id,
            app_secret=credentials.app_secret.get_secret_value(),
            access_token=credentials.token.get_secret_value(),
            timeout=60,
        )
        self._api = FacebookAdsApi(session, api_version=credentials.api_version)

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = self._api.call("POST", (path,), params=params).json()
        return body

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = self._api.call("GET", (path,), params=params).json()
        return body


def _attr(exc: BaseException, name: str) -> Any:
    value = getattr(exc, name, None)
    try:
        return value() if callable(value) else value
    except Exception:
        return None


def map_meta_error(exc: BaseException, *, idempotent: bool) -> PlatformWriteError:
    """FacebookRequestError (or any exception) → ``PlatformWriteError`` (V-20)."""
    code = _attr(exc, "api_error_code")
    message = str(_attr(exc, "api_error_message") or exc)
    transient = bool(_attr(exc, "api_transient_error"))
    http = _attr(exc, "http_status")
    common: dict[str, Any] = {
        "platform": Platform.META,
        "message": message,
        "code": None if code is None else str(code),
    }
    if code in RATE_LIMIT_CODES:
        return PlatformWriteError(kind=WriteErrorKind.RATE_LIMITED, retryable=True, **common)
    if code in AUTH_CODES:
        return PlatformWriteError(kind=WriteErrorKind.AUTH, **common)
    if code in PERMISSION_CODES:
        return PlatformWriteError(kind=WriteErrorKind.PERMISSION, **common)
    if code in NOT_FOUND_CODES:
        return PlatformWriteError(kind=WriteErrorKind.NOT_FOUND, **common)
    if code in INVALID_CODES or (isinstance(http, int) and 400 <= http < 500 and not transient):
        return PlatformWriteError(kind=WriteErrorKind.INVALID, **common)
    # transient / 5xx / connection errors: the request may or may not have been processed
    return PlatformWriteError(
        kind=WriteErrorKind.TRANSIENT,
        retryable=idempotent and (transient or code in TRANSIENT_CODES or code is None),
        applied=None,
        **common,
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _unsupported(message: str) -> PlatformWriteError:
    return PlatformWriteError(
        platform=Platform.META, kind=WriteErrorKind.UNSUPPORTED, message=message
    )


class MetaWriteConnector(WriteConnector):
    platform: ClassVar[Platform] = Platform.META

    def __init__(
        self,
        credentials: PlatformCredentials,
        *,
        transport: MetaWriteTransport | None = None,
        clock: Callable[[], dt.datetime] = _utcnow,
        retry: RetryPolicy | None = None,
        bucket: TokenBucket | None = None,
        counter: OperationCounter | None = None,
    ) -> None:
        require_write_credentials(credentials, Platform.META)
        super().__init__(credentials)
        if not isinstance(credentials, MetaCredentials):
            raise TypeError("MetaWriteConnector needs MetaCredentials")
        self.creds: MetaCredentials = credentials
        self._transport = transport if transport is not None else SdkMetaWriteTransport(credentials)
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._bucket = bucket or TokenBucket(rate=1.0, capacity=5.0)
        self.counter = counter or OperationCounter(Platform.META, daily_limit=None)
        self._currency: str | None = None
        self._timezone: str = "UTC"

    # ------------------------------------------------------------------ plumbing

    @property
    def act(self) -> str:
        return self.creds.ad_account_id

    def _call(self, fn: Callable[[], dict[str, Any]], *, idempotent: bool) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            try:
                return fn()
            except PlatformWriteError:
                raise
            except Exception as exc:
                raise map_meta_error(exc, idempotent=idempotent) from exc

        self._bucket.acquire()
        API_CALLS.labels(platform="meta", kind="write").inc()
        try:
            return call_with_retries(
                attempt,
                is_retryable=lambda e: isinstance(e, PlatformWriteError) and e.retryable,
                policy=self._retry,
                platform=Platform.META,
            )
        finally:
            self.counter.add(self._clock().astimezone(ZoneInfo(self._timezone)).date())

    def _post(
        self, path: str, params: dict[str, Any], *, validate_only: bool, idempotent: bool
    ) -> dict[str, Any]:
        body = dict(params)
        if validate_only:
            body["execution_options"] = list(VALIDATE_ONLY_OPTION)
        return self._call(lambda: self._transport.post(path, body), idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._call(lambda: self._transport.get_node(path, params), idempotent=True)

    def currency(self) -> str:
        if self._currency is None:
            raw = self._get(self.act, {"fields": "currency,timezone_name"})
            self._currency = str(raw["currency"]).upper()
            self._timezone = str(raw.get("timezone_name") or "UTC")
        return self._currency

    @staticmethod
    def _ok(body: dict[str, Any]) -> bool:
        return bool(body.get("success", True)) and "error" not in body

    # ------------------------------------------------------------------ preflight

    def verify_write_access(self) -> None:
        """Refuse a token without ``ads_management`` (a read token in the write slot), §20.
        ``GET /debug_token`` with the app access token (V-25)."""
        raw = self._get(
            "debug_token",
            {
                "input_token": self.creds.token.get_secret_value(),
                "access_token": f"{self.creds.app_id}|{self.creds.app_secret.get_secret_value()}",
            },
        )
        data = raw.get("data") or {}
        scopes = {str(s) for s in data.get("scopes") or []}
        if not data.get("is_valid", False):
            raise PlatformWriteError(
                platform=Platform.META, kind=WriteErrorKind.AUTH, message="write token invalid"
            )
        if WRITE_SCOPE not in scopes:
            raise PlatformWriteError(
                platform=Platform.META,
                kind=WriteErrorKind.PERMISSION,
                message=f"write token lacks {WRITE_SCOPE} (scopes: {sorted(scopes)}); "
                "refusing to use a read-only token for writes",
            )

    # ------------------------------------------------------------------ writes

    def set_status(
        self,
        entity_type: EntityType,
        external_id: str,
        status: EntityStatus,
        *,
        validate_only: bool,
    ) -> WriteResult:
        if entity_type not in STATUS_ENTITY_TYPES:
            raise _unsupported(f"status change not supported for {entity_type.value}")
        if status not in SETTABLE_STATUSES:
            raise _unsupported(f"the engine only pauses/resumes (got {status.value})")
        body = self._post(
            external_id,
            {"status": STATUS_VALUES[status]},
            validate_only=validate_only,
            idempotent=True,
        )
        return WriteResult(ok=self._ok(body), validate_only=validate_only, platform_response=body)

    def set_daily_budget(
        self, entity_type: EntityType, external_id: str, amount: Decimal, *, validate_only: bool
    ) -> WriteResult:
        if entity_type not in BUDGET_ENTITY_TYPES:
            raise _unsupported(f"daily budget not supported for {entity_type.value}")
        problem = local_budget_problem(amount)
        if problem is not None:
            return WriteResult(ok=False, validate_only=validate_only, error=problem)
        minor = money.major_to_meta_minor(amount, self.currency())
        body = self._post(
            external_id,
            {"daily_budget": str(minor)},
            validate_only=validate_only,
            idempotent=True,
        )
        return WriteResult(
            ok=self._ok(body),
            validate_only=validate_only,
            platform_response={**body, "daily_budget_minor": minor},
        )

    def add_negative_keyword(
        self, ad_group_external_id: str, text: str, match_type: str, *, validate_only: bool
    ) -> WriteResult:
        raise _unsupported("Meta has no negative keywords")

    def remove_negative_keyword(
        self, ad_group_external_id: str, criterion_external_id: str, *, validate_only: bool
    ) -> WriteResult:
        raise _unsupported("Meta has no negative keywords")

    def create_ad(self, draft: AdDraft, *, validate_only: bool) -> WriteResult:
        """New ad in ``draft.ad_group_external_id`` built from the template ad's creative
        (``draft.extra['template_ad_id']``) with headline/body replaced. Always PAUSED."""
        template_id = str(draft.extra.get("template_ad_id") or "")
        if not template_id:
            return WriteResult(
                ok=False, validate_only=validate_only, error="draft needs extra.template_ad_id"
            )
        if not (draft.headline or draft.body):
            return WriteResult(ok=False, validate_only=validate_only, error="empty ad text")
        tmpl = self._get(template_id, {"fields": TEMPLATE_AD_FIELDS})
        spec = dict((tmpl.get("creative") or {}).get("object_story_spec") or {})
        key = next((k for k in ("link_data", "video_data") if k in spec), None)
        if key is None:
            return WriteResult(
                ok=False,
                validate_only=validate_only,
                error="template creative has no link_data/video_data story spec (V-28)",
            )
        data = dict(spec[key])
        title_field = "name" if key == "link_data" else "title"
        if draft.headline:
            data[title_field] = draft.headline
        if draft.body:
            data["message"] = draft.body
        spec[key] = data
        name = str(draft.extra.get("name") or f"ads-autopilot draft {template_id}")[:100]
        creative = self._post(
            f"{self.act}/adcreatives",
            {"name": name, "object_story_spec": spec},
            validate_only=validate_only,
            idempotent=False,
        )
        if validate_only:
            # the ad can't be validated against a creative that doesn't exist yet: its fields
            # are fixed here (status PAUSED, adset id from the DB) and validated locally.
            return WriteResult(
                ok=self._ok(creative),
                validate_only=True,
                platform_response={"creative": creative, "ad": "validated locally"},
            )
        creative_id = str(creative.get("id") or "")
        if not creative_id:
            raise PlatformWriteError(
                platform=Platform.META,
                kind=WriteErrorKind.INVALID,
                message="adcreative create returned no id",
                detail={"response": creative},
            )
        try:
            ad = self._post(
                f"{self.act}/ads",
                {
                    "name": name,
                    "adset_id": draft.ad_group_external_id,
                    "creative": {"creative_id": creative_id},
                    "status": "PAUSED",  # §2 non-goals, §15 step 5: never auto-launched
                },
                validate_only=False,
                idempotent=False,
            )
        except PlatformWriteError as exc:
            if exc.applied is False:
                exc.kind = WriteErrorKind.PARTIAL
            exc.detail = {**exc.detail, "orphan_creative_id": creative_id}
            raise
        ad_id = str(ad.get("id") or "") or None
        return WriteResult(
            ok=ad_id is not None,
            validate_only=False,
            platform_response={"creative_id": creative_id, "ad": ad},
            created_external_id=ad_id,
        )
