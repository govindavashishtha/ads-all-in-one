"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { JobRequest } from "@/lib/types";
import { StatusBadge } from "./badges";
import { Button, Card, CardBody, CardHeader } from "./ui";

/**
 * Manual job trigger (POST /jobs/{name}/run, admin). The API only queues a `job_requests` row;
 * the engine scheduler picks it up within about a minute and records the outcome.
 */
export function RunJobCard({
  jobs,
  requests,
  canRun,
  timezone,
  onQueued,
}: {
  jobs: string[];
  requests: JobRequest[];
  canRun: boolean;
  timezone?: string;
  onQueued?: () => void;
}) {
  const [job, setJob] = useState(jobs[0] ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function run() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.runJob(job);
      setMsg({ ok: true, text: `Queued ${r.job} as request #${r.id}. The engine runs it within about a minute.` });
      onQueued?.();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? (e.forbidden ? "Admin role required." : e.message) : "Request failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Run a job now" subtitle="Queued for the engine scheduler (about a minute). Jobs that write still go through the write gate; nothing is sent from this page." />
      <CardBody className="space-y-4 text-sm">
        {canRun ? (
          <div className="flex flex-wrap items-center gap-2">
            <select aria-label="Job" value={job} onChange={(e) => setJob(e.target.value)} className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-sm">
              {jobs.map((j) => (
                <option key={j} value={j}>
                  {j}
                </option>
              ))}
            </select>
            <Button size="sm" variant="primary" disabled={busy || !job} onClick={run}>
              {busy ? "Queuing…" : "Run now"}
            </Button>
          </div>
        ) : (
          <div className="text-xs text-slate-500">Admin role is needed to trigger jobs.</div>
        )}
        {msg && (
          <div role={msg.ok ? "status" : "alert"} className={msg.ok ? "text-slate-700" : "text-red-600"}>
            {msg.text}
          </div>
        )}
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Recent requests</div>
          {requests.length === 0 ? (
            <div className="mt-1 text-slate-500">None yet</div>
          ) : (
            <ul className="mt-1 space-y-1">
              {requests.slice(0, 6).map((r) => (
                <li key={r.id} className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-slate-400">#{r.id}</span>
                  <span className="font-medium">{r.job}</span>
                  <StatusBadge status={r.status} />
                  <span className="text-xs text-slate-500" title={formatDateTime(r.requested_at, timezone)}>
                    by {r.requested_by} {relativeTime(r.requested_at)}
                  </span>
                  {r.error && <span className="text-xs text-red-600">{r.error}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
