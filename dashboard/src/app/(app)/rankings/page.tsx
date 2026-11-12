"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { formatMoney, formatNumber, formatPct, formatRoas, humanize, toNum } from "@/lib/format";
import type { Platform, RankLevel, Rankings, Targets } from "@/lib/types";
import { intervalFor, peerGroupLabel, rankingParentLine } from "@/lib/rankings";
import { useApi } from "@/lib/useApi";
import { PlatformBadge, StatusBadge } from "@/components/badges";
import { IntervalBar } from "@/components/charts/IntervalBar";
import { ExampleTag } from "@/components/MockBanner";
import { useSession } from "@/components/session";
import { NotEnabled, QueryState } from "@/components/states";
import { Badge, Button, Card, CardHeader, EmptyState, PageHeader, Td, Th } from "@/components/ui";

const LEVELS: { id: RankLevel; label: string }[] = [
  { id: "ad", label: "Ads" },
  { id: "ad_group", label: "Ad sets / ad groups" },
  { id: "keyword", label: "Keywords" },
  { id: "campaign", label: "Campaigns" },
];

function RankTable({ data, targets, currency, showLow }: { data: Rankings; targets: Targets | undefined; currency?: string; showLow: boolean }) {
  const objective = targets?.objective ?? "conversions";
  const rows = data.rows.filter((r) => showLow || r.data_sufficient !== false);
  if (!rows.length) return <EmptyState title="Nothing to show with these filters" />;
  const target = objective === "value" ? targets?.target_roas : objective === "traffic" ? null : targets?.target_cpa;
  const max = Math.max(...rows.map((r) => toNum(intervalFor(r, objective).high) ?? 0), toNum(target ?? null) ?? 0) * 1.05;
  const fmt = (v: unknown) =>
    objective === "value" ? formatRoas(v as string) : objective === "traffic" ? formatPct(v as string, 2) : formatMoney(v as string, currency);
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-slate-100">
        <thead className="bg-slate-50/60">
          <tr>
            <Th>#</Th>
            <Th>Entity</Th>
            <Th>Status</Th>
            <Th>{objective === "value" ? "ROAS p10 – p90" : objective === "traffic" ? "CTR p10 – p90" : "CPA p10 – p90"}</Th>
            <Th className="text-right">Estimate</Th>
            <Th className="text-right">
              <span title="Settled scoring window (features spend_s14 / conversions_s14)">Spend · conv.</span>
            </Th>
            <Th className="text-right">P(beats target)</Th>
            <Th>Score</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r, i) => {
            const iv = intervalFor(r, objective);
            const header = i === 0 || rows[i - 1].peer_group !== r.peer_group;
            return [
              header ? (
                <tr key={`h-${r.peer_group}-${i}`} className="bg-slate-50">
                  <td colSpan={8} className="px-3 py-1.5 text-xs font-medium text-slate-500">
                    {peerGroupLabel(r.peer_group, r.peer_size)}
                  </td>
                </tr>
              ) : null,
              <tr key={`${r.entity_type}-${r.entity_id}`} className={r.data_sufficient === false ? "opacity-70" : ""}>
                <Td className="tabular-nums text-slate-500">
                  {r.rank_in_peer ?? "—"}/{r.peer_size ?? "—"}
                </Td>
                <Td>
                  <div className="flex items-center gap-2">
                    <PlatformBadge platform={r.platform} />
                    <div className="min-w-0">
                      <div className="max-w-sm truncate font-medium text-slate-900">{r.name ?? `${humanize(r.entity_type)} #${r.entity_id}`}</div>
                      {r.entity_type !== "campaign" && r.campaign_id != null && (
                        <Link href={`/campaigns/${r.campaign_id}`} className="block max-w-sm truncate text-xs text-slate-500 hover:underline">
                          {rankingParentLine(r)}
                        </Link>
                      )}
                    </div>
                    {r.data_sufficient === false && <Badge title="Below minimum data (§10.6); never paused or re-budgeted by a rule">low data</Badge>}
                    {r.fatigue_flag && <Badge tone="amber" title="Creative fatigue (§11.1): high frequency and falling CTR">fatigue</Badge>}
                    {r.learning && <Badge tone="violet">learning</Badge>}
                  </div>
                </Td>
                <Td>{r.status ? <StatusBadge status={r.status} /> : "—"}</Td>
                <Td>
                  <IntervalBar low={iv.low} mid={iv.mid} high={iv.high} target={target} max={max} lowerIsBetter={iv.lowerIsBetter} />
                  <div className="text-[11px] tabular-nums text-slate-400">
                    {fmt(iv.low)} – {fmt(iv.high)}
                  </div>
                </Td>
                <Td className="text-right tabular-nums">{fmt(iv.mid)}</Td>
                <Td className="text-right tabular-nums">
                  {formatMoney(r.spend ?? null, currency)}
                  <div className="text-[11px] text-slate-400">{r.conversions != null ? `${formatNumber(r.conversions, 1)} conv.` : "—"}</div>
                </Td>
                <Td className="text-right tabular-nums">{formatPct(r.p_beats_target, 0)}</Td>
                <Td className="tabular-nums">{r.composite ?? "—"}</Td>
              </tr>,
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Targets the scores were ranked against (from the response), else the session default. */
function targetsOf(data: Rankings, fallback: Targets | undefined): Targets | undefined {
  const t = data.targets;
  if (!t) return fallback;
  return { objective: t.objective ?? null, target_cpa: t.target_cpa ?? null, target_roas: t.target_roas ?? null };
}

function targetLabel(t: Targets | undefined, currency?: string): string {
  if (!t) return "";
  if (t.objective === "value" && t.target_roas != null) return ` · target ROAS ${formatRoas(t.target_roas)}`;
  if (t.objective !== "traffic" && t.target_cpa != null) return ` · target CPA ${formatMoney(t.target_cpa, currency)}`;
  return "";
}

export default function RankingsPage() {
  const s = useSession();
  const [level, setLevel] = useState<RankLevel>("ad");
  const [platform, setPlatform] = useState<Platform | "">("");
  const [showLow, setShowLow] = useState(true);
  const q = useApi(() => api.rankings(level, platform || undefined), [level, platform]);
  return (
    <>
      <PageHeader
        title="Rankings"
        subtitle="Confidence-aware ranking within each peer group, ordered by the pessimistic bound so a lucky small sample never beats a proven one (§10)."
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {LEVELS.map((l) => (
          <Button key={l.id} size="sm" variant={l.id === level ? "primary" : "secondary"} onClick={() => setLevel(l.id)}>
            {l.label}
          </Button>
        ))}
        <select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value as Platform | "")} className="ml-2 rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-sm">
          <option value="">All platforms</option>
          <option value="meta">Meta</option>
          <option value="google">Google</option>
        </select>
        <label className="ml-2 flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={showLow} onChange={(e) => setShowLow(e.target.checked)} /> Show low-data entities
        </label>
      </div>
      <QueryState state={q} feature="Rankings" phase={2}>
        {(data) =>
          data.as_of === null && data.rows.length === 0 ? (
            <NotEnabled feature="Rankings" phase={2}>
              No scores have been built yet. Rankings appear after the daily pipeline&apos;s score step runs (Phase 2).
            </NotEnabled>
          ) : (
            <Card>
              <CardHeader
                title={`${LEVELS.find((l) => l.id === level)?.label} · ${data.rows.length} ranked`}
                subtitle={`Scored as of ${data.as_of ?? "—"} · objective ${humanize(targetsOf(data, s.targets)?.objective ?? "conversions")}${targetLabel(targetsOf(data, s.targets), s.currency)} · bar = p10–p90, tick = estimate, red line = target`}
                right={<ExampleTag />}
              />
              <RankTable data={data} targets={targetsOf(data, s.targets)} currency={s.currency} showLow={showLow} />
            </Card>
          )
        }
      </QueryState>
    </>
  );
}
