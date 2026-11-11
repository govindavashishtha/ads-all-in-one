"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatMoney, formatNumber, humanize } from "@/lib/format";
import type { Campaign, Platform } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { PlatformBadge, StatusBadge } from "@/components/badges";
import { CpaCell, LearningCell, RoasCell, ScoreCell } from "@/components/EntityCells";
import { ExampleTag } from "@/components/MockBanner";
import { hasRole, useSession } from "@/components/session";
import { QueryState } from "@/components/states";
import { Badge, Card, CardHeader, EmptyState, PageHeader, Td, Th } from "@/components/ui";

function ManagedToggle({ c, canEdit, onChanged }: { c: Campaign; canEdit: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function toggle() {
    const reason = window.prompt(`${c.managed ? "Opt OUT" : "Opt IN"} “${c.name ?? c.id}” ${c.managed ? "from" : "to"} automation? Reason (audited):`, "");
    if (reason === null) return;
    setBusy(true);
    setErr(null);
    try {
      await api.setManaged(c.id, !c.managed, reason || undefined);
      onChanged();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        role="switch"
        aria-checked={c.managed}
        aria-label={`Managed: ${c.name ?? c.id}`}
        disabled={!canEdit || busy}
        onClick={toggle}
        title={canEdit ? "Opt in/out of automation (admin)" : "Admin role required"}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-60 ${c.managed ? "bg-emerald-500" : "bg-slate-300"}`}
      >
        <span className={`inline-block h-4 w-4 rounded-full bg-white shadow transition ${c.managed ? "translate-x-4" : "translate-x-0.5"}`} />
      </button>
      {err && <span className="text-xs text-red-600">{err}</span>}
    </span>
  );
}

export default function CampaignsPage() {
  const s = useSession();
  const q = useApi(() => api.campaigns({ range: "7d" }), []);
  const [platform, setPlatform] = useState<Platform | "all">("all");
  const [onlyManaged, setOnlyManaged] = useState(false);
  const cur = s.currency;
  const canEdit = hasRole(s.user, "admin");

  const filter = useMemo(
    () => (list: Campaign[]) =>
      list.filter((c) => (platform === "all" || c.platform === platform) && (!onlyManaged || c.managed)),
    [platform, onlyManaged],
  );

  return (
    <>
      <PageHeader
        title="Campaigns"
        subtitle="Latest 7-day metrics and scores. Only managed campaigns can be changed by the engine."
        right={
          <div className="flex items-center gap-3 text-sm">
            <select
              aria-label="Platform"
              value={platform}
              onChange={(e) => setPlatform(e.target.value as Platform | "all")}
              className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5"
            >
              <option value="all">All platforms</option>
              <option value="meta">Meta</option>
              <option value="google">Google</option>
            </select>
            <label className="flex items-center gap-1.5 text-slate-600">
              <input type="checkbox" checked={onlyManaged} onChange={(e) => setOnlyManaged(e.target.checked)} /> Managed only
            </label>
          </div>
        }
      />
      <QueryState state={q}>
        {(all) => {
          const list = filter(all);
          return (
            <Card>
              <CardHeader title={`${list.length} campaigns`} right={<ExampleTag />} />
              {list.length === 0 ? (
                <EmptyState title="No campaigns">Campaigns appear after the first ingestion run.</EmptyState>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-100">
                    <thead className="bg-slate-50/60">
                      <tr>
                        <Th>Campaign</Th>
                        <Th>Status</Th>
                        <Th>Managed</Th>
                        <Th>Kind</Th>
                        <Th>Budget level</Th>
                        <Th className="text-right">Daily budget</Th>
                        <Th className="text-right">Spend 7d</Th>
                        <Th className="text-right">Conv. 7d</Th>
                        <Th className="text-right">CPA</Th>
                        <Th className="text-right">ROAS</Th>
                        <Th>Score</Th>
                        <Th>Learning</Th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {list.map((c) => (
                        <tr key={c.id} className="hover:bg-slate-50/70">
                          <Td>
                            <div className="flex items-center gap-2">
                              <PlatformBadge platform={c.platform} />
                              <Link href={`/campaigns/${c.id}`} className="font-medium text-slate-900 hover:underline">
                                {c.name ?? `Campaign ${c.id}`}
                              </Link>
                            </div>
                          </Td>
                          <Td><StatusBadge status={c.status} /></Td>
                          <Td><ManagedToggle c={c} canEdit={canEdit} onChanged={q.reload} /></Td>
                          <Td>{c.kind === "standard" ? "Standard" : <Badge tone="violet">{humanize(c.kind)}</Badge>}</Td>
                          <Td>{c.budget_level === "shared" ? <Badge tone="amber" title="Budget changes blocked in v1">Shared</Badge> : humanize(c.budget_level)}</Td>
                          <Td className="text-right tabular-nums">{c.budget_level === "ad_set" ? <span className="text-xs text-slate-400">per ad set</span> : formatMoney(c.daily_budget, cur)}</Td>
                          <Td className="text-right tabular-nums">{formatMoney(c.metrics?.spend, cur)}</Td>
                          <Td className="text-right tabular-nums">{formatNumber(c.metrics?.conversions, 1)}</Td>
                          <Td className="text-right"><CpaCell cpa={c.metrics?.cpa} currency={cur} target={s.targets?.target_cpa} /></Td>
                          <Td className="text-right"><RoasCell roas={c.metrics?.roas} /></Td>
                          <Td><ScoreCell score={c.score} /></Td>
                          <Td><LearningCell learning={c.learning} /></Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          );
        }}
      </QueryState>
    </>
  );
}
