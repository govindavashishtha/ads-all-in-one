# UI QA — ads-autopilot dashboard (synthetic `ui_demo`)

Explored 12 Nov 2026 (Asia/Kolkata) against a fresh `ads_autopilot_ui_demo`
database (migration 0004, synthetic fixtures, `LLM_PROVIDER=fake`, fake write
platforms). Driven through the Playwright MCP at
`http://localhost:8080/ads-autopilot` (Caddy → Next :3000 + API :8000).
Admin is the env user; viewer is the same user down-scoped to a browser
session (`AUTH_MODE=env` has a single login). No live Meta/Google calls.

Stack start/stop and PIDs: [LOCAL_STACK.md](LOCAL_STACK.md).
Screenshots: `/workspace/ads-autopilot-screens/qa-*.png`.

Caps and targets on this database are the plan §12.2 / `limits.yaml` /
`rules.yaml` **placeholders** (`inputs_confirmed: false`), allowed only
because `APP_ENV=development`. They are not Govinda’s I-1 / I-2 answers.

## Summary

| Severity | Found | Fixed | Open |
|---|---:|---:|---:|
| High     | 0 | 0 | 0 |
| Medium   | 6 | 6 | 0 |
| Low      | 8 | 3 | 5 |

## Findings

### QA-1 — Rankings: first ad group’s ads omit the ad-set name (medium) — **fixed**

- **Page:** Rankings → Ads
- **Steps:** Open Rankings. Look at the first peer group (`ad|ad_group:1`,
  SYN ad 9200011 / 9200012).
- **Expected:** Parent line is `SYN Prospecting (CBO) › SYN ad set 910001`,
  same pattern as later groups.
- **Actual:** Only `SYN Prospecting (CBO)` was shown. The API *did* return
  `parent_name`. The UI hid it when `parent_id === campaign_id`. Those ids
  come from different tables and both start at 1 on a fresh database.
- **Screenshot:** `qa-05-rankings-ads.png` (before), `qa-27-rankings-fixed.png` (after)
- **Fix:** `rankingParentLine()` compares names, not ids.
  Regression: `dashboard/tests/rankings.test.ts`.

### QA-2 — Meta ad account status shown as “1” (medium) — **fixed**

- **Page:** System → Ad accounts
- **Steps:** Open System. Read the Status column.
- **Expected:** A human status (Active / Disabled / …), like Google’s “Enabled”.
- **Actual:** Meta showed raw Marketing API `account_status` `1`.
  (V-13 is still VERIFY-P0 open; the connector stores the integer as a string.)
- **Screenshot:** `qa-13-system.png` (before), `qa-28-system-fixed.png` (after)
- **Fix:** Display-only map in `accountStatusLabel()`; `StatusBadge` accepts
  `platform`. Stored values are unchanged.

### QA-3 — System page overflows the viewport at 390px (medium) — **fixed**

- **Page:** System, mobile width (390×844)
- **Steps:** Resize to 390px and open System.
- **Expected:** The page stays within the viewport; wide tables scroll inside
  their card.
- **Actual:** `documentElement.scrollWidth` was 801px. The jobs / accounts
  tables expanded `Card` (a grid item with `min-width: auto`) and blew out
  the page. The AI usage table had the same class of bug (QA-4).
- **Screenshot:** `qa-14-mobile-system.png` (before), `qa-32-mobile-system.png` (after)
- **Fix:** `min-w-0` on `Card` and the app shell; `overflow-x-auto` around
  the ad-accounts and AI “by purpose” tables; `min-w-0` on System grids and
  `<main>`. Re-verified: no page-level overflow on System, AI usage,
  Overview, Rankings, Campaigns, Approvals at 390px.

### QA-4 — AI usage page overflows at 390px (medium) — **fixed**

- **Page:** AI usage, mobile width
- **Steps:** Resize to 390px and open AI usage.
- **Expected:** The purpose table scrolls inside the card; the page does not.
- **Actual:** `scrollWidth` 586px. The purpose table had no overflow wrapper.
- **Screenshot:** `qa-19-mobile-ai.png` (before), `qa-32-mobile-ai.png` (after)
- **Fix:** See QA-3.

### QA-5 — Heartbeat Health says “Stale” when no URL is configured (medium) — **fixed**

- **Page:** System → Heartbeat
- **Steps:** Open System with `HEARTBEAT_URL` unset.
- **Expected:** Health reflects “not configured”, matching the Last ping badge.
- **Actual:** Health was “Stale” because the UI used `healthy ? "healthy" : "stale"`,
  and `healthy` is false when the ping is not configured.
- **Screenshot:** `qa-13-system.png` (before), `qa-28-system-fixed.png` (after)
- **Fix:** If `configured` is false or `last_status === "not_configured"`,
  show “Not configured”.

### QA-6 — “Run a job” claimed read-only jobs only (medium) — **fixed**

- **Page:** System → Run a job now
- **Steps:** Read the card subtitle. The job list includes `execute_approved`,
  `emergency_restore`, `evaluate`.
- **Expected:** Copy must not imply those jobs cannot write.
- **Actual:** “Queued for the engine scheduler (read-side jobs only; nothing
  is written to an ad platform)”.
