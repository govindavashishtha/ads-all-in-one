/**
 * EXAMPLE DATA for mock mode only (NEXT_PUBLIC_MOCK_API=true), shaped exactly like the
 * OpenAPI schemas (docs/openapi.json). Every number is invented for demos. Caps and targets
 * are the plan's own example values (§11.2 / §12.2 seeds), NOT real business inputs (§25).
 * The UI shows a permanent "Example data" banner whenever this module is used.
 */
import type {
  Ad,
  AdAccount,
  AdGroup,
  Alert,
  Campaign,
  Change,
  ConfigResponse,
  EntityType,
  JobRun,
  LlmUsage,
  LlmUsageDay,
  MetricPoint,
  MetricsSeries,
  Overview,
  Platform,
  GuardrailResult,
  IntradayPoint,
  JobRequest,
  Proposal,
  RangeMetrics,
  RankLevel,
  RankingRow,
  Rankings,
  Score,
  SpendBlock,
  SystemStatus,
  TrendPoint,
  ValueChange,
} from "../types";

export const MOCK_TZ = "Asia/Kolkata";
export const MOCK_CURRENCY = "INR";
const TARGET_CPA = 500;
const CAPS = {
  total: { daily: 20000, monthly: 550000 },
  meta: { daily: 12000, monthly: 330000 },
  google: { daily: 8000, monthly: 220000 },
};
const ACCOUNTS: AdAccount[] = [
  { id: 1, platform: "meta", external_id: "act_0000000001", name: "Example Meta account", status: "ACTIVE", currency: MOCK_CURRENCY, timezone: MOCK_TZ, last_synced_at: null, stale: false },
  { id: 2, platform: "google", external_id: "0000000002", name: "Example Google Ads account", status: "ENABLED", currency: MOCK_CURRENCY, timezone: MOCK_TZ, last_synced_at: null, stale: false },
];

/** Deterministic PRNG so screenshots and tests are stable. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const money = (n: number) => (Math.round(n * 100) / 100).toFixed(2);
const H = 3600_000;
const D = 24 * H;
const iso = (msAgo: number, now: Date) => new Date(now.getTime() - msAgo).toISOString();

function localParts(d: Date): { date: string; hour: number; minute: number } {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: MOCK_TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(d);
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? "00";
  return { date: `${g("year")}-${g("month")}-${g("day")}`, hour: Number(g("hour")) % 24, minute: Number(g("minute")) };
}
function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

interface CampSpec {
  id: number;
  platform: Platform;
  name: string;
  kind: string;
  budget_level: string;
  budget: number;
  cvr: number;
  aov: number;
  ctr: number;
  managed: boolean;
  learning: boolean | null;
  status?: string;
}
const CAMPS: CampSpec[] = [
  { id: 101, platform: "meta", name: "Prospecting – Broad (example)", kind: "standard", budget_level: "ad_set", budget: 4200, cvr: 2.3, aov: 1650, ctr: 0.012, managed: true, learning: false },
  { id: 102, platform: "meta", name: "Retargeting 30d (example)", kind: "standard", budget_level: "campaign", budget: 2500, cvr: 3.4, aov: 1900, ctr: 0.019, managed: true, learning: false },
  { id: 103, platform: "meta", name: "Advantage+ Shopping (example)", kind: "advantage_plus", budget_level: "campaign", budget: 3000, cvr: 2.0, aov: 1550, ctr: 0.011, managed: false, learning: true },
  { id: 104, platform: "meta", name: "Lookalike 1% – Video (example)", kind: "standard", budget_level: "ad_set", budget: 1500, cvr: 1.2, aov: 1400, ctr: 0.009, managed: true, learning: false },
  { id: 201, platform: "google", name: "Search – Brand (example)", kind: "standard", budget_level: "campaign", budget: 1500, cvr: 4.6, aov: 1800, ctr: 0.081, managed: true, learning: false },
  { id: 202, platform: "google", name: "Search – Generic (example)", kind: "standard", budget_level: "campaign", budget: 3000, cvr: 1.7, aov: 1600, ctr: 0.041, managed: true, learning: false },
  { id: 203, platform: "google", name: "Performance Max – All products (example)", kind: "pmax", budget_level: "campaign", budget: 2500, cvr: 2.1, aov: 1500, ctr: 0.014, managed: false, learning: null },
  { id: 204, platform: "google", name: "Search – Competitors (example)", kind: "standard", budget_level: "shared", budget: 800, cvr: 0.9, aov: 1450, ctr: 0.029, managed: false, learning: false, status: "PAUSED" },
];

export interface MockDb {
  now: Date;
  today: string;
  campaigns: Campaign[];
  adGroups: AdGroup[];
  ads: Ad[];
  daily: Map<string, MetricPoint[]>; // `${entity_type}:${id}`
  proposals: Proposal[];
  changes: Change[];
  alerts: Alert[];
  /** spend so far today per platform */
  todaySpend: Record<Platform, number>;
}

function dailySeries(seed: number, days: number, today: string, spec: { budget: number; cvr: number; aov: number; ctr: number }, scale = 1): MetricPoint[] {
  const rnd = mulberry32(seed);
  const out: MetricPoint[] = [];
  for (let i = days; i >= 1; i--) {
    const date = addDays(today, -i);
    const wd = new Date(`${date}T00:00:00Z`).getUTCDay();
    const wk = wd === 0 || wd === 6 ? 0.88 : 1.04;
    const drift = 1 + 0.12 * Math.sin((days - i) / 6);
    const spend = spec.budget * scale * wk * (0.82 + 0.3 * rnd());
    const conv = Math.max(0, ((spend * spec.cvr) / 1000) * drift * (0.7 + 0.6 * rnd()));
    const impressions = Math.round((spend / (120 + 80 * rnd())) * 1000);
    out.push({ date, ts_hour: null, impressions, clicks: Math.round(impressions * spec.ctr * (0.85 + 0.3 * rnd())), spend: money(spend), conversions: conv.toFixed(4), conv_value: money(conv * spec.aov * (0.9 + 0.2 * rnd())), video_views: null });
  }
  return out;
}

