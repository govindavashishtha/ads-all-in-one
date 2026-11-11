# Money fixtures (§8.1, §23 Phase 0 acceptance)

Record ONE known budget and ONE known day's spend per platform from the real accounts
(read-only), compare to what the platform UI shows, and save them as `meta.json` /
`google.json` (same shape as `*.example.json`, with `"synthetic": false`, plus `source`
= how/when recorded). Strip ids/tokens. `tests/contract/test_money_fixtures.py` then asserts
both round-trip. Until then the "real fixtures recorded" test is skipped (Phase 0 open item).
