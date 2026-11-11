/**
 * Typed API client — the ONLY module that knows URLs, methods and auth headers.
 * Reconcile against docs/openapi.json here (and in types.ts) once the API lands.
 *
 * Auth (§17): the browser session is an httpOnly cookie set by POST /auth/login; every
 * state-changing request sends the double-submit CSRF token in X-CSRF-Token.
 */
import { API_URL, CSRF_COOKIE, CSRF_HEADER, MOCK_API } from "./config";
import type {
  AdGroupDetail,
  Alert,
  BatchDecisionResponse,
  Campaign,
  CampaignDetail,
  Change,
  ConfigResponse,
  DecisionResponse,
  EmergencyAction,
  EntityType,
  LlmUsage,
  LoginResponse,
  MetricsSeries,
  Overview,
  Platform,
  Proposal,
  ProposalStatus,
  RankLevel,
  Rankings,
  SystemStatus,
  Targets,
  User,
} from "./types";

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
  /** 401: no or expired session → go to login. */
  get unauthorized(): boolean {
    return this.status === 401;
  }
  /** 403: logged in but the role is too low (§17 roles). */
  get forbidden(): boolean {
    return this.status === 403;
  }
  /**
   * 501 `{detail: {code: "not_enabled", phase}}` = the route's logic ships in a later phase
   * (docs/API.md). 404 on a route is treated the same (older/partial backend).
   */
  get notEnabled(): boolean {
    return this.status === 501 || this.status === 404;
  }
  /** Phase named by a 501 not-enabled body, if any. */
  get phase(): string | null {
    const d = (this.body as { detail?: { phase?: unknown } } | undefined)?.detail;
    return d && typeof d === "object" && d.phase != null ? String(d.phase) : null;
  }
}

/** Pluggable transport: the real fetch or the in-browser mock (src/lib/mock). */
export type Transport = (method: HttpMethod, path: string, body?: unknown) => Promise<unknown>;

let csrfMemory: string | null = null;

export function readCookie(name: string, cookieString?: string): string | null {
  const src = cookieString ?? (typeof document !== "undefined" ? document.cookie : "");
  for (const part of src.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return null;
}

export function setCsrfToken(token: string | null): void {
  csrfMemory = token;
}

export function csrfToken(): string | null {
  return readCookie(CSRF_COOKIE) ?? csrfMemory;
}

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
    if (Array.isArray(d)) return d.map((x) => (x as { msg?: string }).msg ?? String(x)).join("; ");
  }
  return `Request failed (${status})`;
}