export function rangeMetrics(points: MetricPoint[], days: number): RangeMetrics {
  const last = points.slice(-days);
  const spend = last.reduce((a, p) => a + Number(p.spend), 0);
  const conv = last.reduce((a, p) => a + Number(p.conversions), 0);
  const value = last.reduce((a, p) => a + Number(p.conv_value), 0);
  const imp = last.reduce((a, p) => a + p.impressions, 0);
  const clicks = last.reduce((a, p) => a + p.clicks, 0);
  return {
    from_date: last[0]?.date ?? null, to_date: last[last.length - 1]?.date ?? null,
    spend: money(spend), impressions: imp, clicks, conversions: conv.toFixed(4), conv_value: money(value),
    cpa: conv > 0 ? money(spend / conv) : null, roas: spend > 0 ? Math.round((value / spend) * 100) / 100 : null, ctr: imp > 0 ? clicks / imp : null,
  };
}

function scoreFor(m: RangeMetrics, seed: number, asOf: string, peer: string, peerSize: number, rank: number, minSpendX: number): Score {
  const rnd = mulberry32(seed);
  const spend = Number(m.spend ?? 0);
  const conv = Number(m.conversions ?? 0);
  const a = 1 + conv, b = TARGET_CPA + spend; // gamma-poisson flavoured shrinkage (example only)
  const cpa = b / a;
  const width = cpa * (1.6 / Math.sqrt(a));
  const p = Math.max(0.02, Math.min(0.98, 1 / (1 + Math.exp((cpa - TARGET_CPA) / (60 + width / 3))) + (rnd() - 0.5) * 0.04));
  return {
    as_of: asOf, peer_group: peer, peer_size: peerSize, rank_in_peer: rank,
    p_beats_target: Math.round(p * 1000) / 1000, composite: Math.round(100 * p),
    cpa_shrunk: money(cpa), cpa_p10: money(Math.max(1, cpa - width * 0.8)), cpa_p90: money(cpa + width), rank_metric: Math.round((cpa + width) * 100) / 100,
    ctr_post_mean: m.ctr ?? null, data_sufficient: spend >= TARGET_CPA * minSpendX,
  };
}

function rankWithin<T extends { score?: Score | null }>(items: T[], peer: (t: T) => string) {
  const groups = new Map<string, T[]>();
  for (const it of items) groups.set(peer(it), [...(groups.get(peer(it)) ?? []), it]);
  for (const [name, list] of groups) {
    list.sort((x, y) => Number(x.score?.cpa_p90 ?? 1e9) - Number(y.score?.cpa_p90 ?? 1e9));
    list.forEach((it, i) => { if (it.score) { it.score.rank_in_peer = i + 1; it.score.peer_size = list.length; it.score.peer_group = name; } });
  }
}

