// Mock-mode smoke + screenshots (headless Chromium via Playwright).
//   BASE_URL=http://127.0.0.1:8080/ads-autopilot OUT_DIR=/workspace/ads-autopilot-screens npm run screenshots
// Requires a build with NEXT_PUBLIC_MOCK_API=true served behind the proxy (or `next start`).
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import path from "node:path";

const BASE = (process.env.BASE_URL ?? "http://127.0.0.1:8080/ads-autopilot").replace(/\/$/, "");
const OUT = process.env.OUT_DIR ?? "/workspace/ads-autopilot-screens";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-IN", timezoneId: "Asia/Kolkata" });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}
async function shot(name, fullPage = true) {
  await page.waitForTimeout(700); // let charts finish their entry animation
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage });
  console.log(`saved ${file}`);
}

// 1. Unauthenticated → redirected to login under the base path
await page.goto(`${BASE}/campaigns`);
await page.waitForURL(/\/ads-autopilot\/login/);
check("redirects to login under basePath", page.url().includes("/ads-autopilot/login"), page.url());
check("mock banner visible", await page.getByTestId("mock-banner").isVisible());
await shot("01-login");

// 2. Login → back to the requested page
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL(/\/ads-autopilot\/campaigns$/);
check("login returns to ?next page", page.url().endsWith("/ads-autopilot/campaigns"), page.url());

// 3. Overview
await page.goto(`${BASE}`);
await page.getByText("Spend today vs daily cap").waitFor();
await page.getByTestId("budget-gauge").first().waitFor();
check("overview renders 3 gauges", (await page.getByTestId("budget-gauge").count()) === 3);
check("INR formatting", (await page.locator("body").innerText()).includes("₹"));
await shot("02-overview");

// 4. Campaigns + drilldown
await page.goto(`${BASE}/campaigns`);
await page.getByRole("link", { name: "Prospecting – Broad (example)" }).waitFor();
await shot("03-campaigns");
await page.getByRole("link", { name: "Prospecting – Broad (example)" }).click();
await page.getByText("Daily metrics").waitFor();
await page.getByText("Broad 25–44").first().click();
await page.getByText("Creative A").first().waitFor();
check("campaign drilldown loads ads", true);
await shot("04-campaign-detail");

// 5. Rankings
await page.goto(`${BASE}/rankings`);
await page.getByText(/ranked/).first().waitFor();
await shot("05-rankings", false);

// 6. Approvals + approve flow (authenticated POST in mock transport)
await page.goto(`${BASE}/approvals`);
await page.getByTestId("proposal-41").waitFor();
await shot("06-approvals");
await page.getByTestId("proposal-41").getByRole("button", { name: "Approve" }).click();
await page.getByTestId("proposal-41").waitFor({ state: "detached" });
check("approve removes proposal from the awaiting queue", true);

// 7. Approval link page (GET shows, does not act)
await page.goto(`${BASE}/approvals/42`);
await page.getByTestId("proposal-42").waitFor();
check("approval link page shows Approve button (no auto-action)", await page.getByTestId("proposal-42").getByRole("button", { name: "Approve" }).isVisible());
await shot("07-approval-detail");

// 8. Change log, AI usage, System
await page.goto(`${BASE}/changes`);
await page.getByText("Roll back").first().waitFor();
await shot("08-change-log");
await page.goto(`${BASE}/ai-usage`);
await page.getByText("Tokens per day by tier").waitFor();
await shot("09-ai-usage");
await page.goto(`${BASE}/system`);
await page.getByText("Ad accounts").first().waitFor();
check("system shows API op counter and heartbeat", (await page.getByTestId("ops-google").isVisible()) && (await page.getByText("Last pacing success").isVisible()));
await shot("10-system");

// 9. Phase-1-only backend: later pages show "not enabled yet"
await page.evaluate(() => localStorage.setItem("ads-autopilot:mockPhase", "1"));
await page.goto(`${BASE}/rankings`);
await page.getByTestId("not-enabled").waitFor();
check("rankings shows not-enabled state before scores exist", true);
await shot("11-not-enabled-rankings");
await page.goto(`${BASE}/ai-usage`);
await page.getByTestId("not-enabled").waitFor();
check("AI usage shows not-enabled when llm_enabled=false", true);
await page.goto(`${BASE}/changes`);
await page.getByText("No changes yet").waitFor();
check("change log shows an empty state before Phase 3", true);
await page.evaluate(() => localStorage.setItem("ads-autopilot:mockPhase", "2"));
await page.goto(`${BASE}/approvals/43`);
await page.getByTestId("proposal-43").waitFor();
await page.getByRole("button", { name: "Load" }).click();
await page.getByText(/Dry run is not enabled yet/).waitFor();
check("dry-run 501 not_enabled is shown with its phase", true);
await shot("12-dry-run-not-enabled", false);
await page.evaluate(() => localStorage.removeItem("ads-autopilot:mockPhase"));

// 10. Logout revokes the session
await page.getByRole("button", { name: "Log out" }).click();
await page.waitForURL(/\/login/);
await page.goto(`${BASE}/system`);
await page.waitForURL(/\/login/);
check("logout ends the session", page.url().includes("/login"));

check("no browser console errors", errors.length === 0, errors.slice(0, 3).join(" | "));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