export function createFetchTransport(baseUrl: string, fetchImpl: typeof fetch = fetch): Transport {
  return async (method, path, body) => {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") {
      const t = csrfToken();
      if (t) headers[CSRF_HEADER] = t;
    }
    const res = await fetchImpl(`${baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "include",
      cache: "no-store",
    });
    const text = await res.text();
    let parsed: unknown = undefined;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    }
    if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, parsed), parsed);
    return parsed;
  };
}

let transport: Transport | null = null;

async function getTransport(): Promise<Transport> {
  if (transport) return transport;
  if (MOCK_API) {
    const { mockTransport } = await import("./mock/transport");
    transport = mockTransport;
  } else {
    transport = createFetchTransport(API_URL);
  }
  return transport;
}

/** Test hook. */
export function setTransport(t: Transport | null): void {
  transport = t;
}

async function call<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  const t = await getTransport();
  return (await t(method, path, body)) as T;
}

/** Accept both `[...]` and `{items: [...]}` list envelopes until the OpenAPI spec pins one. */
export function unwrapList<T>(v: unknown): T[] {
  if (Array.isArray(v)) return v as T[];
  if (v && typeof v === "object" && Array.isArray((v as { items?: unknown }).items)) {
    return (v as { items: T[] }).items;
  }
  return [];
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

/** Parse targets from the active rules body (§11.2 `targets`); nulls if absent. */
export function targetsFromRules(r: ConfigResponse | null | undefined): Targets {
  const t = ((r?.active?.body ?? {}) as { targets?: Record<string, unknown> }).targets ?? {};
  const obj = t.objective;
  return {
    objective: obj === "conversions" || obj === "value" || obj === "traffic" ? obj : null,
    target_cpa: (t.target_cpa as string | number | null | undefined) ?? null,
    target_roas: (t.target_roas as string | number | null | undefined) ?? null,
  };
}

export const api = {
  /* auth */
  async login(username: string, password: string): Promise<LoginResponse> {
    const r = await call<LoginResponse>("POST", "/auth/login", { username, password });
    if (r?.csrf_token) setCsrfToken(r.csrf_token);
    return r;
  },
  async logout(): Promise<void> {
    try {
      await call("POST", "/auth/logout");
    } finally {
      setCsrfToken(null);
    }
  },
  me: () => call<User>("GET", "/auth/me"),

  /* Phase 1 */
  overview: (range = "7d") => call<Overview>("GET", `/overview${qs({ range })}`),
  campaigns: async (q: { platform?: Platform; managed?: boolean; range?: string } = {}) =>
    unwrapList<Campaign>(await call("GET", `/campaigns${qs({ range: "7d", ...q })}`)),
  campaign: (id: number | string, range = "7d") => call<CampaignDetail>("GET", `/campaigns/${id}${qs({ range })}`),
  adGroup: (id: number | string, range = "7d") => call<AdGroupDetail>("GET", `/ad-groups/${id}${qs({ range })}`),
  setManaged: (id: number, managed: boolean, reason?: string) =>
    call<Campaign>("PUT", `/campaigns/${id}/managed`, { managed, reason: reason ?? null }),
  metrics: (q: {
    entity_type: EntityType;
    entity_id: number | string;
    grain: "day" | "hour";
    from?: string;
    to?: string;
  }) => call<MetricsSeries>("GET", `/metrics${qs(q)}`),
  alerts: async (q: { open?: boolean; severity?: string } = {}) =>
    unwrapList<Alert>(await call("GET", `/alerts${qs({ open: true, ...q })}`)),
  emergencyActions: async (activeOnly = true) =>
    unwrapList<EmergencyAction>(await call("GET", `/emergency-actions${qs({ active_only: activeOnly })}`)),
  system: () => call<SystemStatus>("GET", "/system"),
  rules: () => call<ConfigResponse>("GET", "/rules"),
  limits: () => call<ConfigResponse>("GET", "/limits"),

  /* Phase 2 */
  rankings: (level: RankLevel, platform?: Platform) =>
    call<Rankings>("GET", `/rankings${qs({ level, platform })}`),
  proposals: async (status?: ProposalStatus) =>
    unwrapList<Proposal>(await call("GET", `/proposals${qs({ status })}`)),
  proposal: (id: number) => call<Proposal>("GET", `/proposals/${id}`),
  approve: (id: number, comment?: string) =>
    call<DecisionResponse>("POST", `/proposals/${id}/approve`, { comment: comment ?? null }),
  reject: (id: number, reason: string) => call<DecisionResponse>("POST", `/proposals/${id}/reject`, { reason }),
  approveBatch: (ids: number[], comment?: string) =>
    call<BatchDecisionResponse>("POST", "/proposals/approve-batch", { ids, comment: comment ?? null }),

  /* Phase 3 (501 until the executor ships) */
  dryRun: (id: number) => call<Record<string, unknown>>("GET", `/proposals/${id}/dry-run`),
  changes: async (since?: string) => unwrapList<Change>(await call("GET", `/changes${qs({ since })}`)),
  rollback: (id: number) => call<unknown>("POST", `/changes/${id}/rollback`),

  /* Phase 4 */
  llmUsage: (range = "30d") => call<LlmUsage>("GET", `/llm/usage${qs({ range })}`),
};

export type Api = typeof api;
