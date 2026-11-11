import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, createFetchTransport, qs, readCookie, setCsrfToken, setTransport, unwrapList } from "@/lib/api";

function res(status: number, body: unknown) {
  return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

afterEach(() => {
  setTransport(null);
  setCsrfToken(null);
  document.cookie = "aa_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

describe("fetch transport", () => {
  it("sends cookies, and the CSRF header only on state-changing requests", async () => {
    document.cookie = "aa_csrf=tok123; path=/";
    const f = vi.fn(async () => res(200, { ok: true }));
    const t = createFetchTransport("/ads-autopilot/api/v1", f as unknown as typeof fetch);
    await t("GET", "/overview");
    await t("POST", "/proposals/1/approve", { comment: null });
    const [getUrl, getInit] = f.mock.calls[0] as unknown as [string, RequestInit];
    const [, postInit] = f.mock.calls[1] as unknown as [string, RequestInit];
    expect(getUrl).toBe("/ads-autopilot/api/v1/overview");
    expect(getInit.credentials).toBe("include");
    expect((getInit.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((postInit.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    expect(postInit.body).toBe(JSON.stringify({ comment: null }));
  });

  it("falls back to the CSRF token returned by login", async () => {
    const f = vi.fn(async () => res(200, {}));
    setCsrfToken("from-login");
    await createFetchTransport("", f as unknown as typeof fetch)("POST", "/x");
    expect(((f.mock.calls[0] as unknown as [string, RequestInit])[1].headers as Record<string, string>)["X-CSRF-Token"]).toBe("from-login");
  });

  it("maps errors: 401 unauthorized, 404 not enabled, FastAPI detail message", async () => {
    const t401 = createFetchTransport("", (async () => res(401, { detail: "Not authenticated" })) as unknown as typeof fetch);
    const e1 = (await t401("GET", "/auth/me").catch((e) => e)) as ApiError;
    expect(e1).toBeInstanceOf(ApiError);
    expect(e1.unauthorized).toBe(true);
    expect(e1.message).toBe("Not authenticated");
    const t404 = createFetchTransport("", (async () => res(404, { detail: "Not Found" })) as unknown as typeof fetch);
    expect(((await t404("GET", "/rankings").catch((e) => e)) as ApiError).notEnabled).toBe(true);
    const t422 = createFetchTransport("", (async () => res(422, { detail: [{ msg: "bad level" }] })) as unknown as typeof fetch);
    expect(((await t422("GET", "/rankings").catch((e) => e)) as ApiError).message).toBe("bad level");
  });
});

describe("helpers", () => {
  it("unwraps both list envelopes", () => {
    expect(unwrapList([1, 2])).toEqual([1, 2]);
    expect(unwrapList({ items: [3] })).toEqual([3]);
    expect(unwrapList(null)).toEqual([]);
  });
  it("builds query strings without empty values", () => {
    expect(qs({ level: "ad", platform: undefined, x: "" })).toBe("?level=ad");
    expect(qs({})).toBe("");
  });
  it("reads cookies", () => {
    expect(readCookie("a", "x=1; a=b%20c")).toBe("b c");
    expect(readCookie("z", "x=1")).toBeNull();
  });
});

describe("api.proposal / not_enabled", () => {
  it("reads a single proposal with GET /proposals/{id}", async () => {
    const calls: string[] = [];
    setTransport(async (m, path) => {
      calls.push(`${m} ${path}`);
      return { id: 7, action: "pause" };
    });
    expect((await api.proposal(7)).action).toBe("pause");
    expect(calls).toEqual(["GET /proposals/7"]);
  });
  it("exposes the phase of a 501 not_enabled answer", () => {
    const e = new ApiError(501, "not yet", { detail: { code: "not_enabled", phase: "3", message: "not yet" } });
    expect(e.notEnabled).toBe(true);
    expect(e.phase).toBe("3");
  });
});
