// Real-API smoke + screenshots (headless Chromium via Playwright). No mock transport.
//   ADS_USER=admin ADS_PASSWORD=… BASE_URL=http://localhost:8080/ads-autopilot \
//   OUT_DIR=/workspace/ads-autopilot-screens npm run smoke:real
// Needs: a dashboard build with NEXT_PUBLIC_MOCK_API=false behind the proxy, and the API
// (uvicorn --root-path /ads-autopilot) against a database with scored data in suggest mode
// (e.g. ads_autopilot_score_demo). Cookies are Secure: use https or http://localhost.
// APPROVE=0 skips the approval (it records a real decision in the database; nothing is sent to
// an ad platform before Phase 3).
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import path from "node:path";

const BASE = (process.env.BASE_URL ?? "http://localhost:8080/ads-autopilot").replace(/\/$/, "");
const OUT = process.env.OUT_DIR ?? "/workspace/ads-autopilot-screens";
const USER = process.env.ADS_USER ?? "admin";
const PASSWORD = process.env.ADS_PASSWORD;
const APPROVE = process.env.APPROVE !== "0";
if (!PASSWORD) {
  console.error("Set ADS_PASSWORD (the password matching the API's ADMIN_PASSWORD_HASH)");
  process.exit(2);
}
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-IN", timezoneId: "Asia/Kolkata" });
const page = await ctx.newPage();
let phase = "anonymous";
const errors = []; // console errors / page errors once signed in
const anonErrors = []; // before login (the expected 401 from /auth/me lands here)
const apiFailures = [];
page.on("pageerror", (e) => (phase === "anonymous" ? anonErrors : errors).push(String(e)));
page.on("console", (m) => m.type() === "error" && (phase === "anonymous" ? anonErrors : errors).push(m.text()));
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("/api/v1/") && r.status() >= 400 && !(phase === "anonymous" && r.status() === 401)) apiFailures.push(`${r.status()} ${r.request().method()} ${u.replace(BASE, "")}`);
});

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}
async function shot(name, fullPage = true) {
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(700); // chart entry animation
  const file = path.join(OUT, `real-${name}.png`);
  await page.screenshot({ path: file, fullPage });
  console.log(`saved ${file}`);
}
async function step(name, fn) {
  try {
    await fn();
  } catch (e) {
    check(name, false, String(e).split("\n")[0]);
    await page.screenshot({ path: path.join(OUT, `real-FAIL-${name.replace(/\W+/g, "-")}.png`), fullPage: true }).catch(() => {});
  }
}

// 1. API docs reachable through the prefix (uvicorn --root-path)
await step("api docs", async () => {
  const r = await page.goto(`${BASE}/api/v1/docs`);
  check("API /docs served behind the proxy", r?.status() === 200);
  const spec = await page.request.get(`${BASE}/api/v1/openapi.json`);
  check("openapi.json loads through the prefix", spec.status() === 200);
});

// 2. Unauthenticated → login under the base path; no mock banner in a real build
await step("login", async () => {
  await page.goto(`${BASE}/campaigns`);
  await page.waitForURL(/\/ads-autopilot\/login/);
  check("redirects to login under basePath", page.url().includes("/ads-autopilot/login"), page.url());
  check("no mock banner (real build)", (await page.getByTestId("mock-banner").count()) === 0);
  await shot("01-login");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  phase = "signed-in";
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/ads-autopilot\/campaigns$/);
  check("login returns to ?next page", page.url().endsWith("/ads-autopilot/campaigns"), page.url());
  const cookies = await ctx.cookies();
  const sess = cookies.find((c) => c.name === "aa_session");
  check("session cookie is httpOnly + Secure", !!sess && sess.httpOnly && sess.secure);
});

// 3. Overview
await step("overview", async () => {
  await page.goto(`${BASE}`);
  await page.getByText("Spend today vs daily cap").waitFor();
  await page.getByTestId("budget-gauge").first().waitFor();
  check("overview renders 3 gauges", (await page.getByTestId("budget-gauge").count()) === 3);
  await shot("02-overview");
});

