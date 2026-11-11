"""Google Ads WriteConnector (google-ads SDK mutate operations) — plan §4.1, §8.2, §15, §20.

Operations (Phase 5): pause / resume (campaign, ad group, ad, keyword), campaign daily budget
(non-shared budgets only), negative keywords (ad group level; shared negative lists with
partial-failure handling) and responsive search ad (RSA) drafts created **PAUSED**.

Safety:
- needs the separate WRITE credential (``GOOGLE_WRITE_REFRESH_TOKEN`` user with standard
  access); a read credential is refused in ``__init__``. Google has no token scopes for this:
  a read-only user's mutate fails with an authorization error → ``PERMISSION``;
- ``validate_only=True`` on the same mutate request (VERIFY-P0 V-11);
- quota errors (RESOURCE_EXHAUSTED / quota_error) are retried with backoff (not processed);
  INTERNAL / UNAVAILABLE / DEADLINE_EXCEEDED are retried only for absolute "set" updates —
  creates raise ``PlatformWriteError(applied=None)`` for the reconciler;
- each mutate operation counts toward the daily operations limit (§4.3, V-1/V-24);
- money via ``engine.connectors.money`` only (micros, V-5).

API version pinned in docs/VERSIONS.md (``GOOGLE_ADS_API_VERSION`` shared with the reader).
Field names: VERIFY-P0 V-11, V-26, V-29 (docs/VERIFIED.md).
"""

# ruff: noqa: S608  (GAQL strings built from validated ints; not SQL)
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

from engine.connectors import money
from engine.connectors.base import AdDraft, EntityStatus, WriteConnector, WriteResult
from engine.connectors.google_read import GOOGLE_ADS_API_VERSION
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
from engine.credentials.base import GoogleCredentials, PlatformCredentials
from engine.enums import EntityType, Platform

# ----------------------------------------------------------------------- VERIFY-P0 constants

STATUS_VALUES = {EntityStatus.ACTIVE: "ENABLED", EntityStatus.PAUSED: "PAUSED"}
MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})
# RSA limits (V-29): 3–15 headlines ≤ 30 chars, 2–4 descriptions ≤ 90 chars.
RSA_HEADLINES = (3, 15, 30)
RSA_DESCRIPTIONS = (2, 4, 90)
EXPLORER_DAILY_OPERATIONS = 2_880  # §4.3, V-1

# service key → (SDK service, operation type, mutate method)
SERVICES: dict[str, tuple[str, str, str]] = {
    "campaign": ("CampaignService", "CampaignOperation", "mutate_campaigns"),
    "campaign_budget": (
        "CampaignBudgetService",
        "CampaignBudgetOperation",
        "mutate_campaign_budgets",
    ),
    "ad_group": ("AdGroupService", "AdGroupOperation", "mutate_ad_groups"),
    "ad_group_ad": ("AdGroupAdService", "AdGroupAdOperation", "mutate_ad_group_ads"),
    "ad_group_criterion": (
        "AdGroupCriterionService",
        "AdGroupCriterionOperation",
        "mutate_ad_group_criteria",
    ),
    "shared_criterion": (
        "SharedCriterionService",
        "SharedCriterionOperation",
        "mutate_shared_criteria",
    ),
}
RETRY_STATUS = frozenset({"RESOURCE_EXHAUSTED"})
UNKNOWN_STATUS = frozenset({"INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"})
ERROR_KIND: dict[str, WriteErrorKind] = {
    "quota_error": WriteErrorKind.RATE_LIMITED,
    "authorization_error": WriteErrorKind.PERMISSION,
    "authentication_error": WriteErrorKind.AUTH,
    "not_allowlisted_error": WriteErrorKind.PERMISSION,
    "internal_error": WriteErrorKind.TRANSIENT,
    "resource_not_found_error": WriteErrorKind.NOT_FOUND,
}


class GoogleWriteTransport(Protocol):
    """``mutate`` returns ``{"results": [{"resource_name": ...}, ...],
    "partial_failure_errors": [{"index": i, "message": str, "error_code": str}, ...]}``."""

    def mutate(
        self,
        service: str,
        operations: list[dict[str, Any]],
        *,
        validate_only: bool,
        partial_failure: bool,
    ) -> dict[str, Any]: ...

    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...


