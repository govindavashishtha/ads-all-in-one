import clsx from "clsx";
import { formatMoney, formatPct, formatRoas, toNum } from "@/lib/format";
import type { Money, Score } from "@/lib/types";
import { Badge } from "./ui";

export function ScoreCell({ score }: { score: Score | null | undefined }) {
  if (!score || score.composite == null) return <span className="text-xs text-slate-400">not scored</span>;
  const c = score.composite;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={clsx(
          "inline-flex h-6 min-w-9 items-center justify-center rounded-md px-1.5 text-xs font-semibold tabular-nums",
          c >= 70 ? "bg-emerald-100 text-emerald-800" : c >= 40 ? "bg-amber-100 text-amber-800" : "bg-red-100 text-red-700",
        )}
        title={`P(beats target) ${formatPct(score.p_beats_target, 0)}; rank ${score.rank_in_peer ?? "—"} of ${score.peer_size ?? "—"} (as of ${score.as_of})`}
      >
        {c}
      </span>
      {score.data_sufficient === false && <Badge tone="slate">low data</Badge>}
    </span>
  );
}

export function LearningCell({ learning }: { learning: boolean | null | undefined }) {
  if (learning === null || learning === undefined) return <Badge tone="slate" title="Unknown is treated as learning (protected)">unknown</Badge>;
  return learning ? <Badge tone="violet">learning</Badge> : <span className="text-xs text-slate-400">—</span>;
}

export function CpaCell({ cpa, target, currency }: { cpa: Money | null | undefined; target?: Money | null; currency?: string }) {
  const v = toNum(cpa);
  const t = toNum(target ?? null);
  return (
    <span className={clsx("tabular-nums", v !== null && t !== null && (v > t * 1.2 ? "text-red-600" : v <= t ? "text-emerald-700" : ""))}>
      {formatMoney(cpa, currency)}
    </span>
  );
}

export function RoasCell({ roas }: { roas: number | string | null | undefined }) {
  return <span className="tabular-nums">{formatRoas(roas)}</span>;
}
