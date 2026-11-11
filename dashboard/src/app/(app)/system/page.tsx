"use client";

import { api } from "@/lib/api";
import { formatDateTime, formatNumber, humanize, relativeTime } from "@/lib/format";
import { latestRuns } from "@/lib/jobs";
import type { JobRun } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { KillSwitchBadge, ModeBadge, PlatformBadge, StatusBadge } from "@/components/badges";
import { ExampleTag } from "@/components/MockBanner";
import { hasRole, useSession } from "@/components/session";
import { RunJobCard } from "@/components/RunJobCard";
import { QueryState } from "@/components/states";
import { Badge, Card, CardBody, CardHeader, EmptyState, PageHeader, Td, Th } from "@/components/ui";

function duration(j: JobRun): string {
  if (!j.finished_at) return "running";
  const s = (new Date(j.finished_at).getTime() - new Date(j.started_at).getTime()) / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${(s / 60).toFixed(1)} min`;
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-slate-500">{k}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

export default function SystemPage() {
  const s = useSession();
  const tz = s.timezone;
  const q = useApi(() => api.system(), [], { refreshMs: 30_000 });
  return (
    <>
      <PageHeader title="System" subtitle="Job health, data freshness, connector status and platform API usage (§21)." right={<ExampleTag />} />
      <QueryState state={q}>
        {(sys) => {
          const hb = sys.heartbeat;
          const ops = sys.platform_operations;
          return (
            <div className="space-y-6">
              <div className="grid gap-6 lg:grid-cols-3">
                <Card>
                  <CardHeader title="Control state" subtitle="Changed via CLI / API (admin)" />
                  <CardBody className="space-y-3 text-sm">
                    <Row k="Mode"><ModeBadge mode={sys.mode} hardMax={sys.hard_max_mode} /></Row>
                    <Row k="Hard max mode (env)">{humanize(sys.hard_max_mode)}</Row>
                    <Row k="Kill switch"><KillSwitchBadge state={sys.kill_switch} /></Row>
                    <Row k="Kill switch sources">
                      <span className="text-xs">DB {sys.kill_switch.db ? "on" : "off"} · env {sys.kill_switch.env_override ? "on" : "off"}</span>
                    </Row>
                    <Row k="Environment">{sys.app_env}</Row>
                    <Row k="Auth mode">{sys.auth_mode}</Row>
                    <Row k="Client"><span className="font-mono text-xs">{sys.client_id}</span></Row>
                  </CardBody>
                </Card>
                <Card>
                  <CardHeader title="Heartbeat" subtitle="The pacing job pings HEARTBEAT_URL after every run (§21 dead-man's switch)" />
                  <CardBody className="space-y-3 text-sm" data-testid="heartbeat">
                    <Row k="Health"><StatusBadge status={hb.healthy ? "healthy" : "stale"} /></Row>
                    <Row k="Last ping">
                      {hb.last_status === "unknown" ? (
                        <Badge tone="amber">not reported yet</Badge>
                      ) : hb.last_status === "not_configured" ? (
                        <Badge tone="slate">HEARTBEAT_URL not set</Badge>
                      ) : (
                        <span>
                          <StatusBadge status={hb.last_status} /> {hb.last_ping_at ? relativeTime(hb.last_ping_at) : ""}
                        </span>
                      )}
                    </Row>
                    <Row k="Last pacing success">
                      {hb.last_pacing_success_at ? `${relativeTime(hb.last_pacing_success_at)} (${formatDateTime(hb.last_pacing_success_at, tz)})` : "never"}
                    </Row>
                    {hb.error && <div className="text-xs text-red-600">{hb.error}</div>}
                    <Row k="Server time">{formatDateTime(sys.server_time, tz)}</Row>
                  </CardBody>
                </Card>
                <Card>
                  <CardHeader title="Recent failures" subtitle="Failed job runs, last 7 days" />
                  <CardBody className="space-y-2 text-sm">
                    {sys.recent_failures.length === 0 ? (
                      <span className="text-slate-500">None</span>
                    ) : (
                      sys.recent_failures.slice(0, 4).map((f) => (
                        <div key={f.id}>
                          <div className="font-medium text-red-700">{f.job}{f.step ? ` · ${f.step}` : ""}</div>
                          <div className="text-xs text-slate-500">{formatDateTime(f.started_at, tz)} — {f.error ?? "no message"}</div>
                        </div>
                      ))
                    )}
                  </CardBody>
                </Card>
              </div>

              <div className="grid gap-6 lg:grid-cols-3">
                <Card className="lg:col-span-2">
                  <CardHeader title="Ad accounts" subtitle="Data freshness (stale = no sync for over 2 h)" />
                  <table className="min-w-full divide-y divide-slate-100">
                    <thead className="bg-slate-50/60">
                      <tr>
                        <Th>Account</Th>
                        <Th>Status</Th>
                        <Th>Currency / timezone</Th>
                        <Th>Last sync</Th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {sys.ad_accounts.length === 0 && (
                        <tr><Td colSpan={4} className="text-slate-500">No ad accounts connected yet</Td></tr>
                      )}
                      {sys.ad_accounts.map((a) => (
                        <tr key={a.id}>
                          <Td>
                            <div className="flex items-center gap-2">
                              <PlatformBadge platform={a.platform} />
                              <span className="font-medium">{a.name ?? a.external_id}</span>
                              <span className="font-mono text-xs text-slate-400">{a.external_id}</span>
                            </div>
                          </Td>
                          <Td>{a.status ? <StatusBadge status={a.status} /> : "—"}</Td>
                          <Td className="text-xs">{a.currency} · {a.timezone}</Td>
                          <Td>
                            {a.last_synced_at ? relativeTime(a.last_synced_at) : "never"}
                            {a.stale && <span className="ml-2"><Badge tone="red">stale</Badge></span>}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
                <Card>
                  <CardHeader title="Platform API operations today" subtitle="Google alerts at 80% of the access level's daily limit (§4.3)" />
                  <CardBody className="space-y-4 text-sm">
                    {ops.map((o) => {
                      const pct = o.fraction ?? null;
                      return (
                        <div key={o.platform} data-testid={`ops-${o.platform}`}>
                          <div className="flex justify-between">
                            <span className="font-medium">{humanize(o.platform)}</span>
                            <span className="tabular-nums">
                              {o.used == null ? "not recorded today" : formatNumber(o.used)}
                              {o.limit ? ` / ${formatNumber(o.limit)}` : ""}
                            </span>
                          </div>
                          {pct !== null && (
                            <div className="mt-1 h-1.5 rounded-full bg-slate-100">
                              <div className={`h-full rounded-full ${pct >= 0.8 ? "bg-red-500" : "bg-sky-500"}`} style={{ width: `${Math.min(pct * 100, 100)}%` }} />
                            </div>
                          )}
                          <div className="mt-0.5 text-xs text-slate-400">
                            {o.access_level ?? ""}
                            {o.updated_at ? ` · reported ${relativeTime(o.updated_at)}` : ""}
                          </div>
                        </div>
                      );
                    })}
                  </CardBody>
                </Card>
              </div>

              <Card>
                <CardHeader title="Jobs (latest run per job and step)" />
                {sys.latest_job_runs.length === 0 ? (
                  <EmptyState title="No job runs yet" />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-slate-100">
                      <thead className="bg-slate-50/60">
                        <tr>
                          <Th>Job</Th>
                          <Th>Step</Th>
                          <Th>Status</Th>
                          <Th>Window</Th>
                          <Th>Started</Th>
                          <Th>Duration</Th>
                          <Th className="text-right">Rows</Th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {latestRuns(sys.latest_job_runs).map((j) => (
                          <tr key={j.id}>
                            <Td className="font-medium">{j.job}</Td>
                            <Td>{j.step ?? "—"}</Td>
                            <Td><StatusBadge status={j.status} /></Td>
                            <Td className="font-mono text-xs">{j.window_key}</Td>
                            <Td>{formatDateTime(j.started_at, tz)} <span className="text-xs text-slate-400">({relativeTime(j.started_at)})</span></Td>
                            <Td>{duration(j)}</Td>
                            <Td className="text-right tabular-nums">{j.rows_ingested ?? "—"}</Td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              <RunJobCard
                jobs={sys.manual_jobs}
                requests={sys.recent_job_requests ?? []}
                canRun={hasRole(s.user, "admin")}
                timezone={tz}
                onQueued={() => q.reload()}
              />
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
