import { toNum } from "@/lib/format";
import type { Money } from "@/lib/types";

/**
 * p10–p90 interval with the point estimate, on a shared scale, plus the target line.
 * For CPA lower is better, so the bar is green when the pessimistic bound beats target.
 */
export function IntervalBar({
  low,
  mid,
  high,
  target,
  max,
  lowerIsBetter = true,
}: {
  low: Money | number | string | null | undefined;
  mid: Money | number | string | null | undefined;
  high: Money | number | string | null | undefined;
  target?: Money | number | string | null;
  max: number;
  lowerIsBetter?: boolean;
}) {
  const l = toNum(low), m = toNum(mid), h = toNum(high), t = toNum(target ?? null);
  if (l === null || h === null || max <= 0) return <span className="text-xs text-slate-400">—</span>;
  const x = (v: number) => `${Math.min(Math.max(v / max, 0), 1) * 100}%`;
  const good = t === null ? null : lowerIsBetter ? h <= t : l >= t;
  const bad = t === null ? null : lowerIsBetter ? l > t : h < t;
  const color = good ? "bg-emerald-500" : bad ? "bg-red-400" : "bg-slate-400";
  return (
    <div className="relative h-4 w-40" aria-label={`interval ${l} to ${h}`}>
      <div className="absolute inset-x-0 top-1/2 h-px bg-slate-200" />
      <div className={`absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full ${color}`} style={{ left: x(l), width: `calc(${x(h)} - ${x(l)})` }} />
      {m !== null && <div className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 bg-slate-900" style={{ left: x(m) }} />}
      {t !== null && <div className="absolute inset-y-0 w-px bg-red-500" style={{ left: x(t) }} title="target" />}
    </div>
  );
}
