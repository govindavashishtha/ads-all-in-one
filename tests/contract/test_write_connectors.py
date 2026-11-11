"""WriteConnector contract tests with mocked SDK transports (plan §15, §20, §22 "Contract").

No network, no credentials: the transports below record the exact requests the connectors
would send and replay canned responses / SDK-shaped errors.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from engine.connectors.base import AdDraft, EntityStatus
from engine.connectors.fakes_write import fake_credentials
from engine.connectors.google_write import GoogleWriteConnector, map_google_error, rsa_problem
from engine.connectors.meta_write import MetaWriteConnector, map_meta_error
from engine.connectors.resilience import RetryPolicy, TokenBucket
from engine.connectors.write_common import (
    PlatformWriteError,
    WriteCredentialRequiredError,
    WriteErrorKind,
)
from engine.enums import Access, EntityType, Platform


def _fast() -> dict[str, Any]:
    return {
        "retry": RetryPolicy(sleep=lambda _s: None),
        "bucket": TokenBucket(rate=1e9, capacity=1e9),
    }


class FbError(Exception):
    """Shape of facebook_business.exceptions.FacebookRequestError (methods, not attrs)."""

    def __init__(self, code: int | None, *, transient: bool = False, http: int = 400) -> None:
        super().__init__(f"fb error {code}")
        self._code, self._transient, self._http = code, transient, http

    def api_error_code(self) -> int | None:
        return self._code

    def api_error_message(self) -> str:
        return f"graph error {self._code}"

    def api_transient_error(self) -> bool:
        return self._transient

    def http_status(self) -> int:
        return self._http


class MetaTransport:
    def __init__(self, scopes: list[str] | None = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.fail: list[BaseException] = []  # raised (in order) by the next posts
        self.scopes = scopes if scopes is not None else ["ads_read", "ads_management"]

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, params))
        if self.fail:
            raise self.fail.pop(0)
        if path.endswith("/adcreatives"):
            return {"id": "c1"}
        if path.endswith("/ads"):
            return {"id": "ad9"}
        return {"success": True}

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.gets.append((path, params))
        if path == "debug_token":
            return {"data": {"is_valid": True, "scopes": self.scopes}}
        if path.startswith("act_"):
            return {"currency": "INR", "timezone_name": "Asia/Kolkata"}
        return {
            "id": path,
            "creative": {
                "id": "c0",
                "object_story_spec": {
                    "page_id": "p1",
                    "link_data": {"link": "https://example.test", "message": "old", "name": "x"},
                },
            },
        }


def meta(t: MetaTransport) -> MetaWriteConnector:
    return MetaWriteConnector(fake_credentials(Platform.META), transport=t, **_fast())


# ----------------------------------------------------------------------------- credentials


@pytest.mark.parametrize("cls", [MetaWriteConnector, GoogleWriteConnector])
def test_read_credentials_are_refused_for_writes(cls: Any) -> None:
    platform = Platform.META if cls is MetaWriteConnector else Platform.GOOGLE
    with pytest.raises(WriteCredentialRequiredError):
        cls(fake_credentials(platform, Access.READ), transport=object())


def test_meta_token_without_ads_management_is_refused() -> None:
    conn = meta(MetaTransport(scopes=["ads_read"]))
    with pytest.raises(PlatformWriteError) as e:
        conn.verify_write_access()
    assert e.value.kind is WriteErrorKind.PERMISSION
    meta(MetaTransport()).verify_write_access()  # write scope present → ok


# ----------------------------------------------------------------------------- Meta


def test_meta_status_and_budget_payloads() -> None:
    t = MetaTransport()
    c = meta(t)
    assert c.set_status(EntityType.CAMPAIGN, "123", EntityStatus.PAUSED, validate_only=True).ok
    assert t.posts[-1] == ("123", {"status": "PAUSED", "execution_options": ["validate_only"]})
    c.set_status(EntityType.AD, "9", EntityStatus.ACTIVE, validate_only=False)
    assert t.posts[-1] == ("9", {"status": "ACTIVE"})
    r = c.set_daily_budget(EntityType.AD_GROUP, "55", Decimal("812.50"), validate_only=False)
    assert r.ok and t.posts[-1] == ("55", {"daily_budget": "81250"})  # INR minor units (V-3)
    bad = c.set_daily_budget(EntityType.CAMPAIGN, "1", Decimal("-1"), validate_only=True)
    assert not bad.ok and "> 0" in (bad.error or "")
    with pytest.raises(PlatformWriteError) as e:
        c.set_status(EntityType.CAMPAIGN, "1", EntityStatus.REMOVED, validate_only=False)
    assert e.value.kind is WriteErrorKind.UNSUPPORTED  # never deletes
    with pytest.raises(PlatformWriteError):
        c.add_negative_keyword("1", "x", "exact", validate_only=False)


def test_meta_new_ad_is_created_paused_from_template() -> None:
    t = MetaTransport()
    r = meta(t).create_ad(
        AdDraft(
            Platform.META, "adset7", "New headline", "New body", None, {"template_ad_id": "ad1"}
        ),
        validate_only=False,
    )
    assert r.ok and r.created_external_id == "ad9"
    creative_path, creative = t.posts[0]
    assert creative_path == "act_000000000000/adcreatives"
    link = creative["object_story_spec"]["link_data"]
    assert link["name"] == "New headline" and link["message"] == "New body"
    assert link["link"] == "https://example.test"  # rest of the story spec kept
    ad_path, ad = t.posts[1]
    assert ad_path == "act_000000000000/ads" and ad["status"] == "PAUSED"
    assert ad["adset_id"] == "adset7" and ad["creative"] == {"creative_id": "c1"}


def test_meta_new_ad_partial_failure_reports_orphan_creative() -> None:
    class Tr(MetaTransport):
        def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
            if path.endswith("/ads"):
                raise FbError(100)
            return super().post(path, params)

    with pytest.raises(PlatformWriteError) as e:
        meta(Tr()).create_ad(
            AdDraft(Platform.META, "s", "h", "b", None, {"template_ad_id": "a"}),
            validate_only=False,
        )
    assert e.value.kind is WriteErrorKind.PARTIAL and e.value.detail["orphan_creative_id"] == "c1"


def test_meta_rate_limit_is_retried_then_succeeds() -> None:
    t = MetaTransport()
    t.fail = [FbError(17), FbError(613)]
    assert meta(t).set_status(EntityType.CAMPAIGN, "1", EntityStatus.PAUSED, validate_only=False)
    assert len(t.posts) == 3


def test_meta_transient_create_is_not_retried_and_outcome_unknown() -> None:
    class Tr(MetaTransport):
        def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
            self.posts.append((path, params))
            raise FbError(2, transient=True, http=500)

    t = Tr()
    with pytest.raises(PlatformWriteError) as e:
        meta(t).create_ad(
            AdDraft(Platform.META, "s", "h", "b", None, {"template_ad_id": "a"}),
            validate_only=False,
        )
    assert e.value.applied is None and len(t.posts) == 1  # create: no blind retry
    t2 = Tr()
    with pytest.raises(PlatformWriteError):
        meta(t2).set_status(EntityType.CAMPAIGN, "1", EntityStatus.PAUSED, validate_only=False)
    assert len(t2.posts) == 6  # absolute set: retried (max 5 retries, §9.3)


@pytest.mark.parametrize(
    ("code", "kind", "applied"),
    [
        (17, WriteErrorKind.RATE_LIMITED, False),
        (190, WriteErrorKind.AUTH, False),
        (200, WriteErrorKind.PERMISSION, False),
        (803, WriteErrorKind.NOT_FOUND, False),
        (100, WriteErrorKind.INVALID, False),
        (2, WriteErrorKind.TRANSIENT, None),
    ],
)
def test_meta_error_mapping(code: int, kind: WriteErrorKind, applied: bool | None) -> None:
    err = map_meta_error(FbError(code, http=500 if code == 2 else 400), idempotent=True)
    assert err.kind is kind and err.applied is applied


# ----------------------------------------------------------------------------- Google


class GStatus:
    def __init__(self, name: str) -> None:
        self.name = name


class GoogleAdsError(Exception):
    """Shape of GoogleAdsException: ``.error.code().name`` and ``.failure.errors[]``."""

    def __init__(self, status: str, codes: list[str]) -> None:
        super().__init__(f"{status} {codes}")
        st = GStatus(status)
        self.error = type("E", (), {"code": lambda _self: st})()
        self.failure = type(
            "F", (), {"errors": [type("Er", (), {"error_code": c})() for c in codes]}
        )()


class GTransport:
    def __init__(self) -> None:
        self.mutates: list[dict[str, Any]] = []
        self.fail: list[BaseException] = []
        self.partial_errors: list[dict[str, Any]] = []
        self.shared_budget = False

    def mutate(
        self,
        service: str,
        operations: list[dict[str, Any]],
        *,
        validate_only: bool,
        partial_failure: bool,
    ) -> dict[str, Any]:
        self.mutates.append(
            {
                "service": service,
                "operations": operations,
                "validate_only": validate_only,
                "partial_failure": partial_failure,
            }
        )
        if self.fail:
            raise self.fail.pop(0)
        results = []
        for i, op in enumerate(operations):
            rn: Any
            if "create" in op:
                rn = {
                    "ad_group_criterion": f"customers/0000000000/adGroupCriteria/7~{500 + i}",
                    "ad_group_ad": "customers/0000000000/adGroupAds/7~888",
                    "shared_criterion": f"customers/0000000000/sharedCriteria/3~{600 + i}",
                }[service]
            else:
                rn = op.get("update", {}).get("resource_name") or op.get("remove")
            results.append({"resource_name": rn})
        return {"results": results, "partial_failure_errors": self.partial_errors}

    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "write_budget_lookup":
            return [
                {
                    "campaign": {
                        "id": "11",
                        "campaign_budget": "customers/0000000000/campaignBudgets/99",
                    },
                    "campaign_budget": {"explicitly_shared": self.shared_budget, "period": "DAILY"},
                }
            ]
        if name == "write_template_ad":
            return [{"ad_group_ad": {"ad": {"final_urls": ["https://example.test/a"]}}}]
        return []


def google(t: GTransport) -> GoogleWriteConnector:
    return GoogleWriteConnector(fake_credentials(Platform.GOOGLE), transport=t, **_fast())


def test_google_status_budget_payloads() -> None:
    t = GTransport()
    c = google(t)
    c.set_status(EntityType.CAMPAIGN, "11", EntityStatus.PAUSED, validate_only=True)
    m = t.mutates[-1]
    assert m["service"] == "campaign" and m["validate_only"] is True
    assert m["operations"] == [
        {
            "update": {"resource_name": "customers/0000000000/campaigns/11", "status": "PAUSED"},
            "update_mask": "status",
        }
    ]
    c.set_status(EntityType.AD, "7~42", EntityStatus.ACTIVE, validate_only=False)
    assert t.mutates[-1]["operations"][0]["update"] == {
        "resource_name": "customers/0000000000/adGroupAds/7~42",
        "status": "ENABLED",
    }
    r = c.set_daily_budget(EntityType.CAMPAIGN, "11", Decimal("1234.56"), validate_only=False)
    op = t.mutates[-1]["operations"][0]
    assert op["update"]["amount_micros"] == "1234560000" and op["update_mask"] == "amount_micros"
    assert r.platform_response["budget_resource"].endswith("campaignBudgets/99")


def test_google_shared_budget_is_refused() -> None:
    t = GTransport()
    t.shared_budget = True
    with pytest.raises(PlatformWriteError) as e:
        google(t).set_daily_budget(EntityType.CAMPAIGN, "11", Decimal("10"), validate_only=False)
    assert e.value.kind is WriteErrorKind.UNSUPPORTED and not t.mutates


def test_google_negative_keyword_add_and_remove() -> None:
    t = GTransport()
    c = google(t)
    r = c.add_negative_keyword("7", " cheap shoes ", "exact", validate_only=False)
    assert r.created_external_id == "7~500"
    created = t.mutates[-1]["operations"][0]["create"]
    assert created == {
        "ad_group": "customers/0000000000/adGroups/7",
        "negative": True,
        "status": "ENABLED",
        "keyword": {"text": "cheap shoes", "match_type": "EXACT"},
    }
    c.remove_negative_keyword("7", "7~500", validate_only=False)
    assert t.mutates[-1]["operations"] == [{"remove": "customers/0000000000/adGroupCriteria/7~500"}]


def test_google_shared_set_negatives_partial_failure() -> None:
    t = GTransport()
    t.partial_errors = [{"index": 1, "message": "policy violation", "error_code": "policy"}]
    result, items = google(t).add_negatives_to_shared_set(
        "3", ["a", "b", "c"], "phrase", validate_only=False
    )
    assert t.mutates[-1]["partial_failure"] is True
    assert [i.ok for i in items] == [True, False, True]
    assert items[0].created_external_id == "3~600" and items[1].error == "policy violation"
    assert not result.ok and result.error == "partial failure: 1 of 3 failed"


def test_google_rsa_draft_created_paused_with_template_urls() -> None:
    t = GTransport()
    draft = AdDraft(
        Platform.GOOGLE,
        "7",
        None,
        None,
        None,
        {"template_ad_id": "7~1", "headlines": ["H1", "H2", "H3"], "descriptions": ["D1", "D2"]},
    )
    r = google(t).create_ad(draft, validate_only=False)
    assert r.created_external_id == "7~888"
    create = t.mutates[-1]["operations"][0]["create"]
    assert create["status"] == "PAUSED"
    assert create["ad"]["final_urls"] == ["https://example.test/a"]
    assert [h["text"] for h in create["ad"]["responsive_search_ad"]["headlines"]] == [
        "H1",
        "H2",
        "H3",
    ]
    too_few = AdDraft(Platform.GOOGLE, "7", "only", "one", None, {"template_ad_id": "7~1"})
    bad = google(GTransport()).create_ad(too_few, validate_only=True)
    assert not bad.ok and "headlines" in (bad.error or "")
    assert rsa_problem(["x" * 31, "a", "b"], ["d", "e"]) is not None


def test_google_quota_retried_and_permission_not() -> None:
    t = GTransport()
    t.fail = [GoogleAdsError("RESOURCE_EXHAUSTED", ["quota_error"])]
    google(t).set_status(EntityType.CAMPAIGN, "11", EntityStatus.PAUSED, validate_only=False)
    assert len(t.mutates) == 2
    t2 = GTransport()
    t2.fail = [GoogleAdsError("PERMISSION_DENIED", ["authorization_error"])]
    with pytest.raises(PlatformWriteError) as e:
        google(t2).set_status(EntityType.CAMPAIGN, "11", EntityStatus.PAUSED, validate_only=False)
    assert e.value.kind is WriteErrorKind.PERMISSION and len(t2.mutates) == 1


def test_google_unavailable_on_create_is_unknown_outcome() -> None:
    t = GTransport()
    t.fail = [GoogleAdsError("UNAVAILABLE", [])]
    with pytest.raises(PlatformWriteError) as e:
        google(t).add_negative_keyword("7", "x", "exact", validate_only=False)
    assert e.value.applied is None and len(t.mutates) == 1


@pytest.mark.parametrize(
    ("status", "codes", "kind"),
    [
        ("INVALID_ARGUMENT", ["field_error"], WriteErrorKind.INVALID),
        ("NOT_FOUND", ["resource_not_found_error"], WriteErrorKind.NOT_FOUND),
        ("UNAUTHENTICATED", ["authentication_error"], WriteErrorKind.AUTH),
        ("INTERNAL", ["internal_error"], WriteErrorKind.TRANSIENT),
    ],
)
def test_google_error_mapping(status: str, codes: list[str], kind: WriteErrorKind) -> None:
    assert map_google_error(GoogleAdsError(status, codes), idempotent=False).kind is kind
