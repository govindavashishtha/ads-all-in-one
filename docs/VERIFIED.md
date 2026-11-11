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
