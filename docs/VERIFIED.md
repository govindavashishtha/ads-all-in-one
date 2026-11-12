# VERIFIED — Phase 0 results for every `VERIFY-P0` item

Rule (BUILD_PLAN §0.2): confirm each item against the **official** docs, record doc URL, date
checked, exact field/parameter name and SDK version. If the docs disagree with the plan, the
docs win: change the **connector only**, record the difference here, and don't touch rules,
ranking or guardrail code.

**Status: nothing below is verified yet.** Tick an item only after it has been checked.

| # | Plan § | Item to confirm | Status | Doc URL | Checked (date, by) | Exact name / value found | SDK / API version | Difference from plan |
|---|---|---|---|---|---|---|---|---|
| V-1 | §4.3 | Google Ads API: **developer tokens retired 9 Sept 2026** and access levels (Test / Explorer / Basic / Standard) now belong to the Google Cloud project — the plan takes this from the AdLoop README, not Google docs. Also current daily operation limits (Explorer 2,880, Basic 15,000) and how to apply for Basic access. https://developers.google.com/google-ads/api/docs/api-policy/access-levels | ☐ open | | | | | |
| V-2 | §4.3 | Meta: do the app and the ad account belong to the same Business Manager? If not, advanced access (app review) is needed; start it on day 1. Ties to INPUT I-9. | ☐ open | | | | | |
| V-3 | §8.1 | Meta budget fields (`daily_budget`, `lifetime_budget`) are integers in the currency's **minor unit**; confirm the offset for the account currency (`META_CURRENCY_OFFSETS` in `engine/connectors/money.py`, currently INR=100, USD=100, unverified). | ☐ open | | | | | |
| V-4 | §8.1 | Meta insights `spend` is a **decimal string in major units**. | ☐ open | | | | | |
| V-5 | §8.1 | Google Ads amounts (`amount_micros`, `cost_micros`) are **micros** (÷ 1,000,000). | ☐ open | | | | | |
| V-6 | §8.2 | Meta ad set field `learning_stage_info.status` and its values (`LEARNING`, `FAIL` = learning limited, `SUCCESS`). | ☐ open | | | | | |
| V-7 | §8.2 | Google field `campaign.bidding_strategy_system_status` and which enum values mean "learning". | ☐ open | | | | | |
| V-8 | §8.4 | Meta insights parameter `use_unified_attribution_setting=true` so numbers match Ads Manager. | ☐ open | | | | | |
| V-9 | §9.2 | Google `change_event` resource: fields for actor and client type, lookback limits. | ☐ open | | | | | |
| V-10 | §9.2 | Meta ad account `activities` edge: fields and which changes it reports. | ☐ open | | | | | |
| V-11 | §15 | Google mutate requests support `validate_only = true` for the operations we use (campaign status, campaign budget, ad group criterion negatives, ad creation). | ☐ open | | | | | |
| V-12 | §15 | Meta `execution_options = ["validate_only"]`: which objects/fields support it (status, budget, ad creation). Where unsupported, validate locally (types, ranges, minimum budget). | ☐ open | | | | | |
| V-13 | §8.2 | Meta ad account fields `currency`, `timezone_name` (IANA), `account_status` (int). `engine/connectors/meta_read.py` `ACCOUNT_FIELDS`. | ☐ open | | | | | |
| V-14 | §8.2 | Meta campaign/ad set fields `status` (configured; used as the tracked status) vs `effective_status` (kept in `raw`), `daily_budget` / `lifetime_budget` absent or `"0"` when unset; campaign budget present ⇒ CBO (`budget_level=campaign`), else ad-set budgets. Also: do `/campaigns`, `/adsets`, `/ads` edges return ARCHIVED/DELETED objects by default (needed so backfilled metrics of old entities resolve)? | ☐ open | | | | | |
| V-15 | §8.3 | Meta ad fields `created_time` (used as `first_live_at`, an approximation) and nested `creative{id,title,body,call_to_action_type,object_type}`. | ☐ open | | | | | |
| V-16 | §8.3 | Meta insights `reach` / `frequency` for one `time_range` WITHOUT `time_increment` = de-duplicated 7-day window (metrics_window). | ☐ open | | | | | |
| V-17 | §8.3 | Meta hourly breakdown `hourly_stats_aggregated_by_advertiser_time_zone` (value format `"13:00:00 - 13:59:59"`, ad-account timezone) and how far back it is served (backfill asks 28 days). | ☐ open | | | | | |
| V-18 | §8.3 | Meta video views as the `video_view` entry of `actions` (3-second views). | ☐ open | | | | | |
| V-19 | §8.2 | Meta Advantage+ detection via campaign `smart_promotion_type ∈ {AUTOMATED_SHOPPING_ADS, SMART_APP_PROMOTION}`; newer Advantage+ campaign types may need another field. | ☐ open | | | | | |
| V-20 | §9.3 | Meta throttling error codes retried with backoff: 4, 17, 32, 613, 80000, 80003, 80004, 80014, plus `api_transient_error`. Consider reading `X-Business-Use-Case-Usage` headers. | ☐ open | | | | | |
| V-21 | §8.2 | Google `advertising_channel_type` → `kind`: SEARCH/DISPLAY/SHOPPING/VIDEO = standard, PERFORMANCE_MAX = pmax, everything else (DEMAND_GEN, SMART, …) = other (campaign-only). Decision, confirm with Govinda. | ☐ open | | | | | |
| V-22 | §8.3 | Google `metrics.video_trueview_views` (field exists in v26 protos; replaces `video_views`). | ☐ open | | | | | |
| V-23 | §9.3 | Google retryable errors: gRPC RESOURCE_EXHAUSTED / INTERNAL / UNAVAILABLE / DEADLINE_EXCEEDED and `quota_error` / `internal_error` in `GoogleAdsFailure`. | ☐ open | | | | | |
| V-24 | §4.3 | Google: each `GoogleAdsService.SearchStream` request = 1 operation for the daily operations limit; whether the Cloud project must also be passed (e.g. quota project header) when developer tokens are retired. The installed `google-ads==35.0.0` client makes `developer_token` optional (SDK evidence for V-1, not a doc check). | ☐ open | | | | | |
| V-25 | §21 | Meta token expiry via `GET /debug_token?input_token=…&access_token=app_id\|app_secret` → `data.expires_at` (0 = never; system-user tokens). Google refresh tokens expose no expiry: health check only detects failures. | ☐ open | | | | | |
| V-26 | §8.2 | Google `campaign_budget.explicitly_shared` ⇒ `budget_level=shared`; `campaign_budget.period=DAILY` ⇒ `amount_micros` is the daily budget. `change_event.change_date_time` format/timezone (`YYYY-MM-DD HH:MM:SS[.ffffff]`, account tz), `changed_fields` serialisation (comma-separated paths) and the `old_resource`/`new_resource` keys used in `_CHANGE_MAP`. | ☐ open | | | | | |
| V-27 | §9.2 | Meta `activities` `extra_data` JSON keys `old_value`/`new_value`, and budget values in minor units. | ☐ open | | | | | |
| V-28 | §15 | Meta new-ad path: a template ad's creative is fetched (`creative{id,title,body,object_story_spec}`) and `link_data`/`video_data` title/body/message are replaced; `execution_options=['validate_only']` on the ad create (V-12). Confirm the story-spec shape and which fields may be overwritten for link and video ads. Constants in `engine/connectors/meta_write.py` (`TEMPLATE_AD_FIELDS`). | ☐ open | | | | | |
| V-29 | §15 | Google Responsive Search Ad limits and mutate fields: 3–15 headlines ≤ 30 chars, 2–4 descriptions ≤ 90 chars; RSA created PAUSED; final URLs copied from the template ad. Constants `RSA_HEADLINES` / `RSA_DESCRIPTIONS` in `engine/connectors/google_write.py`. | ☐ open | | | | | |