export function buildMockDb(now: Date = new Date()): MockDb {
  const { date: today, hour, minute } = localParts(now);
  const asOf = addDays(today, -3);
  const updated = iso(55 * 60_000, now);
  const daily = new Map<string, MetricPoint[]>();
  const campaigns: Campaign[] = [];
  const adGroups: AdGroup[] = [];
  const ads: Ad[] = [];
  const kw: { id: number; ad_group_id: number; text: string; match: string; m: RangeMetrics }[] = [];

  for (const c of CAMPS) {
    const series = dailySeries(c.id * 7919, 90, today, c);
    daily.set(`campaign:${c.id}`, series);
    const m = rangeMetrics(series, 7);
    campaigns.push({
      id: c.id, ad_account_id: c.platform === "meta" ? 1 : 2, platform: c.platform,
      external_id: c.platform === "meta" ? `2385${c.id}0000${c.id}` : `98${c.id}551${c.id}`, name: c.name, status: c.status ?? (c.platform === "meta" ? "ACTIVE" : "ENABLED"),
      managed: c.managed, kind: c.kind, automation_scope: c.kind === "standard" ? "full" : "campaign_only", budget_level: c.budget_level,
      daily_budget: c.budget_level === "ad_set" ? null : money(c.budget), lifetime_budget: null, shared_budget_external_id: c.budget_level === "shared" ? "shared_77" : null,
      objective: c.platform === "meta" ? "OUTCOME_SALES" : "SALES", bid_strategy: c.platform === "meta" ? "LOWEST_COST_WITHOUT_CAP" : "MAXIMIZE_CONVERSIONS",
      learning: c.learning, pool: null, updated_at: updated, metrics: m, score: scoreFor(rangeMetrics(series, 14), c.id * 31, asOf, `${c.platform} · sales`, 0, 0, 5),
    });
    if (c.kind !== "standard") continue; // PMax / Advantage+: campaign level only (§8.2)
    const nGroups = 2 + (c.id % 3);
    for (let g = 0; g < nGroups; g++) {
      const gid = c.id * 10 + g;
      const share = [0.45, 0.33, 0.22, 0.15][g] ?? 0.1;
      const gSpec = { budget: c.budget, cvr: c.cvr * (0.7 + 0.25 * g), aov: c.aov, ctr: c.ctr * (1.1 - 0.12 * g) };
      const gSeries = dailySeries(gid * 104729, 28, today, gSpec, share);
      daily.set(`ad_group:${gid}`, gSeries);
      const names = c.platform === "meta" ? ["Broad 25–44", "Interest: Fitness", "Advantage audience", "LAL 1% purchasers"] : ["Exact – core terms", "Phrase – category", "Broad – discovery", "Exact – long tail"];
      const learning = c.platform === "meta" && g === 2;
      adGroups.push({
        id: gid, campaign_id: c.id, platform: c.platform, external_id: `${gid}00${gid}`, kind: c.platform === "meta" ? "ad_set" : "ad_group",
        name: names[g] ?? `Group ${g + 1}`, status: c.platform === "meta" ? "ACTIVE" : "ENABLED", daily_budget: c.budget_level === "ad_set" ? money(c.budget * share) : null,
        optimization_goal: c.platform === "meta" ? "OFFSITE_CONVERSIONS" : null, learning, learning_status_at: learning ? iso(2 * D, now) : null, updated_at: updated,
        metrics: rangeMetrics(gSeries, 7), score: scoreFor(rangeMetrics(gSeries, 14), gid, asOf, c.name, 0, 0, learning ? 1e9 : 3),
      });
      const nAds = 2 + ((gid + 1) % 3);
      for (let a = 0; a < nAds; a++) {
        const aid = gid * 10 + a;
        const aSeries = dailySeries(aid * 15485863, 28, today, { ...gSpec, cvr: gSpec.cvr * (1.2 - 0.2 * a) }, share / nAds);
        daily.set(`ad:${aid}`, aSeries);
        const heads = ["Free shipping this week", "New arrivals are here", "Rated 4.8 by 2,000 buyers", "Bundle & save 20%"];
        ads.push({
          id: aid, ad_group_id: gid, platform: c.platform, external_id: `${aid}${aid}`, name: `${c.platform === "meta" ? "Creative" : "RSA"} ${String.fromCharCode(65 + a)}`,
          status: a === nAds - 1 && gid % 2 === 0 ? "PAUSED" : c.platform === "meta" ? "ACTIVE" : "ENABLED",
          format: c.platform === "meta" ? (a % 2 ? "video" : "image") : "responsive_search_ad", headline: heads[(aid + a) % heads.length], body: null, cta: null,
          creative_id: null, first_live_at: iso((20 + a) * D, now), updated_at: updated,
          metrics: rangeMetrics(aSeries, 7), score: scoreFor(rangeMetrics(aSeries, 14), aid, asOf, `${c.name} › ${names[g]}`, 0, 0, 1),
        });
      }
      if (c.platform === "google") {
        ["running shoes", "buy running shoes online", "trail shoes", "shoes sale", "lightweight trainers"].slice(0, 3 + (g % 3)).forEach((t, i) => {
          const id = gid * 100 + i;
          kw.push({ id, ad_group_id: gid, text: t, match: i % 2 ? "PHRASE" : "EXACT", m: rangeMetrics(dailySeries(id * 7, 14, today, { ...gSpec, cvr: gSpec.cvr * (0.6 + 0.3 * i) }, share / 4), 14) });
        });
      }
    }
  }
  rankWithin(campaigns, (c) => `${c.platform} · sales`);
  rankWithin(adGroups, (g) => campaigns.find((c) => c.id === g.campaign_id)!.name ?? "");
  rankWithin(ads, (a) => a.score?.peer_group ?? "");
  kwStore = kw;

  const curve = (h: number) => Math.pow(h / 24, 1.15);
  const nowH = Math.max(hour + minute / 60, 0.25);
  const todaySpend = { meta: CAPS.meta.daily * 0.97 * curve(nowH), google: CAPS.google.daily * 0.84 * curve(nowH) };

  const gr = (result: "allow" | "needs_approval" | "block", extra: Partial<GuardrailResult> = {}): GuardrailResult => ({
    result, approved: false, mode: "suggest", checked_at: iso(6 * H, now), reasons: result === "needs_approval" ? ["mode_suggest"] : [], reason: null, new_daily_budget: null, detail: {}, ...extra,
  });
  const proposal = (p: Partial<Proposal> & Pick<Proposal, "id" | "action" | "entity_type" | "entity_id" | "reason" | "status">): Proposal => ({
    as_of: addDays(today, -1), source: "rule", rule_key: null, params: {}, expected_impact: null, risk: "low", spend_direction: "neutral",
    guardrail_result: null, expires_at: null, created_at: iso(6 * H, now), dedupe_key: `example-${p.id}`, approval: null, ...p,
  });
  const awaiting = (id: number, h: number) => ({
    expires_at: iso(-h * H, now),
    approval: { id: 500 + id, proposal_id: id, channel: "email", requested_at: iso((24 - h) * H, now), reminded_at: null, decided_at: null, decision: null, decided_by: null, comment: null },
    guardrail_result: gr("needs_approval"),
  });
  const proposals: Proposal[] = [
    proposal({ id: 41, rule_key: "scale_winner", entity_type: "campaign", entity_id: 102, action: "budget_change", params: { change_pct: 15, current_budget: "2500.00" }, expected_impact: { extra_conversions_per_day: "1.2" }, reason: "p_beats_target 0.91 over 14 settled days; spend pace 0.97; frequency 2.1 (< 3.5).", spend_direction: "increasing", status: "awaiting_approval", ...awaiting(41, 18), guardrail_result: gr("needs_approval", { new_daily_budget: "2875.00", detail: { current: "2500.00" } }) }),
    proposal({ id: 42, rule_key: "kill_loser", entity_type: "ad", entity_id: 104010, action: "pause", expected_impact: { spend_saved_per_day: "310.00" }, reason: "CPA p10 ₹1,640 > 3× target on settled data; 4,800 impressions; live 9 days.", spend_direction: "reducing", status: "awaiting_approval", ...awaiting(42, 20) }),
    proposal({ id: 43, rule_key: "trim_underperformer", entity_type: "campaign", entity_id: 202, action: "budget_change", params: { change_pct: -15, current_budget: "3000.00" }, expected_impact: { spend_saved_per_day: "450.00" }, reason: "CPA ₹712 vs target ₹500 for 14 days; p_beats_target 0.12.", spend_direction: "reducing", status: "awaiting_approval", ...awaiting(43, 21), guardrail_result: gr("needs_approval", { new_daily_budget: "2550.00", detail: { current: "3000.00" } }) }),
    proposal({ id: 45, rule_key: "negative_keyword_candidate", entity_type: "search_term", entity_id: 9001, action: "add_negative", params: { term: "free download template", match_type: "PHRASE", ad_group_id: 2020 }, reason: "₹1,920 spent over 30 days, 0 conversions, 64 clicks.", spend_direction: "reducing", status: "awaiting_approval", ...awaiting(45, 22) }),
    proposal({ id: 44, source: "llm_triage", entity_type: "ad_group", entity_id: 1012, action: "budget_change", params: { change_pct: 10, current_budget: "924.00" }, reason: "Anomaly triage: CPA spike recovered; small increase suggested.", risk: "medium", spend_direction: "increasing", status: "blocked", guardrail_result: gr("block", { reason: "learning_phase", detail: { why: "ad set is in the learning phase" } }) }),
    proposal({ id: 38, as_of: addDays(today, -3), rule_key: "scale_winner", entity_type: "campaign", entity_id: 201, action: "budget_change", params: { change_pct: 12, current_budget: "1340.00" }, reason: "p_beats_target 0.95; impression share lost to budget 18%.", spend_direction: "increasing", status: "executed", guardrail_result: gr("allow", { new_daily_budget: "1500.00", approved: true }), created_at: iso(3 * D, now) }),
    proposal({ id: 36, as_of: addDays(today, -4), rule_key: "kill_loser", entity_type: "ad", entity_id: 101021, action: "pause", reason: "CPA p10 above 3× target.", spend_direction: "reducing", status: "rejected", created_at: iso(4 * D, now) }),
  ];

  const ev = (id: number, change_id: number, h: number, due: number, done: number | null, verdict: string | null, extra: Record<string, unknown> = {}) => ({
    id, change_id, horizon_days: h, due_at: iso(due, now), done_at: done == null ? null : iso(done, now), verdict, unit_entity_type: "campaign" as EntityType, unit_entity_id: 0,
    baseline: null, observed: null, p_worse: null, p_better: null, action_taken: done == null ? null : verdict === "worse" ? "rollback" : "none", ...extra,
  });
  const change = (c: Partial<Change> & Pick<Change, "id" | "platform" | "entity_type" | "entity_id" | "action" | "status" | "started_at">): Change => ({
    proposal_id: null, before: null, intended: null, after: null, applied_at: null, applied_by: null, rollback_of: null, rollback_payload: null, validate_result: { ok: true }, evaluations: [], verdict: null, ...c,
  });
  const changes: Change[] = [
    change({ id: 17, proposal_id: 38, platform: "google", entity_type: "campaign", entity_id: 201, action: "budget_change", status: "applied", before: { daily_budget: "1340.00" }, intended: { daily_budget: "1500.00" }, after: { daily_budget: "1500.00" }, started_at: iso(3 * D + 2 * H, now), applied_at: iso(3 * D + 2 * H - 40_000, now), applied_by: "approval:admin", evaluations: [ev(71, 17, 3, 2 * H, H, null), ev(72, 17, 7, -6 * D, null, null)] }),
    change({ id: 16, platform: "meta", entity_type: "ad_group", entity_id: 1040, action: "budget_change", status: "applied", before: { daily_budget: "776.25" }, intended: { daily_budget: "675.00" }, after: { daily_budget: "675.00" }, started_at: iso(9 * D, now), applied_at: iso(9 * D - 20_000, now), applied_by: "evaluator", rollback_of: 14 }),
    change({ id: 15, proposal_id: 33, platform: "meta", entity_type: "ad", entity_id: 101031, action: "pause", status: "applied", before: { status: "ACTIVE" }, intended: { status: "PAUSED" }, after: { status: "PAUSED" }, started_at: iso(10 * D, now), applied_at: iso(10 * D - 30_000, now), applied_by: "approval:admin", verdict: "better", evaluations: [ev(61, 15, 3, 7 * D, 7 * D, null), ev(62, 15, 7, D, D, "better", { p_worse: 0.04, p_better: 0.82 })] }),
    change({ id: 14, proposal_id: 31, platform: "meta", entity_type: "ad_group", entity_id: 1040, action: "budget_change", status: "rolled_back", before: { daily_budget: "675.00" }, intended: { daily_budget: "776.25" }, after: { daily_budget: "776.25" }, started_at: iso(12 * D, now), applied_at: iso(12 * D - 25_000, now), applied_by: "approval:admin", verdict: "worse", evaluations: [ev(51, 14, 3, 9 * D, 9 * D, "worse")] }),
    change({ id: 13, proposal_id: 29, platform: "google", entity_type: "campaign", entity_id: 202, action: "budget_change", status: "verify_failed", before: { daily_budget: "3200.00" }, intended: { daily_budget: "3000.00" }, after: { daily_budget: "3200.00" }, started_at: iso(15 * D, now), applied_by: "approval:admin" }),
  ];

  // Display fields the API adds (entity_name, platform, campaign, before → after).
  const ref = (type: EntityType, id: number): { entity_name: string | null; platform: Platform | null; campaign_id: number | null; campaign_name: string | null; status?: string | null; budget?: string | null } => {
    const campOf = (cid: number) => campaigns.find((c) => c.id === cid);
    if (type === "campaign") {
      const c = campOf(id);
      return { entity_name: c?.name ?? null, platform: c?.platform ?? null, campaign_id: c?.id ?? null, campaign_name: c?.name ?? null, status: c?.status, budget: c?.daily_budget ?? null };
    }
    if (type === "ad_group") {
      const g = adGroups.find((x) => x.id === id);
      const c = g ? campOf(g.campaign_id) : undefined;
      return { entity_name: g?.name ?? null, platform: g?.platform ?? null, campaign_id: c?.id ?? null, campaign_name: c?.name ?? null, status: g?.status, budget: g?.daily_budget ?? null };
    }
    if (type === "ad") {
      const a = ads.find((x) => x.id === id);
      const g = a ? adGroups.find((x) => x.id === a.ad_group_id) : undefined;
      const c = g ? campOf(g.campaign_id) : undefined;
      return { entity_name: a ? `${a.name} — ${a.headline}` : null, platform: a?.platform ?? null, campaign_id: c?.id ?? null, campaign_name: c?.name ?? null, status: a?.status };
    }
    if (type === "search_term") {
      const g = adGroups.find((x) => x.platform === "google");
      const c = g ? campOf(g.campaign_id) : undefined;
      return { entity_name: "free download template", platform: "google", campaign_id: c?.id ?? null, campaign_name: c?.name ?? null };
    }
    return { entity_name: null, platform: null, campaign_id: null, campaign_name: null };
  };
  for (const p of proposals) {
    const r = ref(p.entity_type, p.entity_id);
    const params = (p.params ?? {}) as Record<string, unknown>;
    let change: ValueChange | null = null;
    if (p.action === "budget_change") {
      const before = (p.guardrail_result?.detail?.current as string | undefined) ?? (params.current_budget as string | undefined) ?? r.budget ?? null;
      const after = p.guardrail_result?.new_daily_budget ?? (before != null ? money(Number(before) * (1 + Number(params.change_pct ?? 0) / 100)) : null);
      change = { field: "daily_budget", before, after, after_is_estimate: true };
    } else if (p.action === "pause") change = { field: "status", before: r.status ?? "ACTIVE", after: "paused", after_is_estimate: false };
    else if (p.action === "add_negative") change = { field: "negative_keyword", before: null, after: `${params.term} [${String(params.match_type ?? "exact").toLowerCase()}]`, after_is_estimate: false };
    Object.assign(p, { entity_name: r.entity_name, platform: r.platform, campaign_id: r.campaign_id, campaign_name: r.campaign_name, change });
  }
  for (const c of changes) {
    const r = ref(c.entity_type, c.entity_id);
    const key = c.before && "daily_budget" in c.before ? "daily_budget" : "status";
    const change: ValueChange = { field: key, before: String(c.before?.[key] ?? ""), after: String((c.after ?? c.intended)?.[key] ?? ""), after_is_estimate: !c.after };
    Object.assign(c, { entity_name: r.entity_name, campaign_id: r.campaign_id, campaign_name: r.campaign_name, change });
  }

  const alert = (a: Partial<Alert> & Pick<Alert, "id" | "severity" | "kind" | "message" | "created_at">): Alert => ({ acked_at: null, acked_by: null, dedupe_key: `${a.kind}:${a.id}`, entity_id: null, entity_type: null, last_sent_at: a.created_at, sent_channels: ["email"], ...a });
  const alerts: Alert[] = [
    alert({ id: 301, severity: "critical", kind: "credential_expiring", message: "Example: Google read credential expires in 5 days — re-authorise it in Google Cloud.", created_at: iso(3 * H, now) }),
    alert({ id: 302, severity: "warn", kind: "pacing_warn", message: "Example: Meta spend today is 88% of the ₹12,000 daily cap.", created_at: iso(40 * 60_000, now) }),
    alert({ id: 303, severity: "info", kind: "manual_change", message: "Example: manual budget change detected on “Search – Brand” (48 h cooldown).", entity_type: "campaign", entity_id: 201, created_at: iso(26 * H, now) }),
  ];

  return { now, today, campaigns, adGroups, ads, daily, proposals, changes, alerts, todaySpend };
}

