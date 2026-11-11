import type { JobRun } from "./types";

/** Latest run per (job, step), sorted by job, then by start time (pipeline step order). */
export function latestRuns(jobs: JobRun[]): JobRun[] {
  const m = new Map<string, JobRun>();
  for (const j of jobs) {
    const k = `${j.job}|${j.step ?? ""}`;
    const cur = m.get(k);
    if (!cur || j.started_at > cur.started_at) m.set(k, j);
  }
  return [...m.values()].sort((a, b) => a.job.localeCompare(b.job) || a.started_at.localeCompare(b.started_at));
}

/**
 * The API exposes no heartbeat status, so derive one: every successful pacing run pings
 * HEARTBEAT_URL (§21), so the last successful pacing run is the last ping. Pacing runs every
 * 15 min; more than 30 min without one is treated as stale.
 */
export function heartbeatFromRuns(runs: JobRun[], now: Date, staleAfterMs = 30 * 60_000): { last: JobRun | null; healthy: boolean } {
  let last: JobRun | null = null;
  for (const r of runs) {
    if (r.job !== "pacing" || r.status !== "success") continue;
    if (!last || r.started_at > last.started_at) last = r;
  }
  if (!last) return { last: null, healthy: false };
  const t = new Date(last.finished_at ?? last.started_at).getTime();
  return { last, healthy: now.getTime() - t <= staleAfterMs };
}
