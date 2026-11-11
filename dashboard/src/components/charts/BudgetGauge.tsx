import clsx from "clsx";
import { capRatio, formatMoney, formatPct, paceTone, type PaceTone } from "@/lib/format";
import type { Money } from "@/lib/types";

const toneStroke: Record<PaceTone, string> = { ok: "#10b981", warn: "#f59e0b", danger: "#ef4444", unknown: "#94a3b8" };

/** Semicircle gauge: spend vs cap. Colour bands mirror §12.4 warn (85%) / emergency (97%). */
export function BudgetGauge({
  label,
  spend,
  cap,
  currency,
  sub,
}: {
  label: string;
  spend: Money | null | undefined;
  cap: Money | null | undefined;
  currency?: string;
  sub?: string;
}) {
  const ratio = capRatio(spend, cap);
  const tone = paceTone(ratio);
  const clamped = Math.min(Math.max(ratio ?? 0, 0), 1);
  const r = 52;
  const len = Math.PI * r;
  return (
    <div className="flex flex-col items-center" data-testid="budget-gauge" data-tone={tone}>
      <svg viewBox="0 0 128 72" className="w-36" role="img" aria-label={`${label}: ${ratio === null ? "no cap" : formatPct(ratio, 0)} of cap`}>
        <path d="M 12 66 A 52 52 0 0 1 116 66" fill="none" stroke="#e2e8f0" strokeWidth="11" strokeLinecap="round" />
        <path
          d="M 12 66 A 52 52 0 0 1 116 66"
          fill="none"
          stroke={toneStroke[tone]}
          strokeWidth="11"
          strokeLinecap="round"
          strokeDasharray={`${clamped * len} ${len}`}
        />
        <text x="64" y="58" textAnchor="middle" className="fill-slate-900" fontSize="17" fontWeight="600">
          {ratio === null ? "—" : formatPct(ratio, 0)}
        </text>
      </svg>
      <div className="mt-1 text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={clsx("text-sm tabular-nums", tone === "danger" ? "text-red-600" : "text-slate-800")}>
        {formatMoney(spend, currency)} <span className="text-slate-400">/ {cap == null ? "no cap" : formatMoney(cap, currency)}</span>
      </div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

/** Horizontal progress bar (month-to-date vs monthly cap). */
export function CapBar({ label, spend, cap, currency }: { label: string; spend: Money | null | undefined; cap: Money | null | undefined; currency?: string }) {
  const ratio = capRatio(spend, cap);
  const tone = paceTone(ratio);
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium text-slate-700">{label}</span>
        <span className="tabular-nums text-slate-600">
          {formatMoney(spend, currency)} <span className="text-slate-400">/ {cap == null ? "no cap" : formatMoney(cap, currency)}</span>
        </span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-slate-100">
        <div className="h-full rounded-full" style={{ width: `${Math.min((ratio ?? 0) * 100, 100)}%`, background: toneStroke[tone] }} />
      </div>
    </div>
  );
}