let kwStore: { id: number; ad_group_id: number; text: string; match: string; m: RangeMetrics }[] = [];

/* ---------------- response builders ---------------- */

const KILL = { db: false, effective: false, env_override: false };

export function mockOverview(db: MockDb, range: string, trendDays = 28): Overview {
  const { hour, minute } = localParts(db.now);
  const days = Number.parseInt(range, 10) || 7;
  const elapsed = (hour + minute / 60) / 24;
  const mtdDays = Number(db.today.slice(8, 10)) - 1;
  const block = (platform: "meta" | "google" | "total"): SpendBlock => {
    const plats: Platform[] = platform === "total" ? ["meta", "google"] : [platform];
    const camps = db.campaigns.filter((c) => plats.includes(c.platform));
    const merged = new Map<string, MetricPoint>();
    for (const c of camps) for (const p of db.daily.get(`campaign:${c.id}`)!) {
      const m = merged.get(p.date!) ?? { date: p.date, impressions: 0, clicks: 0, spend: "0", conversions: "0", conv_value: "0" };
      merged.set(p.date!, { ...m, impressions: m.impressions + p.impressions, clicks: m.clicks + p.clicks, spend: money(Number(m.spend) + Number(p.spend)), conversions: (Number(m.conversions) + Number(p.conversions)).toFixed(4), conv_value: money(Number(m.conv_value) + Number(p.conv_value)) });
    }
    const series = [...merged.values()].sort((a, b) => a.date!.localeCompare(b.date!));
    const today = plats.reduce((a, p) => a + db.todaySpend[p], 0);
    const mtd = (mtdDays > 0 ? series.slice(-mtdDays) : []).reduce((a, p) => a + Number(p.spend), 0) + today;
    const cap = CAPS[platform];
    // Example intraday curve (same shape the mock spend follows) for the expected line.
    const curve = (h: number) => Math.pow(h / 24, 1.15);
    const nowH = Math.max(hour + minute / 60, 0.25);
    let cum = 0;
    const intraday: IntradayPoint[] = Array.from({ length: 24 }, (_, h) => {
      const started = h <= hour;
      const spend = started ? today * ((curve(Math.min(h + 1, nowH)) - curve(h)) / curve(nowH)) : null;
      if (spend !== null) cum += spend;
      return { hour: h, spend: spend === null ? null : money(spend), cumulative_spend: started ? money(cum) : null, expected_cumulative: money(cap.daily * curve(h + 1)) };
    });
    return {
      platform, account_date: platform === "total" ? null : db.today, spend_today: money(today), daily_cap: money(cap.daily), spent_fraction_today: today / cap.daily,
      mtd_spend: money(mtd), monthly_cap: money(cap.monthly), mtd_fraction: mtd / cap.monthly, elapsed_day_fraction: elapsed, range: rangeMetrics(series, days),
      timezone: platform === "total" ? null : MOCK_TZ, curve_source: "learned", intraday,
      projected_eod: hour >= 2 ? money(today / curve(nowH)) : null, projected_eod_at: hour >= 2 ? iso(6 * 60_000, db.now) : null,
    };
  };
  const trend: TrendPoint[] = [];
  for (let i = trendDays; i >= 1; i--) {
    const date = addDays(db.today, -i);
    const by: Record<string, string> = {};
    let imp = 0, clicks = 0, spend = 0, conv = 0, value = 0;
    for (const plat of ["meta", "google"] as Platform[]) {
      let ps = 0;
      for (const c of db.campaigns.filter((x) => x.platform === plat)) {
        const pt = db.daily.get(`campaign:${c.id}`)!.find((x) => x.date === date);
        if (!pt) continue;
        imp += pt.impressions; clicks += pt.clicks; ps += Number(pt.spend); conv += Number(pt.conversions); value += Number(pt.conv_value);
      }
      by[plat] = money(ps);
      spend += ps;
    }
    trend.push({ date, impressions: imp, clicks, spend: money(spend), conversions: conv.toFixed(4), conv_value: money(value), cpa: conv > 0 ? money(spend / conv) : null, roas: spend > 0 ? value / spend : null, spend_by_platform: by });
  }
  const unacked = db.alerts.filter((a) => !a.acked_at);
  const count = (s: string) => unacked.filter((a) => a.severity === s).length;
  return {
    client_id: "example_client", client_name: "Example Client", currency: MOCK_CURRENCY, mode: "suggest", hard_max_mode: "suggest", kill_switch: KILL,
    last_synced_at: { meta: iso(7 * 60_000, db.now), google: iso(7 * 60_000, db.now) }, limits_version: 1, note: "Example data (mock mode)",
    open_alerts: { critical: count("critical"), warn: count("warn"), info: count("info"), total: unacked.length },
    open_approvals: db.proposals.filter((p) => p.status === "awaiting_approval").length, active_emergency_actions: 0,
    platforms: [block("meta"), block("google")], total: block("total"), range_days: days,
    timezone: MOCK_TZ, targets: { currency: MOCK_CURRENCY, objective: "conversions", target_cpa: String(TARGET_CPA), target_roas: "3.0", rules_version: 1 },
    trend_days: trendDays, trend, alerts: unacked.filter((a) => a.severity !== "info"), emergency_actions: [],
  };
}

