"use client";

import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatCompact, formatDay, formatMoney, formatNumber, formatRoas, localeFor, toNum } from "@/lib/format";
import type { PaceRow, TrendRow } from "@/lib/overview";
import type { LlmUsageDay, MetricPoint, Money } from "@/lib/types";

const C = { meta: "#2563eb", google: "#f59e0b", actual: "#0f172a", expected: "#94a3b8", target: "#ef4444", conv: "#10b981", small: "#38bdf8", large: "#8b5cf6" };
const axis = { fontSize: 11, fill: "#64748b" };
const grid = <CartesianGrid stroke="#f1f5f9" vertical={false} />;

type TipFormatter = (v: unknown) => string;
const tip = (f: TipFormatter) => ({
  formatter: (v: unknown) => f(v),
  labelStyle: { fontSize: 12, color: "#0f172a" },
  contentStyle: { fontSize: 12, borderRadius: 8, borderColor: "#e2e8f0" },
});

export function PaceChart({
  data: rows,
  dailyCap,
  currency,
  curveSource = "linear",
}: {
  data: PaceRow[];
  dailyCap: Money | null | undefined;
  currency?: string;
  curveSource?: "learned" | "linear" | "mixed";
}) {
  const cap = toNum(dailyCap);
  const m = (v: unknown) => formatMoney(v as number, currency);
  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey="hour" tick={axis} interval={3} />
        <YAxis tick={axis} tickFormatter={(v) => formatCompact(v, localeFor(currency))} width={48} />
        <Tooltip {...tip(m)} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Area isAnimationActive={false} type="monotone" dataKey="actual" name="Actual (cumulative)" stroke={C.actual} fill="#0f172a" fillOpacity={0.08} strokeWidth={2} connectNulls={false} />
        <Line isAnimationActive={false} type="monotone" dataKey="expected" name={curveSource === "learned" ? "Expected (learned curve × cap)" : curveSource === "mixed" ? "Expected (curve × cap)" : "Expected (linear h/24 × cap)"} stroke={C.expected} strokeDasharray="5 4" dot={false} strokeWidth={2} />
        {cap !== null && <ReferenceLine ifOverflow="extendDomain" y={cap} stroke={C.target} strokeDasharray="3 3" label={{ value: "Daily cap", fontSize: 11, fill: C.target, position: "insideTopRight" }} />}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function KpiTrendChart({
  data,
  metric,
  target,
  currency,
}: {
  data: TrendRow[];
  metric: "cpa" | "roas";
  target: Money | number | string | null | undefined;
  currency?: string;
}) {
  const rows = data.map((p) => ({ date: formatDay(p.date), value: metric === "cpa" ? p.cpa : p.roas }));
  const t = toNum(target);
  const f = metric === "cpa" ? (v: unknown) => formatMoney(v as number, currency) : (v: unknown) => formatRoas(v as number);
  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey="date" tick={axis} interval="preserveStartEnd" minTickGap={24} />
        <YAxis tick={axis} width={52} tickFormatter={(v) => (metric === "cpa" ? formatCompact(v, localeFor(currency)) : `${v}×`)} />
        <Tooltip {...tip(f)} />
        <Line isAnimationActive={false} type="monotone" dataKey="value" name={metric === "cpa" ? "CPA" : "ROAS"} stroke={C.actual} strokeWidth={2} dot={false} connectNulls />
        {t !== null && <ReferenceLine ifOverflow="extendDomain" y={t} stroke={C.target} strokeDasharray="4 4" label={{ value: "Target", fontSize: 11, fill: C.target, position: "insideTopRight" }} />}
      </LineChart>
    </ResponsiveContainer>
  );
}

