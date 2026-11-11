import type { RankingRow, Targets } from "./types";

/** Which interval to draw depends on targets.objective (BUILD_PLAN §10.5). */
export function intervalFor(r: RankingRow, objective: Targets["objective"]) {
  if (objective === "value") return { low: r.roas_p10, mid: r.roas_shrunk, high: r.roas_p90, lowerIsBetter: false };
  if (objective === "traffic") return { low: r.ctr_p10, mid: r.ctr_post_mean, high: r.ctr_p90, lowerIsBetter: false };
  return { low: r.cpa_p10, mid: r.cpa_shrunk, high: r.cpa_p90, lowerIsBetter: true };
}
