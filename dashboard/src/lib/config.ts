/**
 * Build-time configuration (BUILD_PLAN §7.2, §18). NEXT_PUBLIC_* values are inlined by
 * `next build`; changing them requires a rebuild (Docker build args).
 */
function trimSlash(v: string): string {
  return v === "/" ? "" : v.replace(/\/+$/, "");
}

export const BASE_PATH = trimSlash(process.env.NEXT_PUBLIC_BASE_PATH ?? "/ads-autopilot");
export const API_URL = trimSlash(process.env.NEXT_PUBLIC_API_URL ?? `${BASE_PATH}/api/v1`);
/** Mock mode: no backend, every page renders clearly-labelled EXAMPLE data. */
// Written as plain comparisons so the bundler constant-folds it and the mock module is
// dropped from non-mock builds.
export const MOCK_API = process.env.NEXT_PUBLIC_MOCK_API === "true" || process.env.NEXT_PUBLIC_MOCK_API === "1";
/** Non-httpOnly cookie the API sets for double-submit CSRF (§17). */
export const CSRF_COOKIE = process.env.NEXT_PUBLIC_CSRF_COOKIE ?? "aa_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
