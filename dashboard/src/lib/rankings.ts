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

export function peerGroupLabel(peerGroup: string | null | undefined, peerSize: number | null | undefined): string {
  const raw = peerGroup?.trim() || "—";
  const n = peerSize;
  const count = n == null ? "?" : String(n);
  const noun = n === 1 ? "entity" : "entities";
  return `Peer group: ${raw} · ${count} ${noun}`;
}
