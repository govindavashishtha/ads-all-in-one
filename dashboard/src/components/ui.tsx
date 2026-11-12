import clsx from "clsx";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <section className={clsx("min-w-0 rounded-xl border border-slate-200 bg-white shadow-sm", className)}>{children}</section>
  );
}

export function CardHeader({ title, subtitle, right }: { title: ReactNode; subtitle?: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-5 py-3.5">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function CardBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx("px-5 py-4", className)}>{children}</div>;
}

export type Tone = "slate" | "green" | "amber" | "red" | "blue" | "violet";
const toneCls: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-700 ring-slate-200",
  green: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  red: "bg-red-50 text-red-700 ring-red-200",
  blue: "bg-sky-50 text-sky-700 ring-sky-200",
  violet: "bg-violet-50 text-violet-700 ring-violet-200",
};

export function Badge({ tone = "slate", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset", toneCls[tone])}
    >
      {children}
    </span>
  );
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost"; size?: "sm" | "md" }) {
  return (
    <button
      {...rest}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-2 text-sm",
        variant === "primary" && "bg-slate-900 text-white hover:bg-slate-700",
        variant === "secondary" && "border border-slate-300 bg-white text-slate-800 hover:bg-slate-50",
        variant === "danger" && "bg-red-600 text-white hover:bg-red-500",
        variant === "ghost" && "text-slate-600 hover:bg-slate-100",
        className,
      )}
    />
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "ok" | "warn" | "danger" }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div
        className={clsx(
          "mt-1 text-2xl font-semibold tabular-nums",
          tone === "danger" ? "text-red-600" : tone === "warn" ? "text-amber-600" : "text-slate-900",
        )}
      >
        {value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-lg bg-slate-100", className)} />;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="text-sm font-medium text-slate-700">{title}</div>
      {children && <div className="mt-1 max-w-md text-sm text-slate-500">{children}</div>}
    </div>
  );
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th className={clsx("whitespace-nowrap px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500", className)}>
      {children}
    </th>
  );
}
export function Td({ children, className, colSpan }: { children?: ReactNode; className?: string; colSpan?: number }) {
  return <td colSpan={colSpan} className={clsx("whitespace-nowrap px-3 py-2.5 text-sm text-slate-700", className)}>{children}</td>;
}