export function mockMetrics(db: MockDb, q: URLSearchParams): MetricsSeries {
  const type = (q.get("entity_type") ?? "campaign") as EntityType;
  const id = Number(q.get("entity_id"));
  const grain = (q.get("grain") ?? "day") as "day" | "hour";
  const from = q.get("from") ?? addDays(db.today, -28);
  const to = q.get("to") ?? addDays(db.today, -1);
  let points: MetricPoint[] = [];
  if (type === "account") {
    const plat: Platform = id === 1 ? "meta" : "google";
    if (grain === "hour") {
      const { hour, minute } = localParts(db.now);
      const curve = (h: number) => Math.pow(h / 24, 1.15);
      const nowH = Math.max(hour + minute / 60, 0.25);
      for (let h = 0; h <= hour; h++) {
        const s = db.todaySpend[plat] * ((curve(Math.min(h + 1, nowH)) - curve(h)) / curve(nowH));
        points.push({ date: null, ts_hour: `${db.today}T${String(h).padStart(2, "0")}:00:00`, impressions: Math.round(s * 7), clicks: Math.round(s / 20), spend: money(s), conversions: (s / 480).toFixed(4), conv_value: money(s * 3.4) });
      }
    } else {
      const merged = new Map<string, MetricPoint>();
      for (const c of db.campaigns.filter((x) => x.platform === plat)) for (const p of db.daily.get(`campaign:${c.id}`)!) {
        const m = merged.get(p.date!) ?? { date: p.date, impressions: 0, clicks: 0, spend: "0", conversions: "0", conv_value: "0" };
        merged.set(p.date!, { ...m, impressions: m.impressions + p.impressions, clicks: m.clicks + p.clicks, spend: money(Number(m.spend) + Number(p.spend)), conversions: (Number(m.conversions) + Number(p.conversions)).toFixed(4), conv_value: money(Number(m.conv_value) + Number(p.conv_value)) });
      }
      points = [...merged.values()].sort((a, b) => a.date!.localeCompare(b.date!));
    }
  } else {
    points = db.daily.get(`${type}:${id}`) ?? [];
  }
  if (grain === "day") points = points.filter((p) => p.date! >= from && p.date! <= to);
  return { entity_type: type, entity_id: id, grain, from_date: from, to_date: to, platform: null, points };
}

