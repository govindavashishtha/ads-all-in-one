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