Connector notes (connectors agent, 11 Nov 2026): every value above is centralised as a constant
at the top of `engine/connectors/meta_read.py` / `google_read.py`; a doc check changes only
those constants. Contract fixtures in `tests/fixtures/connectors/contract/` are hand-written in
the documented shape (`_synthetic: true`), not recordings — replace them with recorded
read-only responses when P-4 is done and keep the asserted values.

## Phase 0 checklist items that are not `VERIFY-P0` but must also be recorded here

| # | Plan § | Item | Status | Notes |
|---|---|---|---|---|
| P-1 | §12.5 | Meta **account spending limit** set in Ads Manager (Govinda). | ☐ open | |
| P-2 | §12.5 | Google **account budget** set where billing allows; otherwise record "no native backstop" and lower `caps.google.daily`. | ☐ open | |
| P-3 | §4.3 | Google Basic-access application submitted (date, project id). | ☐ open | |
| P-4 | §23 | Both platforms readable from a script with the read credentials. | ☐ open | |
| P-5 | §8.1 / §23 | Recorded money fixtures `tests/fixtures/money/{meta,google}.json` pass (`tests/contract/test_money_fixtures.py`). | ☐ open | |
| P-6 | §8.1 | Both ad accounts use the same currency (bootstrap enforces once accounts are ingested). | ☐ open | |
| P-7 | §25 | Inputs I-1, I-2, I-4, I-9, I-10 received (and I-7 hosting). | ☐ open | |
