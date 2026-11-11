# ruff: noqa: PLR0912, PLR0915  (fixture generator: long but linear)
"""Fixture-backed fake transports + a deterministic synthetic dataset (plan §22 "Contract").

Nothing here talks to a platform. Two fixture sources share one format:

- a directory of recorded responses: ``<dir>/meta.json`` and ``<dir>/google.json``
  (``FixtureSet.from_dir``), e.g. ``tests/fixtures/connectors/``;
- ``synthetic_fixtures(today, now_hour)`` — a generated, clearly labelled SYNTHETIC account pair
  (ids are all-zero test ids, names start with ``SYN``) used for end-to-end demos/tests.

Meta format = Graph API JSON (budgets in minor units as strings, ``spend`` decimal strings,
``actions`` lists). Google format = GoogleAdsRow as protobuf-JSON with snake_case names (int64
as strings, enums as names), exactly what ``SdkGoogleTransport`` returns.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from engine.credentials.base import GoogleCredentials, MetaCredentials
from engine.enums import Access

SYNTHETIC_META_ACCOUNT = "act_000000000000"
SYNTHETIC_GOOGLE_CUSTOMER = "0000000000"
SYNTHETIC_CONVERSION_ACTION = "offsite_conversion.fb_pixel_purchase"  # fixture-only value


def fixture_meta_credentials(ad_account_id: str = SYNTHETIC_META_ACCOUNT) -> MetaCredentials:
    """Placeholder read credentials for fake transports (never valid against a platform)."""
    return MetaCredentials(
        access=Access.READ,
        app_id="fixture",
        app_secret=SecretStr("fixture"),
        token=SecretStr("fixture"),
        ad_account_id=ad_account_id,
        api_version="fixture",
    )


def fixture_google_credentials(customer_id: str = SYNTHETIC_GOOGLE_CUSTOMER) -> GoogleCredentials:
    return GoogleCredentials(
        access=Access.READ,
        customer_id=customer_id,
        login_customer_id=None,
        cloud_project_id="fixture",
        oauth_client_id="fixture",
        oauth_client_secret=SecretStr("fixture"),
        refresh_token=SecretStr("fixture"),
    )


@dataclass
class FixtureSet:
    meta: dict[str, Any]
    google: dict[str, Any]
    conversion_action: str | None = None  # the fixture's own Meta action type
    synthetic: bool = True

    @classmethod
    def from_dir(cls, path: Path) -> FixtureSet:
        meta = json.loads((path / "meta.json").read_text())
        google = json.loads((path / "google.json").read_text())
        return cls(
            meta=meta,
            google=google,
            conversion_action=meta.get("_conversion_action"),
            synthetic=bool(meta.get("_synthetic", True)),
        )

    def write_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        meta = {**self.meta, "_conversion_action": self.conversion_action}
        meta["_synthetic"] = self.synthetic
        (path / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
        (path / "google.json").write_text(json.dumps(self.google, indent=1, sort_keys=True))


# ============================================================================ Meta fake


@dataclass
class FakeMetaTransport:
    store: dict[str, Any]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _act(self) -> str:
        return str(self.store["account"]["id"])

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, {k: v for k, v in params.items() if "token" not in k}))
        if path == "debug_token":
            return dict(self.store.get("debug_token", {"data": {"expires_at": 0}}))
        if path == self._act():
            return dict(self.store["account"])
        for edge in ("campaigns", "adsets", "ads"):
            for row in self.store.get(edge, []):
                if str(row["id"]) == path:
                    return dict(row)
        raise LookupError(f"fake meta: no node {path}")

    def get_edge(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((path, params))
        edge = path.rsplit("/", 1)[-1]
        if edge in ("campaigns", "adsets", "ads"):
            return [dict(r) for r in self.store.get(edge, [])]
        if edge == "activities":
            since = int(params.get("since", 0))
            return [
                dict(r)
                for r in self.store.get("activities", [])
                if _meta_ts(r["event_time"]) >= since
            ]
        if edge == "insights":
            return self._insights(params)
        raise LookupError(f"fake meta: unknown edge {path}")

    def _insights(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        level = params["level"]
        tr = json.loads(params["time_range"])
        since, until = dt.date.fromisoformat(tr["since"]), dt.date.fromisoformat(tr["until"])
        if params.get("breakdowns"):
            rows = self.store.get("insights_hourly", {}).get(level, [])
            return [
                dict(r) for r in rows if since <= dt.date.fromisoformat(r["date_start"]) <= until
            ]
        if params.get("time_increment") or since == until:
            rows = self.store.get("insights", {}).get(level, [])
            return [
                dict(r) for r in rows if since <= dt.date.fromisoformat(r["date_start"]) <= until
            ]
        rows = self.store.get("insights_window", {}).get(level, [])
        return [
            dict(r)
            for r in rows
            if r["date_start"] == since.isoformat() and r["date_stop"] == until.isoformat()
        ]


def _meta_ts(value: str) -> int:
    return int(dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp())


# ============================================================================ Google fake

_DATE_FILTERED = ("daily_", "hourly_", "search_terms")


@dataclass
class FakeGoogleTransport:
    store: dict[str, list[dict[str, Any]]]
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((name, query, params))
        if name.startswith("spend_today_"):
            source = "daily_" + name.removeprefix("spend_today_")
            return self._by_date(source, params)
        if name.startswith(_DATE_FILTERED):
            return self._by_date(name, params)
        if name.startswith("state_"):
            return self._state(name.removeprefix("state_"), str(params["id"]))
        return [json.loads(json.dumps(r)) for r in self.store.get(name, [])]

    def _by_date(self, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        start, end = params["start"], params["end"]
        return [
            json.loads(json.dumps(r))
            for r in self.store.get(name, [])
            if start <= dt.date.fromisoformat(r["segments"]["date"]) <= end
        ]

    def _state(self, kind: str, external_id: str) -> list[dict[str, Any]]:
        source = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}.get(
            kind, "keywords"
        )
        out = []
        for r in self.store.get(source, []):
            if (
                (kind == "campaign" and r["campaign"]["id"] == external_id)
                or (kind == "ad_group" and r["ad_group"]["id"] == external_id)
                or (
                    kind == "ad"
                    and f"{r['ad_group']['id']}~{r['ad_group_ad']['ad']['id']}" == (external_id)
                )
                or (
                    kind == "keyword"
                    and (
                        f"{r['ad_group']['id']}~{r['ad_group_criterion']['criterion_id']}"
                        == external_id
                    )
                )
            ):
                out.append(r)
        return out


# ============================================================================ synthetic data

_C = Decimal("0.01")


def _q(x: Decimal) -> Decimal:
    return x.quantize(_C, rounding=ROUND_HALF_UP)


# Intraday shape used to split a day into hours (sums to 1). Synthetic, not a platform fact.
_HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 7, 7, 6, 4, 3, 2]
_HW_TOTAL = sum(_HOUR_WEIGHTS)


@dataclass
class _Day:
    impressions: int
    clicks: int
    spend: Decimal
    conversions: int
    value: Decimal
    video: int

    def __add__(self, o: _Day) -> _Day:
        return _Day(
            self.impressions + o.impressions,
            self.clicks + o.clicks,
            self.spend + o.spend,
            self.conversions + o.conversions,
            self.value + o.value,
            self.video + o.video,
        )


_ZERO = _Day(0, 0, Decimal("0.00"), 0, Decimal("0.00"), 0)


def _rand_day(rng: random.Random, scale: float) -> _Day:
    imps = int(rng.uniform(800, 4000) * scale)
    clicks = int(imps * rng.uniform(0.008, 0.03))
    spend = _q(Decimal(str(round(imps * rng.uniform(0.12, 0.35), 2))))
    conv = int(clicks * rng.uniform(0.0, 0.08))
    value = _q(Decimal(conv) * Decimal(str(round(rng.uniform(900, 2200), 2))))
    return _Day(imps, clicks, spend, conv, value, int(imps * rng.uniform(0.0, 0.05)))


def _scale_partial(d: _Day, hours: int) -> _Day:
    """A day observed only until ``hours`` (exclusive) — consistent with the hourly split."""
    parts = _split_hours(d)
    out = _ZERO
    for h in range(hours):
        out = out + parts[h]
    return out


def _split_hours(d: _Day) -> list[_Day]:
    out: list[_Day] = []
    rem = d
    for h, w in enumerate(_HOUR_WEIGHTS):
        if h == 23:
            out.append(rem)
            break
        f = Decimal(w) / Decimal(_HW_TOTAL)
        part = _Day(
            int(d.impressions * f),
            int(d.clicks * f),
            _q(d.spend * f),
            int(d.conversions * f),
            _q(d.value * f),
            int(d.video * f),
        )
        out.append(part)
        rem = _Day(
            rem.impressions - part.impressions,
            rem.clicks - part.clicks,
            rem.spend - part.spend,
            rem.conversions - part.conversions,
            rem.value - part.value,
            rem.video - part.video,
        )
    return out


def synthetic_fixtures(
    today: dt.date = dt.date(2026, 11, 11),
    now_hour: int = 22,
    *,
    days: int = 95,
    hourly_days: int = 28,
    seed: int = 7,
) -> FixtureSet:
    """Deterministic SYNTHETIC Meta + Google account pair (fixed seed, §0 rule 8)."""
    rng = random.Random(seed)  # noqa: S311
    dates = [today - dt.timedelta(days=i) for i in range(days, -1, -1)]
    return FixtureSet(
        meta=_synthetic_meta(rng, dates, today, now_hour, hourly_days),
        google=_synthetic_google(rng, dates, today, now_hour, hourly_days),
        conversion_action=SYNTHETIC_CONVERSION_ACTION,
        synthetic=True,
    )


def _observed(
    rng: random.Random, dates: list[dt.date], today: dt.date, now_hour: int, scale: float
) -> dict[dt.date, _Day]:
    out: dict[dt.date, _Day] = {}
    for d in dates:
        day = _rand_day(rng, scale)
        out[d] = _scale_partial(day, now_hour + 1) if d == today else day
    return out


# ---------------------------------------------------------------------------- Meta


def _meta_actions(day: _Day) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    actions = [
        {"action_type": "link_click", "value": str(day.clicks)},
        {"action_type": SYNTHETIC_CONVERSION_ACTION, "value": str(day.conversions)},
        # an overlapping action type that must NOT be summed in (§8.4)
        {"action_type": "omni_purchase", "value": str(day.conversions)},
        {"action_type": "video_view", "value": str(day.video)},
    ]
    values = [
        {"action_type": SYNTHETIC_CONVERSION_ACTION, "value": str(day.value)},
        {"action_type": "omni_purchase", "value": str(day.value)},
    ]
    return actions, values


def _meta_row(ids: dict[str, str], d: dt.date, day: _Day, **extra: str) -> dict[str, Any]:
    actions, values = _meta_actions(day)
    return {
        **ids,
        "date_start": d.isoformat(),
        "date_stop": d.isoformat(),
        "impressions": str(day.impressions),
        "inline_link_clicks": str(day.clicks),
        "spend": str(day.spend),
        "actions": actions,
        "action_values": values,
        **extra,
    }


def _synthetic_meta(
    rng: random.Random, dates: list[dt.date], today: dt.date, now_hour: int, hourly_days: int
) -> dict[str, Any]:
    act = SYNTHETIC_META_ACCOUNT
    acct_id = act.removeprefix("act_")
    campaigns = [
        {
            "id": "900001",
            "name": "SYN Prospecting (CBO)",
            "objective": "OUTCOME_SALES",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "daily_budget": "300000",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "buying_type": "AUCTION",
        },
        {
            "id": "900002",
            "name": "SYN Retargeting (ABO)",
            "objective": "OUTCOME_SALES",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "buying_type": "AUCTION",
        },
        {
            "id": "900003",
            "name": "SYN Advantage+ Shopping",
            "objective": "OUTCOME_SALES",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "daily_budget": "200000",
            "smart_promotion_type": "AUTOMATED_SHOPPING_ADS",
            "buying_type": "AUCTION",
        },
        {
            "id": "900004",
            "name": "SYN Old Awareness",
            "objective": "OUTCOME_AWARENESS",
            "status": "PAUSED",
            "effective_status": "PAUSED",
            "daily_budget": "50000",
            "buying_type": "AUCTION",
        },
    ]
    adsets = [
        ("910001", "900001", "ACTIVE", None, "SUCCESS"),
        ("910002", "900001", "ACTIVE", None, "LEARNING"),
        ("910003", "900002", "ACTIVE", "100000", "FAIL"),
        ("910004", "900002", "PAUSED", "50000", None),
        ("910005", "900003", "ACTIVE", None, "SUCCESS"),
        ("910006", "900004", "PAUSED", None, "SUCCESS"),
    ]
    adset_rows: list[dict[str, Any]] = []
    for sid, cid, status, budget, learning in adsets:
        row: dict[str, Any] = {
            "id": sid,
            "name": f"SYN ad set {sid}",
            "campaign_id": cid,
            "status": status,
            "effective_status": status,
            "optimization_goal": "OFFSITE_CONVERSIONS",
        }
        if budget:
            row["daily_budget"] = budget
        if learning:
            row["learning_stage_info"] = {"status": learning}
        adset_rows.append(row)
    ad_rows: list[dict[str, Any]] = []
    for sid, _cid, s_status, _b, _l in adsets:
        for k in (1, 2):
            aid = f"92{sid[2:]}{k}"
            ad_rows.append(
                {
                    "id": aid,
                    "name": f"SYN ad {aid}",
                    "adset_id": sid,
                    "status": s_status if k == 1 else "PAUSED" if sid == "910004" else "ACTIVE",
                    "effective_status": s_status,
                    "created_time": "2026-07-01T10:00:00+0530",
                    "creative": {
                        "id": f"93{sid[2:]}{k}",
                        "title": f"SYN headline {aid}",
                        "body": f"SYN body {aid}",
                        "call_to_action_type": "SHOP_NOW",
                        "object_type": "SHARE",
                    },
                }
            )

    ad_parent = {a["id"]: a["adset_id"] for a in ad_rows}
    set_parent = {s["id"]: s["campaign_id"] for s in adset_rows}
    active_scale = {a["id"]: (1.0 if a["status"] == "ACTIVE" else 0.05) for a in ad_rows}
    per_ad = {a: _observed(rng, dates, today, now_hour, active_scale[a]) for a in ad_parent}

    ins: dict[str, list[dict[str, Any]]] = {"account": [], "campaign": [], "adset": [], "ad": []}
    hourly: dict[str, list[dict[str, Any]]] = {"account": [], "campaign": []}
    for d in dates:
        by_set: dict[str, _Day] = {}
        by_camp: dict[str, _Day] = {}
        for aid, series in per_ad.items():
            day = series[d]
            sid = ad_parent[aid]
            cid = set_parent[sid]
            by_set[sid] = by_set.get(sid, _ZERO) + day
            by_camp[cid] = by_camp.get(cid, _ZERO) + day
            ins["ad"].append(
                _meta_row(
                    {"account_id": acct_id, "campaign_id": cid, "adset_id": sid, "ad_id": aid},
                    d,
                    day,
                )
            )
        for sid, day in by_set.items():
            ids = {"account_id": acct_id, "campaign_id": set_parent[sid], "adset_id": sid}
            ins["adset"].append(_meta_row(ids, d, day))
        total = _ZERO
        for cid, day in by_camp.items():
            total = total + day
            ins["campaign"].append(_meta_row({"account_id": acct_id, "campaign_id": cid}, d, day))
        ins["account"].append(_meta_row({"account_id": acct_id}, d, total))

        if (today - d).days < hourly_days:
            acct_hours = [_ZERO] * 24
            for cid, day in by_camp.items():
                # hourly view of the full-day totals (partial today = first now_hour+1 buckets)
                parts = _hour_parts_for_observed(day, d, today, now_hour)
                for h, part in enumerate(parts):
                    acct_hours[h] = acct_hours[h] + part
                    hourly["campaign"].append(
                        _meta_row(
                            {"account_id": acct_id, "campaign_id": cid},
                            d,
                            part,
                            hourly_stats_aggregated_by_advertiser_time_zone=_bucket(h),
                        )
                    )
            n = now_hour + 1 if d == today else 24
            for h in range(n):
                hourly["account"].append(
                    _meta_row(
                        {"account_id": acct_id},
                        d,
                        acct_hours[h],
                        hourly_stats_aggregated_by_advertiser_time_zone=_bucket(h),
                    )
                )

    window: dict[str, list[dict[str, Any]]] = {"campaign": [], "adset": [], "ad": []}
    for back in range(1, 15):
        as_of = today - dt.timedelta(days=back)
        since = as_of - dt.timedelta(days=6)
        for level, key, rows in (
            ("ad", "ad_id", ins["ad"]),
            ("adset", "adset_id", ins["adset"]),
            ("campaign", "campaign_id", ins["campaign"]),
        ):
            imps: dict[str, int] = {}
            for r in rows:
                if since <= dt.date.fromisoformat(r["date_start"]) <= as_of:
                    imps[r[key]] = imps.get(r[key], 0) + int(r["impressions"])
            for eid, n_imps in sorted(imps.items()):
                freq = round(rng.uniform(1.2, 4.2), 4)
                window[level].append(
                    {
                        "account_id": acct_id,
                        key: eid,
                        "date_start": since.isoformat(),
                        "date_stop": as_of.isoformat(),
                        "reach": str(int(n_imps / freq)),
                        "frequency": str(freq),
                    }
                )

    activities = [
        {
            "event_type": "update_ad_set_budget",
            "event_time": f"{(today - dt.timedelta(days=2)).isoformat()}T11:15:00+0530",
            "actor_name": "SYN Human",
            "object_id": "910003",
            "object_type": "ADSET",
            "application_name": "Ads Manager",
            "extra_data": json.dumps({"old_value": 90000, "new_value": 100000}),
        }
    ]
    return {
        "account": {
            "id": act,
            "name": "SYN Meta account (synthetic)",
            "account_status": 1,
            "currency": "INR",
            "timezone_name": "Asia/Kolkata",
        },
        "campaigns": campaigns,
        "adsets": adset_rows,
        "ads": ad_rows,
        "insights": ins,
        "insights_hourly": hourly,
        "insights_window": window,
        "activities": activities,
    }


def _hour_parts_for_observed(day: _Day, d: dt.date, today: dt.date, now_hour: int) -> list[_Day]:
    """Split an observed day so that the hourly rows sum exactly to the daily row."""
    if d != today:
        return _split_hours(day)
    # today's observed total is already partial; spread it over the elapsed hours
    weights = _HOUR_WEIGHTS[: now_hour + 1]
    total_w = sum(weights)
    out: list[_Day] = []
    rem = day
    for h, w in enumerate(weights):
        if h == len(weights) - 1:
            out.append(rem)
            break
        f = Decimal(w) / Decimal(total_w)
        part = _Day(
            int(day.impressions * f),
            int(day.clicks * f),
            _q(day.spend * f),
            int(day.conversions * f),
            _q(day.value * f),
            int(day.video * f),
        )
        out.append(part)
        rem = _Day(
            rem.impressions - part.impressions,
            rem.clicks - part.clicks,
            rem.spend - part.spend,
            rem.conversions - part.conversions,
            rem.value - part.value,
            rem.video - part.video,
        )
    return out


def _bucket(h: int) -> str:
    return f"{h:02d}:00:00 - {h:02d}:59:59"


# ---------------------------------------------------------------------------- Google


def _g_metrics(day: _Day, video: bool = False) -> dict[str, Any]:
    m: dict[str, Any] = {
        "impressions": str(day.impressions),
        "clicks": str(day.clicks),
        "cost_micros": str(int(day.spend * 1_000_000)),
        "conversions": float(day.conversions),
        "conversions_value": float(day.value),
    }
    if video:
        m["video_trueview_views"] = str(day.video)
    return m


def _synthetic_google(
    rng: random.Random, dates: list[dt.date], today: dt.date, now_hour: int, hourly_days: int
) -> dict[str, Any]:
    cust = SYNTHETIC_GOOGLE_CUSTOMER
    camps = [
        (
            "800001",
            "SYN Brand Search",
            "SEARCH",
            "ENABLED",
            "ENABLED",
            "700001",
            "2000000000",
            False,
        ),
        (
            "800002",
            "SYN Generic Search",
            "SEARCH",
            "ENABLED",
            "LEARNING_BUDGET_CHANGE",
            "700002",
            "1500000000",
            True,
        ),
        (
            "800003",
            "SYN Performance Max",
            "PERFORMANCE_MAX",
            "ENABLED",
            "ENABLED",
            "700003",
            "2500000000",
            False,
        ),
        ("800004", "SYN Old Display", "DISPLAY", "PAUSED", "PAUSED", "700004", "300000000", False),
    ]
    campaigns = [
        {
            "campaign": {
                "resource_name": f"customers/{cust}/campaigns/{cid}",
                "id": cid,
                "name": name,
                "status": status,
                "advertising_channel_type": channel,
                "advertising_channel_sub_type": "UNSPECIFIED",
                "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
                "bidding_strategy_system_status": bss,
                "campaign_budget": f"customers/{cust}/campaignBudgets/{bid}",
            },
            "campaign_budget": {
                "resource_name": f"customers/{cust}/campaignBudgets/{bid}",
                "id": bid,
                "amount_micros": micros,
                "total_amount_micros": "0",
                "explicitly_shared": shared,
                "period": "DAILY",
            },
        }
        for cid, name, channel, status, bss, bid, micros, shared in camps
    ]
    groups = [
        ("810001", "800001", "ENABLED"),
        ("810002", "800001", "PAUSED"),
        ("810003", "800002", "ENABLED"),
        ("810004", "800004", "PAUSED"),
    ]
    ad_groups = [
        {
            "ad_group": {
                "id": gid,
                "name": f"SYN ad group {gid}",
                "status": st,
                "type": "SEARCH_STANDARD",
            },
            "campaign": {"id": cid},
        }
        for gid, cid, st in groups
    ]
    ads: list[dict[str, Any]] = []
    for gid, _cid, st in groups:
        for k in (1, 2):
            ad_id = f"82{gid[2:]}{k}"
            ads.append(
                {
                    "ad_group": {"id": gid},
                    "ad_group_ad": {
                        "status": st if k == 1 else "ENABLED",
                        "ad": {
                            "id": ad_id,
                            "name": "",
                            "type": "RESPONSIVE_SEARCH_AD",
                            "responsive_search_ad": {
                                "headlines": [{"text": f"SYN headline {ad_id}"}],
                                "descriptions": [{"text": f"SYN description {ad_id}"}],
                            },
                        },
                    },
                }
            )
    keywords: list[dict[str, Any]] = []
    for gid, _cid, _st in groups[:3]:
        for k, (text, mt) in enumerate(
            [
                ("syn shoes", "EXACT"),
                ("syn running shoes", "PHRASE"),
                ("buy syn", "BROAD"),
                ("free", "BROAD"),
            ],
            start=1,
        ):
            keywords.append(
                {
                    "ad_group": {"id": gid},
                    "ad_group_criterion": {
                        "criterion_id": f"83{gid[2:]}{k}",
                        "keyword": {"text": text, "match_type": mt},
                        "status": "ENABLED",
                        "negative": k == 4,
                    },
                }
            )
    group_camp = {g: c for g, c, _ in groups}
    ad_scale = {
        f"{a['ad_group']['id']}~{a['ad_group_ad']['ad']['id']}": (
            1.0 if a["ad_group_ad"]["status"] == "ENABLED" else 0.05
        )
        for a in ads
    }
    per_ad = {k: _observed(rng, dates, today, now_hour, s) for k, s in ad_scale.items()}
    pmax = _observed(rng, dates, today, now_hour, 1.5)

    store: dict[str, list[dict[str, Any]]] = {
        k: []
        for k in (
            "daily_account",
            "daily_campaign",
            "daily_ad_group",
            "daily_ad",
            "daily_keyword",
            "hourly_account",
            "hourly_campaign",
            "search_terms",
        )
    }
    for d in dates:
        seg = {"date": d.isoformat()}
        by_group: dict[str, _Day] = {}
        by_camp: dict[str, _Day] = {"800003": pmax[d]}
        for key, series in per_ad.items():
            gid, ad_id = key.split("~")
            day = series[d]
            by_group[gid] = by_group.get(gid, _ZERO) + day
            cid = group_camp[gid]
            by_camp[cid] = by_camp.get(cid, _ZERO) + day
            store["daily_ad"].append(
                {
                    "ad_group": {"id": gid},
                    "ad_group_ad": {"ad": {"id": ad_id}},
                    "segments": seg,
                    "metrics": _g_metrics(day, video=True),
                }
            )
        for gid, day in by_group.items():
            store["daily_ad_group"].append(
                {"ad_group": {"id": gid}, "segments": seg, "metrics": _g_metrics(day, True)}
            )
            kws = [
                k
                for k in keywords
                if k["ad_group"]["id"] == gid and not k["ad_group_criterion"]["negative"]
            ]
            rem = day
            for i, kw in enumerate(kws):
                if i == len(kws) - 1:
                    part = rem
                else:
                    f = Decimal(1) / Decimal(len(kws))
                    part = _Day(
                        int(day.impressions * f),
                        int(day.clicks * f),
                        _q(day.spend * f),
                        int(day.conversions * f),
                        _q(day.value * f),
                        0,
                    )
                    rem = _Day(
                        rem.impressions - part.impressions,
                        rem.clicks - part.clicks,
                        rem.spend - part.spend,
                        rem.conversions - part.conversions,
                        rem.value - part.value,
                        0,
                    )
                store["daily_keyword"].append(
                    {
                        "ad_group": {"id": gid},
                        "ad_group_criterion": {
                            "criterion_id": kw["ad_group_criterion"]["criterion_id"]
                        },
                        "segments": seg,
                        "metrics": _g_metrics(part),
                    }
                )
            if (today - d).days < 30 and day.clicks:
                for term in ("syn shoes sale", "cheap syn", "syn store near me"):
                    st_day = _rand_day(rng, 0.1)
                    store["search_terms"].append(
                        {
                            "ad_group": {"id": gid},
                            "search_term_view": {"search_term": term},
                            "segments": seg,
                            "metrics": _g_metrics(st_day),
                        }
                    )
        total = _ZERO
        acct_hours = [_ZERO] * 24
        for cid, day in sorted(by_camp.items()):
            total = total + day
            store["daily_campaign"].append(
                {"campaign": {"id": cid}, "segments": seg, "metrics": _g_metrics(day, True)}
            )
            if (today - d).days < hourly_days:
                for h, part in enumerate(_hour_parts_for_observed(day, d, today, now_hour)):
                    acct_hours[h] = acct_hours[h] + part
                    store["hourly_campaign"].append(
                        {
                            "campaign": {"id": cid},
                            "segments": {**seg, "hour": h},
                            "metrics": _g_metrics(part),
                        }
                    )
        store["daily_account"].append(
            {"customer": {"id": cust}, "segments": seg, "metrics": _g_metrics(total)}
        )
        if (today - d).days < hourly_days:
            n = now_hour + 1 if d == today else 24
            for h in range(n):
                store["hourly_account"].append(
                    {
                        "customer": {"id": cust},
                        "segments": {**seg, "hour": h},
                        "metrics": _g_metrics(acct_hours[h]),
                    }
                )

    changed = (today - dt.timedelta(days=1)).isoformat()
    store["change_events"] = [
        {
            "change_event": {
                "change_date_time": f"{changed} 09:30:00.000000",
                "change_resource_type": "CAMPAIGN_BUDGET",
                "change_resource_name": f"customers/{cust}/campaignBudgets/700001",
                "client_type": "GOOGLE_ADS_WEB_CLIENT",
                "user_email": "syn-human@example.invalid",
                "changed_fields": "amount_micros",
                "campaign": f"customers/{cust}/campaigns/800001",
                "old_resource": {"campaign_budget": {"amount_micros": "1800000000"}},
                "new_resource": {"campaign_budget": {"amount_micros": "2000000000"}},
                "resource_change_operation": "UPDATE",
            }
        }
    ]
    store["customer"] = [
        {
            "customer": {
                "id": cust,
                "descriptive_name": "SYN Google account (synthetic)",
                "currency_code": "INR",
                "time_zone": "Asia/Kolkata",
                "status": "ENABLED",
            }
        }
    ]
    store["campaigns"] = campaigns
    store["ad_groups"] = ad_groups
    store["ads"] = ads
    store["keywords"] = keywords
    return store