export function mockRules(db: MockDb): ConfigResponse {
  return {
    kind: "rules",
    active: { id: 1, kind: "rules", version: 1, active: true, changed_by: "bootstrap", created_at: iso(30 * D, db.now), reason: "seed (example values)", body: { inputs_confirmed: false, targets: { currency: MOCK_CURRENCY, objective: "conversions", target_cpa: 500, target_roas: 3.0 } } },
    history: [],
  };
}

export function mockRankings(db: MockDb, level: RankLevel, platform?: string): Rankings {
  type Src = { entity_type: EntityType; id: number; platform: Platform; name: string | null; status: string | null; campaign_id: number | null; score?: Score | null; parent_id?: number | null; parent_name?: string | null };
  let src: Src[];
  if (level === "campaign") src = db.campaigns.map((c) => ({ entity_type: "campaign", id: c.id, platform: c.platform, name: c.name, status: c.status, campaign_id: c.id, score: c.score }));
  else if (level === "ad_group") src = db.adGroups.map((g) => ({ entity_type: "ad_group", id: g.id, platform: g.platform, name: g.name, status: g.status, campaign_id: g.campaign_id, score: g.score, parent_id: g.campaign_id, parent_name: db.campaigns.find((c) => c.id === g.campaign_id)?.name ?? null }));
  else if (level === "ad") src = db.ads.map((a) => {
    const g = db.adGroups.find((x) => x.id === a.ad_group_id)!;
    return { entity_type: "ad", id: a.id, platform: a.platform, name: `${a.name} — ${a.headline}`, status: a.status, campaign_id: g.campaign_id, score: a.score, parent_id: g.id, parent_name: g.name };
  });
  else {
    const asOf = addDays(db.today, -3);
    const items = kwStore.map((k) => {
      const g = db.adGroups.find((x) => x.id === k.ad_group_id)!;
      const c = db.campaigns.find((x) => x.id === g.campaign_id)!;
      return { k, c, g, score: scoreFor(k.m, k.id, asOf, `${c.name} › ${g.name} · ${k.match.toLowerCase()}`, 0, 0, 2) };
    });
    rankWithin(items, (x) => x.score.peer_group ?? "");
    src = items.map(({ k, c, g, score }) => ({ entity_type: "keyword", id: k.id, platform: "google", name: `[${k.match.toLowerCase()}] ${k.text}`, status: "ENABLED", campaign_id: c.id, score, parent_id: g.id, parent_name: g.name }));
  }
  if (platform) src = src.filter((s) => s.platform === platform);
  const campName = (id: number | null) => db.campaigns.find((c) => c.id === id)?.name ?? null;
  const rows: RankingRow[] = src.filter((s) => s.score).map((s, i) => {
    const sc = s.score!;
    const cpa = Number(sc.cpa_shrunk ?? 0);
    const spend = cpa > 0 ? cpa * (3 + (i % 7)) : 800 + i * 37;
    return {
      ...sc, entity_type: s.entity_type, entity_id: s.id, platform: s.platform, name: s.name, status: s.status, campaign_id: s.campaign_id, as_of: sc.as_of,
      campaign_name: campName(s.campaign_id), parent_id: s.parent_id ?? null, parent_name: s.parent_name ?? null,
      spend: money(spend), conversions: cpa > 0 ? (spend / cpa).toFixed(4) : "0", spend_7d: money(spend / 2), conversions_7d: cpa > 0 ? (spend / cpa / 2).toFixed(4) : "0",
      fatigue_flag: s.entity_type === "ad" && i % 9 === 4, learning: false,
    };
  });
  rows.sort((a, b) => (a.peer_group ?? "").localeCompare(b.peer_group ?? "") || (a.rank_in_peer ?? 0) - (b.rank_in_peer ?? 0));
  return { as_of: addDays(db.today, -3), level, rows, targets: { currency: MOCK_CURRENCY, objective: "conversions", target_cpa: String(TARGET_CPA), target_roas: "3.0", rules_version: 1 } };
}