class SdkGoogleWriteTransport:
    """Real transport over ``GoogleAdsClient`` (write user). Never used in tests."""

    def __init__(self, credentials: GoogleCredentials) -> None:
        from google.ads.googleads.client import GoogleAdsClient

        config: dict[str, Any] = {
            "client_id": credentials.oauth_client_id,
            "client_secret": credentials.oauth_client_secret.get_secret_value(),
            "refresh_token": credentials.refresh_token.get_secret_value(),
            "use_proto_plus": False,
        }
        if credentials.login_customer_id:
            config["login_customer_id"] = credentials.login_customer_id.replace("-", "")
        self._client = GoogleAdsClient.load_from_dict(config, version=GOOGLE_ADS_API_VERSION)
        self._customer_id = credentials.customer_id.replace("-", "")

    def mutate(
        self,
        service: str,
        operations: list[dict[str, Any]],
        *,
        validate_only: bool,
        partial_failure: bool,
    ) -> dict[str, Any]:
        from google.protobuf.json_format import (  # type: ignore[import-untyped]
            MessageToDict,
            ParseDict,
        )

        svc_name, op_type, method = SERVICES[service]
        svc = self._client.get_service(svc_name)
        ops = [ParseDict(op, self._client.get_type(op_type)) for op in operations]
        resp = getattr(svc, method)(
            customer_id=self._customer_id,
            operations=ops,
            partial_failure=partial_failure,
            validate_only=validate_only,
        )
        body = MessageToDict(resp, preserving_proto_field_name=True)
        errors: list[dict[str, Any]] = []
        pfe: Any = getattr(resp, "partial_failure_error", None)
        if pfe is not None and pfe.code:
            failure_type = self._client.get_type("GoogleAdsFailure")
            for detail in pfe.details:
                failure = type(failure_type)()
                detail.Unpack(failure)
                for err in failure.errors:
                    elems = list(err.location.field_path_elements)
                    errors.append(
                        {
                            "index": elems[0].index if elems else None,
                            "message": err.message,
                            "error_code": err.error_code.WhichOneof("error_code"),
                        }
                    )
        return {"results": body.get("results", []), "partial_failure_errors": errors}

    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from google.protobuf.json_format import MessageToDict

        svc = self._client.get_service("GoogleAdsService")
        out: list[dict[str, Any]] = []
        for batch in svc.search_stream(customer_id=self._customer_id, query=query):
            out.extend(MessageToDict(r, preserving_proto_field_name=True) for r in batch.results)
        return out


def _status_name(exc: BaseException) -> str:
    err = getattr(exc, "error", None)
    code_fn = getattr(err, "code", None)
    if callable(code_fn):
        try:
            return str(getattr(code_fn(), "name", ""))
        except Exception:
            return ""
    return ""


def _error_codes(exc: BaseException) -> list[str]:
    failure = getattr(exc, "failure", None)
    out: list[str] = []
    for err in getattr(failure, "errors", None) or []:
        ec = getattr(err, "error_code", None)
        which = getattr(ec, "WhichOneof", None)
        if callable(which):
            try:
                name = which("error_code")
            except Exception:
                name = None
            if name:
                out.append(str(name))
                continue
        if isinstance(ec, str):
            out.append(ec)
    return out


def map_google_error(exc: BaseException, *, idempotent: bool) -> PlatformWriteError:
    """GoogleAdsException / gRPC error → ``PlatformWriteError`` (V-23)."""
    status = _status_name(exc)
    codes = _error_codes(exc)
    common: dict[str, Any] = {
        "platform": Platform.GOOGLE,
        "message": str(exc)[:1000],
        "code": ",".join(codes) or status or None,
    }
    if status in RETRY_STATUS or "quota_error" in codes:
        return PlatformWriteError(kind=WriteErrorKind.RATE_LIMITED, retryable=True, **common)
    for c in codes:
        kind = ERROR_KIND.get(c)
        if kind is WriteErrorKind.TRANSIENT:
            return PlatformWriteError(kind=kind, applied=None, retryable=idempotent, **common)
        if kind is not None:
            return PlatformWriteError(kind=kind, **common)
    if codes:  # any other GoogleAdsFailure (field/range/criterion/mutate...) = rejected
        return PlatformWriteError(kind=WriteErrorKind.INVALID, **common)
    # gRPC transport problems or unknown exceptions: outcome unknown
    return PlatformWriteError(
        kind=WriteErrorKind.TRANSIENT,
        applied=None,
        retryable=idempotent and (status in UNKNOWN_STATUS or not status),
        **common,
    )


