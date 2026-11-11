"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, formatPct, humanize } from "@/lib/format";
import type { Change } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { PlatformBadge, StatusBadge } from "@/components/badges";
import { ExampleTag } from "@/components/MockBanner";
import { EntityLabel } from "@/components/entityNames";
import { fmtValue } from "@/components/ProposalCard";
import { hasRole, useSession } from "@/components/session";
import { QueryState } from "@/components/states";
import { Badge, Button, Card, EmptyState, PageHeader } from "@/components/ui";

function Diff({ c, currency }: { c: Change; currency?: string }) {
  if (c.change && c.change.field !== "none") {
    const d = c.change;
    return (
      <div className="text-sm">
        <span className="text-slate-500">{humanize(d.field)}: </span>
        <span className="tabular-nums text-slate-500">{fmtValue(d.field, d.before, currency)}</span>
        <span className="mx-1 text-slate-400">→</span>
        <span className="font-medium tabular-nums text-slate-900">{fmtValue(d.field, d.after, currency)}</span>
        {d.after_is_estimate && <span className="ml-2 text-xs text-slate-400">(intended; no read-back yet)</span>}
      </div>
    );
  }
  const keys = [...new Set([...Object.keys(c.before ?? {}), ...Object.keys(c.intended ?? {})])];
  return (
    <div className="space-y-0.5">
      {keys.map((k) => {
        const mismatch = c.after && c.intended && JSON.stringify(c.after[k]) !== JSON.stringify(c.intended[k]);
        return (
          <div key={k} className="text-sm">
            <span className="text-slate-500">{humanize(k)}: </span>
            <span className="tabular-nums text-slate-500">{fmtValue(k, c.before?.[k], currency)}</span>
            <span className="mx-1 text-slate-400">→</span>
            <span className="font-medium tabular-nums text-slate-900">{fmtValue(k, c.intended?.[k], currency)}</span>
            {mismatch && <span className="ml-2 text-xs text-red-600">read-back: {fmtValue(k, c.after?.[k], currency)}</span>}
          </div>
        );
      })}
    </div>
  );
}

function Verdicts({ c, tz }: { c: Change; tz?: string }) {
  if (!c.evaluations?.length) return <span className="text-xs text-slate-400">{c.rollback_of ? "rollback (not evaluated)" : "no evaluation"}</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {c.evaluations.map((e) => (
        <span key={e.id} title={e.done_at ? `Done ${formatDateTime(e.done_at, tz)}` : `Due ${formatDateTime(e.due_at, tz)}`} className="inline-flex items-center gap-1 text-xs text-slate-600">
          D+{e.horizon_days}
          {e.done_at ? (
            e.verdict ? <StatusBadge status={e.verdict} /> : <Badge>checked</Badge>
          ) : (
            <Badge tone="slate">pending</Badge>
          )}
          {e.p_worse != null && <span className="text-slate-400">p(worse) {formatPct(e.p_worse, 0)}</span>}
        </span>
      ))}
    </div>
  );
}

export default function ChangesPage() {
  const s = useSession();
  const q = useApi(
    async () =>
      (await api.changes()).sort(
        (a, b) => new Date(b.applied_at ?? b.started_at).getTime() - new Date(a.applied_at ?? a.started_at).getTime(),
      ),
    [],
  );
  const [msg, setMsg] = useState<string | null>(null);
  const canRollback = hasRole(s.user, "approver");

  async function rollback(c: Change) {
    if (!window.confirm(`Roll back change #${c.id} on ${humanize(c.entity_type)} #${c.entity_id}? If the entity has drifted, a rollback proposal needing approval is created instead.`)) return;
    try {
      await api.rollback(c.id);
      setMsg(`Rollback of #${c.id} requested.`);
      q.reload();
    } catch (e) {
      setMsg(
        e instanceof ApiError && e.notEnabled
          ? `Rollback is not enabled yet${e.phase ? ` (Phase ${e.phase})` : ""}: ${e.message}`
          : e instanceof ApiError
            ? e.message
            : "Rollback failed",
      );
    }
  }

  return (
    <>
      <PageHeader title="Change log" subtitle="Every write the executor made, with its D+3 safety check and D+7 verdict (§15, §16)." right={<ExampleTag />} />
      {msg && <div role="status" className="mb-4 rounded-lg bg-slate-100 px-4 py-2 text-sm text-slate-700">{msg}</div>}
      <QueryState state={q} feature="Change log" phase={3}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyState title="No changes yet">The engine has not written to any ad account. Writes start after the Phase 3 gate.</EmptyState>
            </Card>
          ) : (
            <Card>
              <ol className="divide-y divide-slate-100">
                {list.map((c) => (
                  <li key={c.id} className="flex flex-wrap items-start gap-4 px-5 py-4">
                    <div className="w-36 shrink-0 text-xs text-slate-500">
                      <div className="font-medium text-slate-700">{formatDateTime(c.applied_at ?? c.started_at, s.timezone)}</div>
                      <div>#{c.id}{c.proposal_id ? ` · proposal ${c.proposal_id}` : ""}</div>
                      <div>{c.applied_by ?? "—"}</div>
                    </div>
                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <PlatformBadge platform={c.platform} />
                        <span className="font-semibold text-slate-900">{humanize(c.action)}</span>
                        <span className="text-slate-500">on</span>
                        <EntityLabel type={c.entity_type} id={c.entity_id} name={c.entity_name} campaignId={c.campaign_id} campaignName={c.campaign_name} />
                        <StatusBadge status={c.status} />
                        {c.verdict && (
                          <span title="Overall verdict">
                            <StatusBadge status={c.verdict} />
                          </span>
                        )}
                        {c.rollback_of && <Badge tone="violet">rollback of #{c.rollback_of}</Badge>}
                      </div>
                      <Diff c={c} currency={s.currency} />
                      <Verdicts c={c} tz={s.timezone} />
                    </div>
                    {canRollback && c.status === "applied" && !c.rollback_of && (
                      <Button size="sm" onClick={() => rollback(c)}>
                        Roll back
                      </Button>
                    )}
                  </li>
                ))}
              </ol>
            </Card>
          )
        }
      </QueryState>
    </>
  );
}