export function mockLlmUsage(db: MockDb, range: string): LlmUsage {
  const n = Number.parseInt(range, 10) || 30;
  const rnd = mulberry32(4242);
  const days: LlmUsageDay[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const date = addDays(db.today, -i);
    const monday = new Date(`${date}T00:00:00Z`).getUTCDay() === 1;
    const sIn = Math.round(8000 + rnd() * 7000), sOut = Math.round(sIn * 0.2);
    days.push({ date, tier: "small", calls: 4 + Math.round(rnd() * 4), input_tokens: sIn, output_tokens: sOut, cached_input_tokens: Math.round(sIn * 0.3), cost_usd: ((sIn * 1 + sOut * 5) / 1e6).toFixed(5) });
    const lIn = Math.round((rnd() > 0.55 ? 10000 : 0) + (monday ? 32000 : 0));
    if (lIn) days.push({ date, tier: "large", calls: monday ? 3 : 1, input_tokens: lIn, output_tokens: Math.round(lIn * 0.18), cached_input_tokens: 0, cost_usd: ((lIn * 3 + lIn * 0.18 * 15) / 1e6).toFixed(5) });
  }
  const tot = (list: LlmUsageDay[]) => ({ calls: list.reduce((a, d) => a + d.calls, 0), input_tokens: list.reduce((a, d) => a + d.input_tokens, 0), output_tokens: list.reduce((a, d) => a + d.output_tokens, 0), cached_input_tokens: list.reduce((a, d) => a + d.cached_input_tokens, 0), cost_usd: list.reduce((a, d) => a + Number(d.cost_usd), 0).toFixed(5) });
  const month = db.today.slice(0, 7);
  return {
    llm_enabled: true, range_days: n, daily_token_cap: 100000, daily_cost_cap_usd: "1.00", monthly_cost_cap_usd: "20.00", days,
    today: tot(days.filter((d) => d.date === db.today)), month_to_date: tot(days.filter((d) => d.date.startsWith(month))),
    by_purpose: {
      triage: { calls: 64, input_tokens: 201000, output_tokens: 40000, cached_input_tokens: 60000, cost_usd: "0.40100" },
      analyst: { calls: 9, input_tokens: 90000, output_tokens: 16000, cached_input_tokens: 0, cost_usd: "0.51000" },
      digest: { calls: 30, input_tokens: 60000, output_tokens: 14000, cached_input_tokens: 20000, cost_usd: "0.13000" },
      search_terms: { calls: 30, input_tokens: 45000, output_tokens: 8000, cached_input_tokens: 0, cost_usd: "0.08500" },
      creative: { calls: 4, input_tokens: 120000, output_tokens: 21000, cached_input_tokens: 0, cost_usd: "0.67500" },
    },
  };
}

