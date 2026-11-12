"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import clsx from "clsx";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { MOCK_API } from "@/lib/config";
import { KillSwitchBadge, ModeBadge } from "./badges";
import { useSession } from "./session";

export const NAV: { href: string; label: string; phase: number }[] = [
  { href: "/", label: "Overview", phase: 1 },
  { href: "/campaigns", label: "Campaigns", phase: 1 },
  { href: "/rankings", label: "Rankings", phase: 2 },
  { href: "/approvals", label: "Approvals", phase: 2 },
  { href: "/changes", label: "Change log", phase: 3 },
  { href: "/ai-usage", label: "AI usage", phase: 4 },
  { href: "/system", label: "System", phase: 1 },
];

export function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const s = useSession();
  const [busy, setBusy] = useState(false);

  async function logout() {
    setBusy(true);
    try {
      await api.logout();
    } catch {
      /* session may already be gone */
    }
    router.replace("/login");
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-slate-950 text-slate-300 md:flex">
        <div className="px-5 py-5">
          <div className="text-sm font-semibold tracking-tight text-white">ads-autopilot</div>
          <div className="mt-0.5 truncate text-xs text-slate-400">{s.clientName ?? "—"}</div>
        </div>
        <nav className="flex-1 space-y-0.5 px-3">
          {NAV.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className={clsx(
                "flex items-center justify-between rounded-lg px-3 py-2 text-sm transition",
                isActive(pathname, n.href) ? "bg-white/10 font-medium text-white" : "hover:bg-white/5 hover:text-white",
              )}
            >
              <span>{n.label}</span>
              {n.href === "/approvals" && !!s.openApprovals && (
                <span className="rounded-full bg-amber-400 px-1.5 text-[11px] font-semibold text-slate-900">{s.openApprovals}</span>
              )}
            </Link>
          ))}
        </nav>
        <div className="px-5 py-4 text-[11px] leading-relaxed text-slate-500">
          {[s.currency, s.timezone].filter(Boolean).join(" · ")}
          {MOCK_API && <div className="mt-1 font-semibold text-amber-400">Mock mode · example data</div>}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-6 py-3">
          <nav className="flex gap-1 overflow-x-auto md:hidden">
            {NAV.map((n) => (
              <Link key={n.href} href={n.href} className={clsx("rounded px-2 py-1 text-xs", isActive(pathname, n.href) ? "bg-slate-900 text-white" : "text-slate-600")}>
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            {s.mode && <ModeBadge mode={s.mode} hardMax={s.hardMaxMode} />}
            {s.killSwitch && <KillSwitchBadge state={s.killSwitch} />}
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-600">
            {s.user && (
              <span>
                {s.user.username} <span className="text-slate-400">· {s.user.role}</span>
              </span>
            )}
            <button onClick={logout} disabled={busy} className="rounded-lg px-2.5 py-1 text-sm text-slate-600 hover:bg-slate-100">
              Log out
            </button>
          </div>
        </header>
        <main className="min-w-0 flex-1 px-6 py-6">{children}</main>
      </div>
    </div>
  );
}