@dataclass(frozen=True, slots=True)
class NegativeItem:
    text: str
    ok: bool
    created_external_id: str | None
    error: str | None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _last(resource_name: Any) -> str:
    return str(resource_name).rsplit("/", 1)[-1]


class GoogleWriteConnector(WriteConnector):
    platform: ClassVar[Platform] = Platform.GOOGLE

    def __init__(
        self,
        credentials: PlatformCredentials,
        *,
        transport: GoogleWriteTransport | None = None,
        clock: Callable[[], dt.datetime] = _utcnow,
        retry: RetryPolicy | None = None,
        bucket: TokenBucket | None = None,
        counter: OperationCounter | None = None,
        timezone: str = "UTC",
    ) -> None:
        require_write_credentials(credentials, Platform.GOOGLE)
        super().__init__(credentials)
        if not isinstance(credentials, GoogleCredentials):
            raise TypeError("GoogleWriteConnector needs GoogleCredentials")
        self.creds: GoogleCredentials = credentials
        self._transport = (
            transport if transport is not None else SdkGoogleWriteTransport(credentials)
        )
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._bucket = bucket or TokenBucket(rate=1.0, capacity=5.0)
        self.counter = counter or OperationCounter(Platform.GOOGLE, EXPLORER_DAILY_OPERATIONS)
        self._tz = timezone

    # ------------------------------------------------------------------ plumbing

    @property
    def customer(self) -> str:
        return self.creds.customer_id.replace("-", "")

    def _rn(self, collection: str, ident: str) -> str:
        return f"customers/{self.customer}/{collection}/{ident}"

    def _count(self, n: int) -> None:
        self.counter.add(self._clock().astimezone(ZoneInfo(self._tz)).date(), n)

    def _guard(self, fn: Callable[[], Any], *, idempotent: bool) -> Any:
        def attempt() -> Any:
            try:
                return fn()
            except PlatformWriteError:
                raise
            except Exception as exc:
                raise map_google_error(exc, idempotent=idempotent) from exc

        self._bucket.acquire()
        return call_with_retries(
            attempt,
            is_retryable=lambda e: isinstance(e, PlatformWriteError) and e.retryable,
            policy=self._retry,
            platform=Platform.GOOGLE,
        )

    def _mutate(
        self,
        service: str,
        operations: list[dict[str, Any]],
        *,
        validate_only: bool,
        idempotent: bool,
        partial_failure: bool = False,
    ) -> dict[str, Any]:
        API_CALLS.labels(platform="google", kind="write").inc()
        try:
            body: dict[str, Any] = self._guard(
                lambda: self._transport.mutate(
                    service,
                    operations,
                    validate_only=validate_only,
                    partial_failure=partial_failure,
                ),
                idempotent=idempotent,
            )
        finally:
            self._count(len(operations))
        if body.get("partial_failure_errors") and not partial_failure:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.INVALID,
                message="mutate returned errors",
                detail={"errors": body["partial_failure_errors"]},
            )
        return body

    def _search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        API_CALLS.labels(platform="google", kind="read").inc()
        try:
            rows: list[dict[str, Any]] = self._guard(
                lambda: self._transport.search(name, query, params), idempotent=True
            )
        finally:
            self._count(1)
        return rows

    @staticmethod
    def _split(external_id: str) -> tuple[int, int]:
        ag, _, child = external_id.partition("~")
        if not child:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.INVALID,
                message=f"expected '<ad_group_id>~<id>', got {external_id!r}",
            )
        return int(ag), int(child)

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
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.UNSUPPORTED,
                message=f"the engine only pauses/resumes (got {status.value})",
            )
        if entity_type is EntityType.CAMPAIGN:
            service, rn = "campaign", self._rn("campaigns", str(int(external_id)))
        elif entity_type is EntityType.AD_GROUP:
            service, rn = "ad_group", self._rn("adGroups", str(int(external_id)))
        elif entity_type is EntityType.AD:
            ag, ad = self._split(external_id)
            service, rn = "ad_group_ad", self._rn("adGroupAds", f"{ag}~{ad}")
        elif entity_type is EntityType.KEYWORD:
            ag, crit = self._split(external_id)
            service, rn = "ad_group_criterion", self._rn("adGroupCriteria", f"{ag}~{crit}")
        else:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.UNSUPPORTED,
                message=f"status change not supported for {entity_type.value}",
            )
        op = {
            "update": {"resource_name": rn, "status": STATUS_VALUES[status]},
            "update_mask": "status",
        }
        body = self._mutate(service, [op], validate_only=validate_only, idempotent=True)
        return WriteResult(ok=True, validate_only=validate_only, platform_response=body)

    def set_daily_budget(
        self, entity_type: EntityType, external_id: str, amount: Decimal, *, validate_only: bool
    ) -> WriteResult:
        if entity_type is not EntityType.CAMPAIGN:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.UNSUPPORTED,
                message="Google budgets live on the campaign budget (campaign level only)",
            )
        problem = local_budget_problem(amount)
        if problem is not None:
            return WriteResult(ok=False, validate_only=validate_only, error=problem)
        rows = self._search(
            "write_budget_lookup",
            "SELECT campaign.id, campaign.campaign_budget, campaign_budget.explicitly_shared, "
            f"campaign_budget.period FROM campaign WHERE campaign.id = {int(external_id)}",
            {"id": external_id},
        )
        if len(rows) != 1:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.NOT_FOUND,
                message=f"campaign {external_id} not found",
            )
        budget = rows[0].get("campaign_budget") or {}
        if budget.get("explicitly_shared"):  # V-26; v1 never edits shared budgets (§2)
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.UNSUPPORTED,
                message="campaign uses a shared budget (not supported in v1)",
            )
        if str(budget.get("period") or "DAILY") != "DAILY":
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.UNSUPPORTED,
                message=f"campaign budget period {budget.get('period')} is not DAILY",
            )
        rn = rows[0]["campaign"]["campaign_budget"]
        micros = money.major_to_google_micros(amount)
        op = {
            "update": {"resource_name": rn, "amount_micros": str(micros)},
            "update_mask": "amount_micros",
        }
        body = self._mutate("campaign_budget", [op], validate_only=validate_only, idempotent=True)
        return WriteResult(
            ok=True,
            validate_only=validate_only,
            platform_response={**body, "amount_micros": micros, "budget_resource": rn},
        )

    @staticmethod
    def _match(match_type: str) -> str:
        m = match_type.upper()
        if m not in MATCH_TYPES:
            raise PlatformWriteError(
                platform=Platform.GOOGLE,
                kind=WriteErrorKind.INVALID,
                message=f"unknown match type {match_type!r}",
            )
        return m

    def add_negative_keyword(
        self, ad_group_external_id: str, text: str, match_type: str, *, validate_only: bool
    ) -> WriteResult:
        if not text.strip():
            return WriteResult(ok=False, validate_only=validate_only, error="empty keyword text")
        op = {
            "create": {
                "ad_group": self._rn("adGroups", str(int(ad_group_external_id))),
                "negative": True,
                "status": "ENABLED",
                "keyword": {"text": text.strip(), "match_type": self._match(match_type)},
            }
        }
        body = self._mutate(
            "ad_group_criterion", [op], validate_only=validate_only, idempotent=False
        )
        results = body.get("results") or []
        created = _last(results[0]["resource_name"]) if results and not validate_only else None
        return WriteResult(
            ok=True,
            validate_only=validate_only,
            platform_response=body,
            created_external_id=created,
        )

    def remove_negative_keyword(
        self, ad_group_external_id: str, criterion_external_id: str, *, validate_only: bool
    ) -> WriteResult:
        crit = criterion_external_id.rpartition("~")[2]
        op = {"remove": self._rn("adGroupCriteria", f"{int(ad_group_external_id)}~{int(crit)}")}
        # a remove of an already-removed criterion fails as NOT_FOUND, so retrying is safe
        body = self._mutate(
            "ad_group_criterion", [op], validate_only=validate_only, idempotent=True
        )
        return WriteResult(ok=True, validate_only=validate_only, platform_response=body)

    def add_negatives_to_shared_set(
        self, shared_set_id: str, texts: list[str], match_type: str, *, validate_only: bool
    ) -> tuple[WriteResult, list[NegativeItem]]:
        """Negative keyword list (shared set). ``partial_failure=True``: good items apply even
        if others fail; per-item outcomes are returned. Not used by the v1 executor (rules only
        propose ad-group negatives); available for a later shared-list flow."""
        match = self._match(match_type)
        clean = [t.strip() for t in texts if t.strip()]
        if not clean:
            return WriteResult(ok=False, validate_only=validate_only, error="no keywords"), []
        ops = [
            {
                "create": {
                    "shared_set": self._rn("sharedSets", str(int(shared_set_id))),
                    "keyword": {"text": t, "match_type": match},
                }
            }
            for t in clean
        ]
        body = self._mutate(
            "shared_criterion",
            ops,
            validate_only=validate_only,
            idempotent=False,
            partial_failure=True,
        )
        errors = {e.get("index"): e for e in body.get("partial_failure_errors") or []}
        results = body.get("results") or []
        items: list[NegativeItem] = []
        for i, text in enumerate(clean):
            err = errors.get(i)
            rn = results[i].get("resource_name") if i < len(results) else None
            items.append(
                NegativeItem(
                    text=text,
                    ok=err is None,
                    created_external_id=_last(rn) if rn and err is None else None,
                    error=None if err is None else str(err.get("message")),
                )
            )
        failed = sum(1 for it in items if not it.ok)
        return (
            WriteResult(
                ok=failed == 0,
                validate_only=validate_only,
                platform_response=body,
                error=None if failed == 0 else f"partial failure: {failed} of {len(items)} failed",
            ),
            items,
        )

    def create_ad(self, draft: AdDraft, *, validate_only: bool) -> WriteResult:
        """RSA draft in ``draft.ad_group_external_id``, final URLs copied from the template ad
        (``extra.template_ad_id`` = ``<ad_group_id>~<ad_id>``). Always created PAUSED."""
        headlines = [str(h) for h in draft.extra.get("headlines") or [draft.headline] if h]
        descriptions = [str(d) for d in draft.extra.get("descriptions") or [draft.body] if d]
        problem = rsa_problem(headlines, descriptions)
        if problem is not None:
            return WriteResult(ok=False, validate_only=validate_only, error=problem)
        template = str(draft.extra.get("template_ad_id") or "")
        if not template:
            return WriteResult(
                ok=False, validate_only=validate_only, error="draft needs extra.template_ad_id"
            )
        t_ag, t_ad = self._split(template)
        rows = self._search(
            "write_template_ad",
            "SELECT ad_group.id, ad_group_ad.ad.id, ad_group_ad.ad.final_urls FROM ad_group_ad "
            f"WHERE ad_group.id = {t_ag} AND ad_group_ad.ad.id = {t_ad}",
            {"id": template},
        )
        urls = (
            ((rows[0].get("ad_group_ad") or {}).get("ad") or {}).get("final_urls") if rows else None
        )
        if not urls:
            return WriteResult(
                ok=False, validate_only=validate_only, error="template ad has no final_urls"
            )
        op = {
            "create": {
                "ad_group": self._rn("adGroups", str(int(draft.ad_group_external_id))),
                "status": "PAUSED",  # §2 non-goals, §15 step 5
                "ad": {
                    "final_urls": list(urls),
                    "responsive_search_ad": {
                        "headlines": [{"text": h} for h in headlines],
                        "descriptions": [{"text": d} for d in descriptions],
                    },
                },
            }
        }
        body = self._mutate("ad_group_ad", [op], validate_only=validate_only, idempotent=False)
        results = body.get("results") or []
        created = _last(results[0]["resource_name"]) if results and not validate_only else None
        return WriteResult(
            ok=True,
            validate_only=validate_only,
            platform_response=body,
            created_external_id=created,
        )


def rsa_problem(headlines: list[str], descriptions: list[str]) -> str | None:
    lo, hi, n = RSA_HEADLINES
    if not lo <= len(headlines) <= hi:
        return f"RSA needs {lo}-{hi} headlines, got {len(headlines)}"
    if any(len(h) > n for h in headlines):
        return f"RSA headline longer than {n} characters"
    lo, hi, n = RSA_DESCRIPTIONS
    if not lo <= len(descriptions) <= hi:
        return f"RSA needs {lo}-{hi} descriptions, got {len(descriptions)}"
    if any(len(d) > n for d in descriptions):
        return f"RSA description longer than {n} characters"
    return None