export const MANUAL_JOBS = ["pacing", "hourly_pull", "health_checks", "daily_pipeline", "search_terms", "weekly_strategy", "approval_expiry", "notify_dispatch"];
export const jobRequests: JobRequest[] = [];

export function mockSystem(db: MockDb): SystemStatus {
  const now = db.now;
  const runs: JobRun[] = [];
  let id = 900;
  const run = (job: string, step: string | null, ago: number, status: string, dur: number, rows?: number, error?: string) => {
    runs.push({ id: id--, job, step, window_key: step ? addDays(db.today, -1) : new Date(now.getTime() - ago).toISOString().slice(0, 16), status, started_at: iso(ago, now), finished_at: iso(ago - dur, now), rows_ingested: rows ?? null, error: error ?? null });
  };
  const { hour, minute } = localParts(now);
  const since6 = ((hour - 6) * 60 + minute) * 60_000; // daily pipeline fired at 06:00 account time
  run("pacing", null, 6 * 60_000, "success", 4_000, 12);
  run("approval_expiry", null, 8 * 60_000, "success", 800);
  run("hourly_pull", null, 55 * 60_000, "success", 38_000, 214);
  run("emergency_restore", null, 50 * 60_000, "success", 600);
  ["repull", "windows", "features", "evaluate", "score", "rules", "triage", "guardrail", "execute", "approvals", "digest"].forEach((s, i) => run("daily_pipeline", s, since6 - i * 50_000, "success", 45_000, s === "repull" ? 5320 : undefined));
  run("search_terms", null, since6 - H, "success", 51_000, 1880);
  run("retention", null, ((hour - 3) * 60 + minute) * 60_000, "success", 3_000, 0);
  const failures: JobRun[] = [{ id: 700, job: "hourly_pull", step: null, window_key: new Date(now.getTime() - 2 * H).toISOString().slice(0, 16), status: "failed", started_at: iso(2 * H, now), finished_at: iso(2 * H - 60_000, now), rows_ingested: 0, error: "Example: transient 503 from the Google Ads API; the next run succeeded" }];
  return {
    client_id: "example_client", app_env: "development (mock)", auth_mode: "env", mode: "suggest", hard_max_mode: "suggest", kill_switch: KILL, server_time: now.toISOString(),
    ad_accounts: ACCOUNTS.map((a) => ({ ...a, last_synced_at: iso(7 * 60_000, now) })),
    latest_job_runs: runs, recent_failures: failures,
    heartbeat: { configured: true, last_status: "ok", last_ping_at: iso(6 * 60_000, now), last_pacing_success_at: iso(6 * 60_000 - 4_000, now), healthy: true, error: null },
    platform_operations: [
      { platform: "meta", day: db.today, used: 241, limit: null, fraction: null, access_level: "no fixed daily limit (rate-limited per app/account)", updated_at: iso(6 * 60_000, now) },
      { platform: "google", day: db.today, used: 612, limit: 2880, fraction: 612 / 2880, access_level: "explorer (assumed until Basic access is confirmed, VERIFY-P0 V-1)", updated_at: iso(6 * 60_000, now) },
    ],
    manual_jobs: MANUAL_JOBS,
    recent_job_requests: jobRequests,
  };
}
