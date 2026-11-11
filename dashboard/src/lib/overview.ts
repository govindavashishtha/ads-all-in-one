/**
 * Overview page view model (§18). Everything comes from ONE call, `GET /overview`: caps and
 * today's spend, range KPIs, the daily trend, the intraday pace vs the expected curve (learned
 * from the last 28 days, or h/24), the pacing job's projected end-of-day, targets, open
 * warn/critical alerts and active emergency actions.
 */
import { api } from "./api";
import { toNum } from "./format";
import type { Alert, EmergencyAction, IntradayPoint, Overview, Targets, TrendPoint } from "./types";

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

export function toTrendRows(points: TrendPoint[]): TrendRow[] {
  return points.map((p) => ({
    date: p.date,
    spend: toNum(p.spend) ?? 0,
    conversions: toNum(p.conversions) ?? 0,
    conv_value: toNum(p.conv_value) ?? 0,
    cpa: toNum(p.cpa ?? null),
    roas: p.roas ?? null,
    meta_spend: toNum(p.spend_by_platform?.meta ?? null) ?? 0,
    google_spend: toNum(p.spend_by_platform?.google ?? null) ?? 0,
  }));
}

export function toPaceRows(points: IntradayPoint[] | undefined): PaceRow[] {
  return (points ?? []).map((p) => ({
    hour: `${String(p.hour).padStart(2, "0")}:00`,
    actual: toNum(p.cumulative_spend),
    expected: toNum(p.expected_cumulative),
  }));
}

export function targetsFromOverview(ov: Overview): Targets {
  const t = ov.targets;
  return {
    objective: t?.objective ?? null,
    target_cpa: t?.target_cpa ?? null,
    target_roas: t?.target_roas ?? null,
  };
}

export interface OverviewView {
  ov: Overview;
  timezone: string | undefined;
  targets: Targets;
  trend: TrendRow[];
  pace: PaceRow[];
  alerts: Alert[];
  emergency: EmergencyAction[];
}

export function overviewView(ov: Overview): OverviewView {
  return {
    ov,
    timezone: ov.platforms.find((p) => p.timezone)?.timezone ?? ov.timezone,
    targets: targetsFromOverview(ov),
    trend: toTrendRows(ov.trend ?? []),
    pace: toPaceRows(ov.total.intraday),
    alerts: ov.alerts ?? [],
    emergency: ov.emergency_actions ?? [],
  };
}

export async function loadOverview(trendDays = 28): Promise<OverviewView> {
  return overviewView(await api.overview("7d", trendDays));
}
