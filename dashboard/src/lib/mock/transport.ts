/**
 * In-browser fake of the /api/v1 contract (docs/openapi.json) for mock mode. EXAMPLE data only.
 *
 * Demo knob (localStorage): `ads-autopilot:mockPhase` = "1".."5" (default 5) simulates a backend
 * at that phase, like the real API does: Phase 2+ data is empty before scoring runs, Phase 3
 * routes answer 501 `not_enabled`, and LLM usage reports `llm_enabled: false` before Phase 4.
 */
import { ApiError, type HttpMethod, type Transport } from "../api";
import type { Proposal, RankLevel } from "../types";
import { buildMockDb, mockLlmUsage, mockMetrics, mockOverview, mockRankings, mockRules, mockSystem, type MockDb } from "./data";

const SESSION_KEY = "ads-autopilot:mockSession";
export const MOCK_PHASE_KEY = "ads-autopilot:mockPhase";

let db: MockDb | null = null;
function getDb(): MockDb {
  if (!db) db = buildMockDb(new Date());
  return db;
}
/** Test hook. */
export function resetMockDb(now?: Date): void {
  db = buildMockDb(now ?? new Date());
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}
function mockPhase(): number {
  try {
    const v = typeof window !== "undefined" ? window.localStorage.getItem(MOCK_PHASE_KEY) : null;
    const n = v ? Number(v) : 5;
    return Number.isFinite(n) ? n : 5;
  } catch {
    return 5;
  }
}

function notEnabled(phase: string, message: string): never {
  throw new ApiError(501, message, { detail: { code: "not_enabled", phase, message } });
}

function requireSession(): void {
  if (storage()?.getItem(SESSION_KEY) !== "1") throw new ApiError(401, "Not authenticated");
}

function decide(d: MockDb, id: number, to: "approved" | "rejected", note?: string | null): Proposal {
  const p = d.proposals.find((x) => x.id === id);
  if (!p) throw new ApiError(404, "Proposal not found");
  if (p.status !== "awaiting_approval") throw new ApiError(409, `Proposal ${p.id} is ${p.status}, not awaiting_approval`);
  p.status = to;
  if (p.approval) {
    p.approval = { ...p.approval, decided_at: new Date().toISOString(), decision: to === "approved" ? "approve" : "reject", decided_by: "demo", comment: note ?? null };
  }
  return p;
}

const NEXT_STEP = "Example: the executor re-runs the guardrail with fresh state before applying (Phase 3).";

