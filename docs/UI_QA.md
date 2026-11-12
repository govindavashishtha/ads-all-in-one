# UI QA — ads-autopilot dashboard (synthetic `ui_demo`)

Explored 12 Nov 2026 (Asia/Kolkata) against a fresh `ads_autopilot_ui_demo`
database (migration 0004, synthetic fixtures, `LLM_PROVIDER=fake`, fake write
platforms). Driven through the Playwright MCP at
`http://localhost:8080/ads-autopilot` (Caddy → Next :3000 + API :8000).
Admin is the env user; viewer is the same user down-scoped to a browser
session (`AUTH_MODE=env` has a single login). No live Meta/Google calls.

A second pass the same morning (11:03 Asia/Kolkata) fixed the remaining
no-decision lows (QA-9 favicon, QA-11 peer-group labels, QA-12 ad format,
QA-13 money). QA-10 (dry-run JSON), D-10, and the demo-clock vs wall-clock
mismatch are still open on purpose.

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
| Low      | 8 | 7 | 1 |

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

### QA-9 — Missing favicon at the site root (low) — **fixed**

- **Page:** Every page (browser requests `http://localhost:8080/favicon.ico`)
- **Expected:** An icon, or a 204, without a console 404.
- **Actual (before):** Caddy’s default handler returned 404 for `/favicon.ico`
  (outside `/ads-autopilot`). One console error on every load.
- **Fix:** Ship `dashboard/public/icon.svg` and `favicon.ico` (bar-chart mark).
  Layout metadata points at `${BASE_PATH}/icon.svg` and `${BASE_PATH}/favicon.ico`.
  Caddy rewrites origin-root `/favicon.ico` and `/icon.svg` to the same files
  under `/ads-autopilot`, because Chromium still requests the origin root.
- **Re-check (Playwright MCP + curl, 12 Nov 2026 11:03 Asia/Kolkata):**
  `http://localhost:8080/favicon.ico`, `/icon.svg`,
  `/ads-autopilot/favicon.ico`, and `/ads-autopilot/icon.svg` all return 200.
  Login HTML has `<link rel="icon" href="/ads-autopilot/icon.svg">` and
  `/ads-autopilot/favicon.ico`. No favicon 404s in the session.
- **Screenshot:** `qa-01-login.png` (before)

### QA-10 — Dry-run card exposes the API path and raw JSON (low) — **open**

- **Page:** Approvals → proposal detail → Dry run → Load
- **Expected:** A readable plan (guardrail, write gate, ops).
- **Actual:** The plan *and* a raw JSON dump, with subtitle
  `GET /proposals/{id}/dry-run`. Useful for operators, noisy for a human
  approver.
- **Screenshot:** `qa-10-approval-detail-dryrun.png`
- **Why open:** Could be hidden behind an “Show payload” toggle; needs a
  product call on how technical the dry-run card should be.

### QA-11 — Peer-group headers show raw keys (low) — **fixed**

- **Page:** Rankings
- **Expected:** A readable group label (e.g. “Ads in SYN ad set 910001”).
- **Actual (before):** `Peer group: ad|ad_group:1 · 2 entities` (grammar fixed
  in QA-7; the key itself was still raw).
- **Fix:** `peerGroupLabel` / `peerGroupDescription` parse the engine key
  (`ad|ad_group:N`, `ad_group|campaign:N`, `keyword|ad_group:N|match:…`,
  `campaign|{platform}|objective:…`) and use the row’s `parent_name` /
  `campaign_name` / platform. Meta containers are “ad set(s)”, Google
  “ad group(s)”. The raw key stays in the header cell’s `title` tooltip.
- **Re-check (Playwright MCP):** Ads → “Ads in SYN ad set 910001 · 2 entities”
  (`title=ad|ad_group:1`); Ad sets / ad groups → “Ad sets in SYN Prospecting
  (CBO) · 2 entities” and “Ad groups in SYN Brand Search · 1 entity”;
  Keywords → “Keywords in SYN ad group 810001 · broad match · 1 entity”;
  Campaigns → “Meta campaigns · Sales · 3 entities”, “Google campaigns ·
  Search · 2 entities”, “Google campaigns · Performance Max · 1 entity”.
- **Screenshot:** `qa-27-rankings-fixed.png` (before), `qa-34-rankings-peergroups.png` (after)

### QA-12 — Ad format shown as `SHARE` (low) — **fixed**

- **Page:** Campaign detail → expanded ad set
- **Expected:** A human creative format (or “—” if unknown), with the raw
  platform value available somewhere.
- **Actual (before):** Raw `SHARE` (Meta `creative.object_type`) because
  `humanize()` only capitalises the first character of an already-uppercase
  token. Google ads showed `RESPONSIVE_SEARCH_AD` the same way.
- **Fix:** New `adFormatLabel()` maps known Meta `object_type` and Google
  `AdType` values (SHARE → “Link”, PHOTO → “Single image”,
  RESPONSIVE_SEARCH_AD → “Responsive search ad”; unknown tokens are
  title-cased). Campaign detail renders the friendly label with
  `title={raw}`. `humanize()` is unchanged so other call sites are untouched.
- **Re-check (Playwright MCP):** SYN Prospecting (CBO) ads show “Link” with
  tooltip `SHARE`; SYN Brand Search ads show “Responsive search ad” with
  tooltip `RESPONSIVE_SEARCH_AD`. Visible text no longer contains the raw tokens.
- **Screenshot:** `qa-04-campaign-detail.png` (before),
  `qa-35-campaign-meta-format.png` / `qa-36-campaign-google-format.png` (after)

### QA-13 — Money formatting mixes `₹2,000` and `₹500.00` (low) — **fixed**

- **Page:** Campaigns list / campaign detail (and every other money cell)
- **Expected:** One documented rule (always two decimals, or always none).
- **Actual (before):** `formatMoney` dropped fraction digits when
  `|amount| >= 1000` (`₹2,000` vs `₹500.00`; USD `1234.5` rounded to `$1,235`).
  Indian grouping itself (`₹5,50,000`) was already correct for `en-IN`.
- **Fix / rule:** One shared formatter, documented on `formatMoney` in
  `dashboard/src/lib/format.ts`. Major currency units always show exactly two
  decimal places, including trailing zeros (`₹2,000.00`, `₹500.00`,
  `$1,234.50`, `₹1,29,670.00`, `₹5,50,000.00`), matching BUILD_PLAN §0 / §8
  (Decimal, major units, 2 dp outside connectors). `opts.decimals` overrides;
  `opts.compact` remains for chart axis ticks only (compact notation, 1
  fraction digit). Tests updated to the same rule.
- **Re-check (Playwright MCP, desktop + 390px):** Overview, Campaigns, Rankings,
  and campaign detail show only two-decimal amounts (e.g. daily budget
  `₹2,000.00` / `₹3,000.00` next to target CPA `₹500.00`; monthly cap
  `₹5,50,000.00`). No leftover whole-rupee amounts. Chart axis ticks still use
  compact `1.7K`-style labels. No page-level overflow at 390px.
- **Screenshot:** `qa-03-campaigns-desktop.png` (before),
  `qa-33-overview-money.png`, `qa-36-campaign-google-format.png`,
  `qa-37-campaigns-390.png` (after)

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