// 4. Campaigns + drilldown into the first campaign
await step("campaigns", async () => {
  await page.goto(`${BASE}/campaigns`);
  const first = page.locator('a[href*="/campaigns/"]').first();
  await first.waitFor();
  check("campaign list has rows", (await page.locator('a[href*="/campaigns/"]').count()) > 0);
  await shot("03-campaigns");
  await first.click();
  await page.getByText("Daily metrics").waitFor();
  await shot("04-campaign-detail");
});

// 5. Rankings (ad, ad group, campaign)
await step("rankings", async () => {
  await page.goto(`${BASE}/rankings`);
  await page.getByText(/ranked/).first().waitFor();
  await shot("05-rankings", false);
});

// 6. Approvals: approve the first awaiting proposal
let approvedId = null;
await step("approvals", async () => {
  await page.goto(`${BASE}/approvals`);
  await page.waitForLoadState("networkidle");
  const cards = page.locator('[data-testid^="proposal-"]');
  const n = await cards.count();
  check("awaiting proposals listed", n > 0, `${n} awaiting`);
  await shot("06-approvals");
  if (n > 0 && APPROVE) {
    const card = cards.first();
    approvedId = (await card.getAttribute("data-testid")).replace("proposal-", "");
    await card.getByRole("button", { name: "Approve" }).click();
    await page.getByTestId(`proposal-${approvedId}`).waitFor({ state: "detached" });
    check(`approve #${approvedId} removes it from the awaiting queue`, true);
    await page.goto(`${BASE}/approvals/${approvedId}`);
    await page.getByTestId(`proposal-${approvedId}`).waitFor();
    const txt = await page.getByTestId(`proposal-${approvedId}`).innerText();
    check(`#${approvedId} shows approved by ${USER}`, /approved/i.test(txt) && txt.includes(USER));
    await shot("07-approval-detail");
  }
});

// 7. Escalated tab (LLM triage outcome when present)
await step("escalated", async () => {
  await page.goto(`${BASE}/approvals`);
  await page.getByRole("button", { name: "Escalated" }).click();
  await page.waitForLoadState("networkidle");
  await shot("08-escalated", false);
});

// 8. Change log, AI usage, System
await step("changes", async () => {
  await page.goto(`${BASE}/changes`);
  await page.getByRole("heading", { name: /Change log/i }).first().waitFor();
  await shot("09-change-log", false);
});
await step("ai-usage", async () => {
  await page.goto(`${BASE}/ai-usage`);
  await page.getByTestId("llm-status").waitFor();
  check("AI usage shows the engine's LLM status", true, await page.getByTestId("llm-status").innerText());
  await shot("10-ai-usage", false);
});
await step("system", async () => {
  await page.goto(`${BASE}/system`);
  await page.getByText("Ad accounts").first().waitFor();
  await page.getByTestId("ops-google").waitFor();
  check("system shows op counters and heartbeat", (await page.getByTestId("ops-google").isVisible()) && (await page.getByText(/heartbeat/i).first().isVisible()));
  await shot("11-system");
});

// 9. Logout revokes the session
await step("logout", async () => {
  await page.getByRole("button", { name: "Log out" }).click();
  await page.waitForURL(/\/login/);
  phase = "anonymous";
  await page.goto(`${BASE}/system`);
  await page.waitForURL(/\/login/);
  check("logout ends the session", page.url().includes("/login"));
});

check("no API errors while signed in", apiFailures.length === 0, apiFailures.slice(0, 5).join(" | "));
check("no browser console errors while signed in", errors.length === 0, errors.slice(0, 5).join(" | "));
// Before login the only expected console noise is the browser's "401" line for GET /auth/me.
const unexpectedAnon = anonErrors.filter((e) => !/status of 401/.test(e));
check("no console errors before login other than the expected 401", unexpectedAnon.length === 0, unexpectedAnon.slice(0, 3).join(" | "));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed${approvedId ? ` (approved #${approvedId})` : ""}`);
process.exit(failed.length ? 1 : 0);
