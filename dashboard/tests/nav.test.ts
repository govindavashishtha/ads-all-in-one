import { describe, expect, it } from "vitest";
import { safeNext } from "@/lib/nav";
import { latestRuns } from "@/lib/jobs";
import type { JobRun } from "@/lib/types";

describe("safeNext (post-login redirect)", () => {
  it("only allows app-relative paths", () => {
    expect(safeNext("/campaigns?x=1")).toBe("/campaigns?x=1");
    expect(safeNext(null)).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
  });
});

describe("latestRuns", () => {
  it("keeps the newest run per job+step", () => {
    const j = (id: number, job: string, step: string | null, at: string): JobRun => ({ id, job, step, window_key: "w", status: "success", started_at: at, finished_at: at, rows_ingested: null, error: null });
    const out = latestRuns([j(1, "pacing", null, "2026-11-11T10:00:00Z"), j(2, "pacing", null, "2026-11-11T10:15:00Z"), j(3, "daily_pipeline", "score", "2026-11-11T00:30:00Z")]);
    expect(out.map((r) => r.id)).toEqual([3, 2]);
  });
});