export function PlatformSplitChart({ data, currency }: { data: TrendRow[]; currency?: string }) {
  const rows = data.map((p) => ({ date: formatDay(p.date), meta: p.meta_spend, google: p.google_spend }));
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey="date" tick={axis} interval="preserveStartEnd" minTickGap={24} />
        <YAxis tick={axis} width={48} tickFormatter={(v) => formatCompact(v, localeFor(currency))} />
        <Tooltip {...tip((v) => formatMoney(v as number, currency))} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar isAnimationActive={false} dataKey="meta" name="Meta" stackId="s" fill={C.meta} />
        <Bar isAnimationActive={false} dataKey="google" name="Google" stackId="s" fill={C.google} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function EntityMetricsChart({ data, currency, targetCpa }: { data: MetricPoint[]; currency?: string; targetCpa?: Money | null }) {
  const rows = data.map((p) => {
    const spend = toNum(p.spend) ?? 0;
    const conv = toNum(p.conversions) ?? 0;
    return { date: formatDay(p.date ?? (p.ts_hour ?? "").slice(0, 10)), spend, conversions: conv, cpa: conv > 0 ? spend / conv : null };
  });
  const t = toNum(targetCpa ?? null);
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div>
        <div className="mb-1 text-xs font-medium text-slate-500">Spend and conversions</div>
        <ResponsiveContainer width="100%" height={220}>
          <ComposedChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            {grid}
            <XAxis dataKey="date" tick={axis} interval="preserveStartEnd" minTickGap={24} />
            <YAxis yAxisId="l" tick={axis} width={48} tickFormatter={(v) => formatCompact(v, localeFor(currency))} />
            <YAxis yAxisId="r" orientation="right" tick={axis} width={32} />
            <Tooltip
              formatter={(v: unknown, n: unknown) => (n === "Spend" ? formatMoney(v as number, currency) : formatNumber(v as number, 1))}
              contentStyle={{ fontSize: 12, borderRadius: 8 }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar isAnimationActive={false} yAxisId="l" dataKey="spend" name="Spend" fill="#cbd5e1" radius={[3, 3, 0, 0]} />
            <Line isAnimationActive={false} yAxisId="r" type="monotone" dataKey="conversions" name="Conversions" stroke={C.conv} strokeWidth={2} dot={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-slate-500">Daily CPA</div>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            {grid}
            <XAxis dataKey="date" tick={axis} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={axis} width={48} tickFormatter={(v) => formatCompact(v, localeFor(currency))} />
            <Tooltip {...tip((v) => formatMoney(v as number, currency))} />
            <Line isAnimationActive={false} type="monotone" dataKey="cpa" name="CPA" stroke={C.actual} strokeWidth={2} dot={false} connectNulls />
            {t !== null && <ReferenceLine ifOverflow="extendDomain" y={t} stroke={C.target} strokeDasharray="4 4" label={{ value: "Target", fontSize: 11, fill: C.target, position: "insideTopRight" }} />}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/** Stacked input+output tokens per day by tier (LLMUsageDay rows are per date × tier). */
export function TokensChart({ days, tokenCap }: { days: LlmUsageDay[]; tokenCap?: number | null }) {
  const byDate = new Map<string, { date: string; small: number; large: number }>();
  for (const d of days) {
    const r = byDate.get(d.date) ?? { date: d.date, small: 0, large: 0 };
    const t = d.input_tokens + d.output_tokens;
    if (d.tier === "large") r.large += t;
    else r.small += t;
    byDate.set(d.date, r);
  }
  const rows = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date)).map((r) => ({ ...r, date: formatDay(r.date) }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey="date" tick={axis} interval="preserveStartEnd" minTickGap={24} />
        <YAxis tick={axis} width={48} tickFormatter={(v) => formatCompact(v)} />
        <Tooltip {...tip((v) => `${formatNumber(v as number)} tokens`)} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar isAnimationActive={false} dataKey="small" name="Small model" stackId="t" fill={C.small} />
        <Bar isAnimationActive={false} dataKey="large" name="Large model" stackId="t" fill={C.large} radius={[3, 3, 0, 0]} />
        {tokenCap ? <ReferenceLine ifOverflow="extendDomain" y={tokenCap} stroke={C.target} strokeDasharray="4 4" label={{ value: "Daily token cap", fontSize: 11, fill: C.target, position: "insideTopRight" }} /> : null}
      </BarChart>
    </ResponsiveContainer>
  );
}
