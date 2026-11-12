"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useState } from "react";
import { api } from "@/lib/api";
import { adFormatLabel, formatMoney, formatNumber, formatPct, humanize, localDateMinus } from "@/lib/format";
import type { AdGroup } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { PlatformBadge, StatusBadge } from "@/components/badges";
import { EntityMetricsChart } from "@/components/charts/charts";
import { CpaCell, LearningCell, RoasCell, ScoreCell } from "@/components/EntityCells";
import { ExampleTag } from "@/components/MockBanner";
import { useSession } from "@/components/session";
import { QueryState } from "@/components/states";
import { Badge, Button, Card, CardBody, CardHeader, EmptyState, PageHeader, Skeleton, Stat, Td, Th } from "@/components/ui";

const RANGES = [14, 28, 90] as const;

function AdsRow({ group, colSpan, currency, target }: { group: AdGroup; colSpan: number; currency?: string; target?: string | number | null }) {
  const q = useApi(() => api.adGroup(group.id), [group.id]);
  return (
    <tr className="bg-slate-50/60">
      <td colSpan={colSpan} className="px-6 py-3">
        <QueryState state={q} skeleton={<Skeleton className="h-16" />}>
          {(g) =>
            !g.ads?.length ? (
              <div className="text-sm text-slate-500">No ads in this {group.kind === "ad_set" ? "ad set" : "ad group"}.</div>
            ) : (
              <table className="min-w-full">
                <thead>
                  <tr>
                    <Th>Ad</Th>
                    <Th>Status</Th>
                    <Th>Format</Th>
                    <Th className="text-right">Spend 7d</Th>
                    <Th className="text-right">CTR</Th>
                    <Th className="text-right">Conv.</Th>
                    <Th className="text-right">CPA</Th>
                    <Th>Score</Th>
                  </tr>
                </thead>
                <tbody>
                  {g.ads.map((a) => (
                    <tr key={a.id}>
                      <Td>
                        <div className="font-medium text-slate-800">{a.name}</div>
                        {a.headline && <div className="max-w-xs truncate text-xs text-slate-500">{a.headline}</div>}
                      </Td>
                      <Td><StatusBadge status={a.status} /></Td>
                      <Td>{a.format ? <span title={a.format}>{adFormatLabel(a.format)}</span> : adFormatLabel(null)}</Td>
                      <Td className="text-right tabular-nums">{formatMoney(a.metrics?.spend, currency)}</Td>
                      <Td className="text-right tabular-nums">{formatPct(a.metrics?.ctr, 2)}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(a.metrics?.conversions, 1)}</Td>
                      <Td className="text-right"><CpaCell cpa={a.metrics?.cpa} currency={currency} target={target} /></Td>
                      <Td><ScoreCell score={a.score} /></Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          }
        </QueryState>
      </td>
    </tr>
  );
}

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const s = useSession();
  const cur = s.currency;
  const [days, setDays] = useState<(typeof RANGES)[number]>(28);
  const [open, setOpen] = useState<number | null>(null);
  const q = useApi(() => api.campaign(id), [id]);
  const m = useApi(
    () => api.metrics({ entity_type: "campaign", entity_id: id, grain: "day", from: localDateMinus(days, s.timezone), to: localDateMinus(1, s.timezone) }),
    [id, days, s.timezone],
  );

  return (
    <>
      <div className="mb-2 text-sm">
        <Link href="/campaigns" className="text-slate-500 hover:text-slate-800">← Campaigns</Link>
      </div>
      <QueryState state={q} skeleton={<Skeleton className="h-96" />}>
        {(c) => (
          <div className="space-y-6">
            <PageHeader
              title={c.name ?? `Campaign ${c.id}`}
              subtitle={
                <span className="flex flex-wrap items-center gap-2">
                  <PlatformBadge platform={c.platform} />
                  <StatusBadge status={c.status} />
                  <Badge tone={c.managed ? "green" : "slate"}>{c.managed ? "Managed" : "Not managed"}</Badge>
                  <Badge>{humanize(c.kind)}</Badge>
                  {c.bid_strategy && <Badge tone="slate">{humanize(c.bid_strategy.toLowerCase())}</Badge>}
                  {c.automation_scope === "campaign_only" && <Badge tone="violet" title="§8.2: campaign-level actions only">Campaign-level only</Badge>}
                  <LearningCell learning={c.learning} />
                  <span className="text-xs text-slate-400">ID {c.external_id}</span>
                </span>
              }
            />
            <Card>
              <CardBody className="grid grid-cols-2 gap-6 md:grid-cols-6">
                <Stat label="Daily budget" value={c.budget_level === "ad_set" ? "Per ad set" : formatMoney(c.daily_budget, cur)} sub={humanize(c.budget_level) + " level"} />
                <Stat label="Spend 7d" value={formatMoney(c.metrics?.spend, cur)} />
                <Stat label="Conversions 7d" value={formatNumber(c.metrics?.conversions, 1)} />
                <Stat label="CPA 7d" value={<CpaCell cpa={c.metrics?.cpa} currency={cur} target={s.targets?.target_cpa} />} sub={s.targets?.target_cpa != null ? `Target ${formatMoney(s.targets.target_cpa, cur)}` : undefined} />
                <Stat label="ROAS 7d" value={<RoasCell roas={c.metrics?.roas} />} />
                <Stat label="Score" value={<ScoreCell score={c.score} />} sub={c.score?.rank_in_peer ? `Rank ${c.score.rank_in_peer} of ${c.score.peer_size} · as of ${c.score.as_of}` : undefined} />
              </CardBody>
            </Card>

            <Card>
              <CardHeader
                title="Daily metrics"
                subtitle="Account-local days, platform-reported conversions (§8.4)"
                right={
                  <div className="flex items-center gap-2">
                    <ExampleTag />
                    {RANGES.map((r) => (
                      <Button key={r} size="sm" variant={r === days ? "primary" : "secondary"} onClick={() => setDays(r)}>
                        {r}d
                      </Button>
                    ))}
                  </div>
                }
              />
              <CardBody>
                <QueryState state={m} skeleton={<Skeleton className="h-56" />}>
                  {(series) =>
                    series.points.length ? (
                      <EntityMetricsChart data={series.points} currency={cur} targetCpa={s.targets?.target_cpa} />
                    ) : (
                      <EmptyState title="No metrics for this range" />
                    )
                  }
                </QueryState>
              </CardBody>
            </Card>

            <Card>
              <CardHeader
                title={c.platform === "meta" ? "Ad sets" : "Ad groups"}
                subtitle={c.automation_scope === "campaign_only" ? "Child entities are not managed for this campaign type." : "Click a row to see its ads"}
              />
              {!c.ad_groups?.length ? (
                <EmptyState title="No ad sets / ad groups">
                  {c.automation_scope === "campaign_only" ? "Performance Max / Advantage+ campaigns are handled at campaign level only." : "None ingested yet."}
                </EmptyState>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-100">
                    <thead className="bg-slate-50/60">
                      <tr>
                        <Th>Name</Th>
                        <Th>Status</Th>
                        <Th className="text-right">Daily budget</Th>
                        <Th className="text-right">Spend 7d</Th>
                        <Th className="text-right">Conv. 7d</Th>
                        <Th className="text-right">CPA</Th>
                        <Th>Score</Th>
                        <Th>Learning</Th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {c.ad_groups.map((g) => (
                        <Fragment key={g.id}>
                          <tr className="cursor-pointer hover:bg-slate-50/70" onClick={() => setOpen(open === g.id ? null : g.id)} aria-expanded={open === g.id}>
                            <Td>
                              <span className="mr-1.5 inline-block w-3 text-slate-400">{open === g.id ? "▾" : "▸"}</span>
                              <span className="font-medium text-slate-900">{g.name ?? `#${g.id}`}</span>
                            </Td>
                            <Td><StatusBadge status={g.status} /></Td>
                            <Td className="text-right tabular-nums">{g.daily_budget == null ? <span className="text-xs text-slate-400">campaign</span> : formatMoney(g.daily_budget, cur)}</Td>
                            <Td className="text-right tabular-nums">{formatMoney(g.metrics?.spend, cur)}</Td>
                            <Td className="text-right tabular-nums">{formatNumber(g.metrics?.conversions, 1)}</Td>
                            <Td className="text-right"><CpaCell cpa={g.metrics?.cpa} currency={cur} target={s.targets?.target_cpa} /></Td>
                            <Td><ScoreCell score={g.score} /></Td>
                            <Td><LearningCell learning={g.learning} /></Td>
                          </tr>
                          {open === g.id && <AdsRow group={g} colSpan={8} currency={cur} target={s.targets?.target_cpa} />}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>
        )}
      </QueryState>
    </>
  );
}