- **Screenshot:** `qa-13-system.png` (before), `qa-28-system-fixed.png` (after)
- **Fix:** Subtitle now says jobs are queued (~1 min) and write jobs still
  go through the write gate; nothing is sent from the page.

### QA-7 — “1 calls” / “1 entities” grammar (low) — **fixed**

- **Pages:** AI usage (token stats), Rankings (peer-group header)
- **Expected:** “1 call”, “1 entity”.
- **Actual:** “1 calls”; peer groups always said “entities”.
- **Screenshot:** `qa-12-ai-usage.png` (before), `qa-29-ai-fixed.png` (after)
- **Fix:** `pluralize()`; `peerGroupLabel()`.

### QA-8 — Empty Executed / Rejected tab copy (low) — **fixed**

- **Page:** Approvals → Executed / Rejected
- **Expected:** Empty copy that matches the tab.
- **Actual:** Title “No proposals” and body “New proposals are created by
  the daily pipeline.”
- **Screenshot:** `qa-30-executed-empty.png` (after)
- **Fix:** Tab-specific title and “Decisions and executions will show up here.”

### QA-9 — Missing favicon at the site root (low) — **open**

- **Page:** Every page (browser requests `http://localhost:8080/favicon.ico`)
- **Expected:** An icon, or a 204, without a console 404.
- **Actual:** Caddy’s default handler returns 404 for `/favicon.ico` (it is
  outside `/ads-autopilot`). One console error on every load.
- **Screenshot:** `qa-01-login.png`
- **Why open:** Fixing it means a Caddy route and/or a document icon under
  `basePath`. Left as a small proxy/asset follow-up, not a product-logic bug.

### QA-10 — Dry-run card exposes the API path and raw JSON (low) — **open**

- **Page:** Approvals → proposal detail → Dry run → Load
- **Expected:** A readable plan (guardrail, write gate, ops).
- **Actual:** The plan *and* a raw JSON dump, with subtitle
  `GET /proposals/{id}/dry-run`. Useful for operators, noisy for a human
  approver.
- **Screenshot:** `qa-10-approval-detail-dryrun.png`
- **Why open:** Could be hidden behind an “Show payload” toggle; needs a
  product call on how technical the dry-run card should be.

### QA-11 — Peer-group headers show raw keys (low) — **open**

- **Page:** Rankings
- **Expected:** A readable group label (e.g. “Ads in SYN ad set 910001”).
- **Actual:** `Peer group: ad|ad_group:1 · 2 entities` (grammar fixed in QA-7;
  the key itself is still raw).
- **Screenshot:** `qa-27-rankings-fixed.png`
- **Why open:** Cosmetic; the parent line under each entity already names
  the ad set / campaign.

### QA-12 — Ad format shown as `SHARE` (low) — **open**

- **Page:** Campaign detail → expanded ad set
- **Expected:** A human creative format (or “—” if unknown).
- **Actual:** Raw `SHARE` (Meta object/creative type) because `humanize()`
  only capitalises the first character of an already-uppercase token.
- **Screenshot:** `qa-04-campaign-detail.png`
- **Why open:** Display-only; changing `humanize()` globally needs a careful
  pass over every call site.

### QA-13 — Money formatting mixes `₹2,000` and `₹500.00` (low) — **open**

- **Page:** Campaigns list / campaign detail
- **Expected:** One rule (whole rupees vs always two decimals).
- **Actual:** `formatMoney` drops `.00` for some amounts and keeps it for
  others (e.g. ₹2,000 vs ₹500.00). Indian grouping itself (`₹5,50,000`) is
  correct for `en-IN`.
- **Screenshot:** `qa-03-campaigns-desktop.png`
- **Why open:** Formatter behaviour is already tested; changing it would
  ripple through every money cell. Not wrong, just uneven.

## Verified (not bugs)

- Login / logout under `basePath`; Secure cookies on `http://localhost`; CSRF
  on approve/reject (not exercised against a live platform).
- Viewer cannot Approve / Reject, Enable writes, Pause all, or Run a job.
  Managed switches are visible but disabled (`title`: “Admin role required”).
  Dry-run remains a GET and is available to viewers, which matches the API.
- Write gate reports CLOSED while `execution_enabled` is false (default, D-28).
- Escalated tab shows the fake-LLM triage line (“no action (confidence 60%)”).
- Blocked add-negative on SYN Generic Search is `unsupported_scope` (shared
  budget, D-10). That strictness is a standing product decision, not a UI bug.
- “Spend today ₹0” and “oldest sync 13h ago” / accounts marked stale are
  expected: the demo pipeline was pinned to 2026-11-11 22:00 Asia/Kolkata
  and the API clock is wall-clock 12 Nov 2026. MTD, 7d, and rankings (`as_of`
  2026-11-10) still have data.

## Open questions for Govinda

1. **D-10 (already open):** keep blocking `add_negative` inside shared-budget
   campaigns, or loosen that?
2. Should the dry-run card keep the raw JSON payload (QA-10), or hide it
   behind a toggle?
3. When you are ready for a live read-only pass, the demo clock should be
   dropped so “today” and last-sync freshness match the real account day.
