import { beforeEach, describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { MOCK_PHASE_KEY, mockTransport, resetMockDb } from "@/lib/mock/transport";
import type { DecisionResponse, LlmUsage, Overview, Rankings } from "@/lib/types";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetMockDb(new Date("2026-11-11T12:00:00Z"));
});

const login = () => mockTransport("POST", "/auth/login", { username: "u", password: "p" });

describe("mock transport", () => {
  it("requires a session like the real API", async () => {
    const e = await mockTransport("GET", "/overview").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).status).toBe(401);
  });

  it("serves an overview with the account currency and caps", async () => {
    await login();
    const o = (await mockTransport("GET", "/overview?range=7d")) as Overview;
    expect(o.currency).toBe("INR");
    expect(o.total.platform).toBe("total");
    expect(o.platforms.map((s) => s.platform)).toEqual(["meta", "google"]);
    expect(Number(o.total.daily_cap)).toBeGreaterThan(0);
  });

  it("mimics the real API for phases the simulated backend lacks", async () => {
    await login();
    localStorage.setItem(MOCK_PHASE_KEY, "1");
    const r = (await mockTransport("GET", "/rankings?level=ad")) as Rankings;
    expect(r.as_of).toBeNull();
    expect(r.rows).toEqual([]);
    expect(((await mockTransport("POST", "/changes/14/rollback", {}).catch((x) => x)) as ApiError).notEnabled).toBe(true);
    expect(((await mockTransport("GET", "/llm/usage?range=30d")) as LlmUsage).llm_enabled).toBe(false);
    expect(await mockTransport("GET", "/system")).toBeTruthy();
  });

  it("approves only proposals that await approval", async () => {
    await login();
    const r = (await mockTransport("POST", "/proposals/41/approve", {})) as DecisionResponse;
    expect(r.proposal.status).toBe("approved");
    expect(r.next_step).toBeTruthy();
    const again = await mockTransport("POST", "/proposals/41/approve", {}).catch((x) => x);
    expect((again as ApiError).status).toBe(409);
  });
});