async function handle(method: HttpMethod, fullPath: string, body?: unknown): Promise<unknown> {
  const [path, query = ""] = fullPath.split("?");
  const q = new URLSearchParams(query);
  const d = getDb();
  const phase = mockPhase();

  if (method === "POST" && path === "/auth/login") {
    const b = (body ?? {}) as { username?: string; password?: string };
    if (!b.username || !b.password) throw new ApiError(422, "Username and password are required");
    storage()?.setItem(SESSION_KEY, "1");
    return { username: b.username, role: "admin", csrf_token: "mock-csrf", expires_at: new Date(Date.now() + 12 * 3600_000).toISOString() };
  }
  requireSession();
  if (method === "POST" && path === "/auth/logout") {
    storage()?.removeItem(SESSION_KEY);
    return {};
  }

  if (method === "GET") {
    if (path === "/auth/me") return { username: "demo", role: "admin", session_kind: "browser", auth_mode: "env (mock)" };
    if (path === "/overview") return mockOverview(d, q.get("range") ?? "7d");
    if (path === "/system") return mockSystem(d);
    if (path === "/rules") return mockRules(d);
    if (path === "/metrics") return mockMetrics(d, q);
    if (path === "/alerts") return d.alerts.filter((a) => q.get("open") !== "true" || !a.acked_at);
    if (path === "/emergency-actions") return [];
    if (path === "/campaigns") {
      return d.campaigns.filter((c) => (!q.get("platform") || c.platform === q.get("platform")) && (!q.get("managed") || String(c.managed) === q.get("managed")));
    }
    let m = path.match(/^\/campaigns\/(\d+)$/);
    if (m) {
      const c = d.campaigns.find((x) => x.id === Number(m![1]));
      if (!c) throw new ApiError(404, "Campaign not found");
      return { ...c, ad_groups: d.adGroups.filter((g) => g.campaign_id === c.id) };
    }
    m = path.match(/^\/ad-groups\/(\d+)$/);
    if (m) {
      const g = d.adGroups.find((x) => x.id === Number(m![1]));
      if (!g) throw new ApiError(404, "Ad group not found");
      return { ...g, ads: d.ads.filter((a) => a.ad_group_id === g.id), keywords: [] };
    }
    if (path === "/rankings") {
      if (phase < 2) return { as_of: null, level: q.get("level") ?? "ad", rows: [] };
      return mockRankings(d, (q.get("level") ?? "ad") as RankLevel, q.get("platform") ?? undefined);
    }
    if (path === "/proposals") {
      if (phase < 2) return [];
      const st = q.get("status");
      return d.proposals.filter((p) => !st || p.status === st);
    }
    m = path.match(/^\/proposals\/(\d+)\/dry-run$/);
    if (m) {
      if (phase < 3) notEnabled("3", "Dry-run payloads are built by the executor (Phase 3).");
      const p = d.proposals.find((x) => x.id === Number(m![1]));
      return { example: true, action: p?.action, params: p?.params, validate_only: true };
    }
    m = path.match(/^\/proposals\/(\d+)$/);
    if (m) {
      const p = phase >= 2 ? d.proposals.find((x) => x.id === Number(m![1])) : undefined;
      if (!p) throw new ApiError(404, "Proposal not found");
      return p;
    }
    if (path === "/changes") return phase < 3 ? [] : d.changes;
    if (path === "/llm/usage") {
      const u = mockLlmUsage(d, q.get("range") ?? "30d");
      if (phase < 4) return { ...u, llm_enabled: false, days: [], by_purpose: {}, today: {}, month_to_date: {} };
      return u;
    }
  }

  if (method === "POST") {
    let m = path.match(/^\/proposals\/(\d+)\/(approve|reject)$/);
    if (m) {
      const b = (body ?? {}) as { comment?: string | null; reason?: string };
      if (m[2] === "reject" && !b.reason) throw new ApiError(422, "reason is required");
      return { proposal: decide(d, Number(m[1]), m[2] === "approve" ? "approved" : "rejected", m[2] === "approve" ? b.comment : b.reason), next_step: NEXT_STEP };
    }
    if (path === "/proposals/approve-batch") {
      const b = (body ?? {}) as { ids?: number[]; comment?: string | null };
      return {
        next_step: NEXT_STEP,
        results: (b.ids ?? []).map((id) => {
          try {
            return { proposal_id: id, ok: true, status: decide(d, id, "approved", b.comment).status, error: null };
          } catch (e) {
            return { proposal_id: id, ok: false, status: d.proposals.find((x) => x.id === id)?.status ?? null, error: (e as Error).message };
          }
        }),
      };
    }
    m = path.match(/^\/changes\/(\d+)\/rollback$/);
    if (m) {
      if (phase < 3) notEnabled("3", "Rollback ships with the executor (Phase 3).");
      const c = d.changes.find((x) => x.id === Number(m![1]));
      if (!c) throw new ApiError(404, "Change not found");
      if (c.status !== "applied") throw new ApiError(409, `Change ${c.id} is ${c.status}`);
      c.status = "rolled_back";
      return c;
    }
  }

  if (method === "PUT") {
    const m = path.match(/^\/campaigns\/(\d+)\/managed$/);
    if (m) {
      const c = d.campaigns.find((x) => x.id === Number(m[1]));
      if (!c) throw new ApiError(404, "Campaign not found");
      c.managed = Boolean((body as { managed?: boolean })?.managed);
      return c;
    }
  }

  throw new ApiError(404, "Not Found");
}

export const mockTransport: Transport = async (method, path, body) => {
  await new Promise((r) => setTimeout(r, 120));
  // structuredClone so callers never mutate the fake DB by accident
  return structuredClone(await handle(method, path, body));
};
