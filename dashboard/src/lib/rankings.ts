import type { RankingRow, Targets } from "./types";

/** Which interval to draw depends on targets.objective (BUILD_PLAN §10.5). */
export function intervalFor(r: RankingRow, objective: Targets["objective"]) {
  if (objective === "value") return { low: r.roas_p10, mid: r.roas_shrunk, high: r.roas_p90, lowerIsBetter: false };
  if (objective === "traffic") return { low: r.ctr_p10, mid: r.ctr_post_mean, high: r.ctr_p90, lowerIsBetter: false };
  return { low: r.cpa_p10, mid: r.cpa_shrunk, high: r.cpa_p90, lowerIsBetter: true };
}

/** Campaign › parent label for a ranking row.

Ids on `campaigns` and `ad_groups` are separate sequences, so `parent_id === campaign_id`
does **not** mean the parent is the campaign (QA: first ad group in a fresh DB is often id 1).
Show `parent_name` when it is present and different from the campaign name.
*/
export function rankingParentLine(r: {
  campaign_id?: number | null;
  campaign_name?: string | null;
  parent_id?: number | null;
  parent_name?: string | null;
}): string {
  const campaign =
    r.campaign_name?.trim() || (r.campaign_id != null ? `Campaign #${r.campaign_id}` : "");
  const parent = r.parent_name?.trim() ?? "";
  if (parent && parent !== (r.campaign_name?.trim() ?? "")) {
    return campaign ? `${campaign} › ${parent}` : parent;
  }
  return campaign;
}

export type PeerGroupContext = {
  parent_name?: string | null;
  campaign_name?: string | null;
  platform?: string | null;
};

const OBJECTIVE_LABELS: Record<string, string> = {
  OUTCOME_SALES: "Sales",
  OUTCOME_TRAFFIC: "Traffic",
  OUTCOME_AWARENESS: "Awareness",
  OUTCOME_ENGAGEMENT: "Engagement",
  OUTCOME_LEADS: "Leads",
  OUTCOME_APP_PROMOTION: "App promotion",
  OUTCOME_CONVERSIONS: "Conversions",
  SEARCH: "Search",
  PERFORMANCE_MAX: "Performance Max",
  DISPLAY: "Display",
  VIDEO: "Video",
  SHOPPING: "Shopping",
  DEMAND_GEN: "Demand Gen",
  SMART: "Smart",
  DISCOVERY: "Discovery",
};

function titleCaseToken(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function platformWord(platform: string | null | undefined): string {
  const p = (platform ?? "").toLowerCase();
  if (p === "meta") return "Meta";
  if (p === "google") return "Google";
  return platform?.trim() || "";
}

/** Container noun for Meta ad sets vs Google ad groups. */
function containerNoun(platform: string | null | undefined, plural: boolean): string {
  const p = (platform ?? "").toLowerCase();
  if (p === "google") return plural ? "Ad groups" : "ad group";
  if (p === "meta") return plural ? "Ad sets" : "ad set";
  return plural ? "Ad groups" : "ad group";
}

/** Human-readable peer-group description. Raw key stays in a tooltip at the call site. */
export function peerGroupDescription(
  peerGroup: string | null | undefined,
  ctx: PeerGroupContext = {},
): string {
  const raw = peerGroup?.trim();
  if (!raw) return "—";

  let m = raw.match(/^ad\|ad_group:(\d+)$/);
  if (m) {
    const name = ctx.parent_name?.trim() || `${containerNoun(ctx.platform, false)} ${m[1]}`;
    return `Ads in ${name}`;
  }

  m = raw.match(/^ad_group\|campaign:(\d+)$/);
  if (m) {
    const name = ctx.campaign_name?.trim() || `campaign ${m[1]}`;
    return `${containerNoun(ctx.platform, true)} in ${name}`;
  }

  m = raw.match(/^keyword\|ad_group:(\d+)\|match:([^|]+)$/);
  if (m) {
    const name = ctx.parent_name?.trim() || `ad group ${m[1]}`;
    const match = m[2].replace(/_/g, " ").toLowerCase();
    return `Keywords in ${name} · ${match} match`;
  }

  m = raw.match(/^campaign\|([^|]+)\|objective:(.*)$/);
  if (m) {
    const plat = platformWord(m[1]);
    const objRaw = (m[2] ?? "").trim();
    const obj = objRaw ? (OBJECTIVE_LABELS[objRaw.toUpperCase()] ?? titleCaseToken(objRaw)) : "";
    if (plat && obj) return `${plat} campaigns · ${obj}`;
    if (plat) return `${plat} campaigns`;
    if (obj) return `Campaigns · ${obj}`;
    return "Campaigns";
  }

  return raw;
}

export function peerGroupLabel(
  peerGroup: string | null | undefined,
  peerSize: number | null | undefined,
  ctx: PeerGroupContext = {},
): string {
  const desc = peerGroupDescription(peerGroup, ctx);
  const n = peerSize;
  const count = n == null ? "?" : String(n);
  const noun = n === 1 ? "entity" : "entities";
  return `${desc} · ${count} ${noun}`;
}
