"use client";

import Link from "next/link";
import { formatDateTime, formatMoney, formatNumber, formatPct, formatRoas, humanize, relativeTime, toNum } from "@/lib/format";
import { loadOverview, type OverviewView } from "@/lib/overview";
import type { SpendBlock } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { BudgetGauge, CapBar } from "@/components/charts/BudgetGauge";
import { KpiTrendChart, PaceChart, PlatformSplitChart } from "@/components/charts/charts";
import { KillSwitchBadge, ModeBadge, StatusBadge } from "@/components/badges";
import { ExampleTag } from "@/components/MockBanner";
import { QueryState } from "@/components/states";
import { Badge, Card, CardBody, CardHeader, EmptyState, PageHeader, Skeleton, Stat } from "@/components/ui";

const label: Record<SpendBlock["platform"], string> = { total: "Total", meta: "Meta", google: "Google" };

function OverviewBody({ v }: { v: OverviewView }) {
  const { ov, targets, trend } = v;
  const cur = ov.currency;
  const tz = v.timezone;
  const blocks = [ov.total, ...ov.platforms];
  const r = ov.total.range;
  const tCpa = toNum(targets.target_cpa);
  const tRoas = toNum(targets.target_roas);
  const cpa = toNum(r.cpa ?? null);
  const lastSync = Object.values(ov.last_synced_at ?? {}).filter(Boolean).sort()[0] ?? null;
  const ks = ov.kill_switch;

  return (
    <div className="space-y-6">
      {(ov.active_emergency_actions > 0 || ks.effective) && (
        <div role="alert" className="rounded-xl border border-red-300 bg-red-50 px-5 py-3 text-sm text-red-800">
          {ks.effective && <strong>Kill switch is ON{ks.env_override ? " (env override)" : ""}. </strong>}
          {ov.active_emergency_actions > 0 && (
            <span>
              {ov.active_emergency_actions} active emergency action(s)
              {v.emergency.length > 0 &&
                ": " +
                  v.emergency
                    .map((e) => `${humanize(e.platform)} ${humanize(e.trigger)} at ${formatDateTime(e.triggered_at, tz)} (${e.campaign_ids.length} campaigns paused)`)
                    .join("; ")}
            </span>
          )}
        </div>
      )}
      {ov.note && <div className="rounded-lg bg-slate-100 px-4 py-2 text-sm text-slate-600">{ov.note}</div>}

      <Card>
        <CardHeader
          title="Spend today vs daily cap"
          subtitle={`Account day ${ov.total.account_date ?? "—"}${tz ? ` (${tz})` : ""} · ${formatPct(ov.total.elapsed_day_fraction, 0)} of the day elapsed · oldest sync ${relativeTime(lastSync)}`}
          right={<ExampleTag />}
        />
        <CardBody className="grid grid-cols-1 gap-6 sm:grid-cols-3">
          {blocks.map((s) => (
            <BudgetGauge
              key={s.platform}
              label={label[s.platform]}
              spend={s.spend_today}
              cap={s.daily_cap}
              currency={cur}
              sub={s.elapsed_day_fraction != null && s.spent_fraction_today != null ? `${formatPct(s.spent_fraction_today, 0)} spent at ${formatPct(s.elapsed_day_fraction, 0)} of the day` : undefined}
            />
          ))}
        </CardBody>
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Intraday pace (all accounts)" subtitle="Cumulative spend by account-local hour vs a linear reference to the total daily cap" right={<ExampleTag />} />
          <CardBody>
            {v.pace.some((p) => p.actual !== null) ? (
              <PaceChart data={v.pace} dailyCap={ov.total.daily_cap} currency={cur} />
            ) : (
              <EmptyState title="No hourly data for today yet">Hourly metrics arrive with the hourly pull (:05 past each hour).</EmptyState>
            )}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Month to date vs monthly cap" />
          <CardBody className="space-y-4">
            {blocks.map((s) => (
              <CapBar key={s.platform} label={label[s.platform]} spend={s.mtd_spend} cap={s.monthly_cap} currency={cur} />
            ))}
            <div className="text-xs text-slate-500">Limits version {ov.limits_version ?? "—"}</div>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardBody className="grid grid-cols-2 gap-6 md:grid-cols-5">
          <Stat label={`Spend · ${ov.range_days}d`} value={formatMoney(r.spend, cur)} sub={r.from_date && r.to_date ? `${r.from_date} → ${r.to_date}` : undefined} />
          <Stat label={`Conversions · ${ov.range_days}d`} value={formatNumber(r.conversions, 0)} />
          <Stat
            label={`CPA · ${ov.range_days}d`}
            value={formatMoney(r.cpa, cur)}
            sub={tCpa !== null ? `Target ${formatMoney(tCpa, cur)}` : "Target not set (input I-1)"}
            tone={tCpa !== null && cpa !== null && cpa > tCpa * 1.2 ? "warn" : undefined}
          />
          <Stat label={`ROAS · ${ov.range_days}d`} value={formatRoas(r.roas)} sub={tRoas !== null ? `Target ${formatRoas(tRoas)}` : "—"} />
          <Stat label="Open approvals" value={<Link href="/approvals" className="hover:underline">{ov.open_approvals}</Link>} sub="Awaiting a decision" />
        </CardBody>
      </Card>

      {trend.length === 0 ? (
        <Card>
          <EmptyState title="No daily metrics yet">Trends appear after ingestion / the 90-day backfill (Phase 1).</EmptyState>
        </Card>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader title={`CPA · last ${trend.length} days`} subtitle="Daily blended CPA (account level) with the target line" right={<ExampleTag />} />
            <CardBody>
              <KpiTrendChart data={trend} metric="cpa" target={targets.target_cpa} currency={cur} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title={`ROAS · last ${trend.length} days`} subtitle="Conversion value ÷ spend" right={<ExampleTag />} />
            <CardBody>
              <KpiTrendChart data={trend} metric="roas" target={targets.objective === "value" ? targets.target_roas : null} currency={cur} />
            </CardBody>
          </Card>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Meta vs Google spend" right={<ExampleTag />} />
          <CardBody>{trend.length ? <PlatformSplitChart data={trend} currency={cur} /> : <EmptyState title="No data yet" />}</CardBody>
        </Card>
        <Card>
          <CardHeader
            title="Alerts"
            subtitle={`${ov.open_alerts.critical ?? 0} critical · ${ov.open_alerts.warn ?? 0} warn · ${ov.open_alerts.info ?? 0} info (unacknowledged)`}
          />
          <CardBody className="space-y-3">
            {v.alerts.length === 0 && <div className="text-sm text-slate-500">No open warnings or critical alerts.</div>}
            {v.alerts.slice(0, 6).map((a) => (
              <div key={a.id} className="flex items-start gap-2.5">
                <span className="shrink-0 pt-0.5">
                  <StatusBadge status={a.severity} />
                </span>
                <div className="min-w-0">
                  <div className="text-sm text-slate-800">{a.message}</div>
                  <div className="text-xs text-slate-500">
                    {humanize(a.kind)} · {formatDateTime(a.created_at, tz)}
                  </div>
                </div>
              </div>
            ))}
          </CardBody>
        </Card>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <ModeBadge mode={ov.mode} hardMax={ov.hard_max_mode} />
        <KillSwitchBadge state={ks} />
        <Badge>Hard max: {humanize(ov.hard_max_mode)}</Badge>
        <span>Objective: {humanize(targets.objective ?? "not set")}</span>
        {v.warnings.length > 0 && <span className="text-amber-700">· Partial data: {v.warnings.join("; ")}</span>}
      </div>
    </div>
  );
}

export default function OverviewPage() {
  const q = useApi(() => loadOverview(28), [], { refreshMs: 60_000 });
  return (
    <>
      <PageHeader title="Overview" subtitle="Spend against caps, pacing and efficiency across Meta and Google" />
      <QueryState
        state={q}
        skeleton={
          <div className="space-y-6">
            <Skeleton className="h-52" />
            <Skeleton className="h-72" />
          </div>
        }
      >
        {(v) => <OverviewBody v={v} />}
      </QueryState>
    </>
  );
}
