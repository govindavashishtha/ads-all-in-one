/**
 * Overview page composition (§18). `GET /overview` gives caps, today's spend and range KPIs;
 * the time series come from `GET /metrics` at account level (entity_type=account,
 * entity_id = ad_accounts.id from `GET /system`), targets from the active rules version.
 */
import { api, ApiError, targetsFromRules } from "./api";
import { addDaysIso, toNum } from "./format";
import type { AdAccount, Alert, EmergencyAction, MetricPoint, Overview, Platform, Targets } from "./types";

export interface TrendRow {
  date: string;
  spend: number;
  conversions: number;
  conv_value: number;
  cpa: number | null;
  roas: number | null;
  meta_spend: number;
  google_spend: number;
}

export interface PaceRow {
  hour: string;
  actual: number | null;
  expected: number | null;
}

/** Sum account-level daily series across platforms; CPA/ROAS recomputed from the sums. */
export function buildTrend(series: { platform: Platform; points: MetricPoint[] }[]): TrendRow[] {
  const byDate = new Map<string, TrendRow>();
  for (const s of series) {
    for (const p of s.points) {
      if (!p.date) continue;
      const r =
        byDate.get(p.date) ??
        { date: p.date, spend: 0, conversions: 0, conv_value: 0, cpa: null, roas: null, meta_spend: 0, google_spend: 0 };
      const spend = toNum(p.spend) ?? 0;
      r.spend += spend;
      r.conversions += toNum(p.conversions) ?? 0;
      r.conv_value += toNum(p.conv_value) ?? 0;
      if (s.platform === "meta") r.meta_spend += spend;
      else r.google_spend += spend;
      byDate.set(p.date, r);
    }
  }
  return [...byDate.values()]
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((r) => ({
      ...r,
      cpa: r.conversions > 0 ? r.spend / r.conversions : null,
      roas: r.spend > 0 && r.conv_value > 0 ? r.conv_value / r.spend : null,
    }));
}

/**
 * Cumulative intraday spend by account-local hour (ts_hour is naive local, §8.3) against a
 * linear reference `h/24 × daily cap`. The plan's learned curve (§12.4) is the pacing job's
 * and is not exposed by the API yet, so the reference is the plan's own fallback.
 */
export function buildPace(hourly: MetricPoint[][], dailyCap: number | null, elapsedFraction: number | null): PaceRow[] {
  const perHour = new Array<number>(24).fill(0);
  let maxHour = -1;
  for (const pts of hourly) {
    for (const p of pts) {
      const h = p.ts_hour ? Number(p.ts_hour.slice(11, 13)) : NaN;
      if (!Number.isInteger(h) || h < 0 || h > 23) continue;
      perHour[h] += toNum(p.spend) ?? 0;
      maxHour = Math.max(maxHour, h);
    }
  }
  const nowHour = elapsedFraction != null ? Math.min(23, Math.floor(elapsedFraction * 24)) : maxHour;
  let cum = 0;
  return perHour.map((v, h) => {
    cum += v;
    return {
      hour: `${String(h).padStart(2, "0")}:00`,
      actual: h <= nowHour ? cum : null,
      expected: dailyCap != null ? (dailyCap * (h + 1)) / 24 : null,
    };
  });
}

export interface OverviewView {
  ov: Overview;
  accounts: AdAccount[];
  timezone: string | undefined;
  targets: Targets;
  trend: TrendRow[];
  pace: PaceRow[];
  alerts: Alert[];
  emergency: EmergencyAction[];
  /** Secondary calls that failed (the page still renders). */
  warnings: string[];
}

function settledValue<T>(r: PromiseSettledResult<T>, label: string, warnings: string[]): T | undefined {
  if (r.status === "fulfilled") return r.value;
  if (r.reason instanceof ApiError && r.reason.unauthorized) throw r.reason;
  warnings.push(`${label}: ${r.reason instanceof Error ? r.reason.message : "failed"}`);
  return undefined;
}

export async function loadOverview(trendDays = 28): Promise<OverviewView> {
  const warnings: string[] = [];
  const [ovR, sysR, rulesR, alertsR] = await Promise.allSettled([api.overview("7d"), api.system(), api.rules(), api.alerts({ open: true })]);
  if (ovR.status === "rejected") throw ovR.reason;
  const ov = ovR.value;
  const sys = settledValue(sysR, "System", warnings);
  const rules = settledValue(rulesR, "Rules", warnings);
  const alerts = settledValue(alertsR, "Alerts", warnings) ?? [];
  const accounts = sys?.ad_accounts ?? [];

  const series = await Promise.allSettled(
    accounts.map(async (a) => {
      const today = ov.platforms.find((p) => p.platform === a.platform)?.account_date ?? ov.total.account_date;
      if (!today) return { a, day: [] as MetricPoint[], hour: [] as MetricPoint[] };
      const [day, hour] = await Promise.all([
        api.metrics({ entity_type: "account", entity_id: a.id, grain: "day", from: addDaysIso(today, -trendDays), to: addDaysIso(today, -1) }),
        api.metrics({ entity_type: "account", entity_id: a.id, grain: "hour", from: today, to: today }),
      ]);
      return { a, day: day.points, hour: hour.points };
    }),
  );
  const ok = series.flatMap((r) => (r.status === "fulfilled" ? [r.value] : (settledValue(r, "Metrics", warnings), [])));

  let emergency: EmergencyAction[] = [];
  if (ov.active_emergency_actions > 0) {
    emergency = settledValue(await Promise.allSettled([api.emergencyActions(true)]).then((x) => x[0]), "Emergency actions", warnings) ?? [];
  }

  return {
    ov,
    accounts,
    timezone: accounts[0]?.timezone,
    targets: targetsFromRules(rules),
    trend: buildTrend(ok.map((s) => ({ platform: s.a.platform, points: s.day }))),
    pace: buildPace(ok.map((s) => s.hour), toNum(ov.total.daily_cap), ov.total.elapsed_day_fraction ?? null),
    alerts: alerts.filter((a) => a.severity !== "info"),
    emergency,
    warnings,
  };
}
