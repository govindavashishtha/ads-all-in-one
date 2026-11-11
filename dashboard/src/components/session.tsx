"use client";

import { createContext, useContext, type ReactNode } from "react";
import { api } from "@/lib/api";
import { targetsFromOverview } from "@/lib/overview";
import type { KillSwitchState, Mode, Role, Targets, User } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export interface SessionValue {
  user: User | undefined;
  clientName: string | undefined;
  /** Account currency from GET /overview — used for all money formatting. */
  currency: string | undefined;
  /** Ad-account timezone (GET /system → ad_accounts[0].timezone); metric dates are account-local. */
  timezone: string | undefined;
  mode: Mode | undefined;
  hardMaxMode: Mode | undefined;
  killSwitch: KillSwitchState | undefined;
  openApprovals: number | undefined;
  targets: Targets | undefined;
  refresh: () => void;
}

const empty: SessionValue = {
  user: undefined,
  clientName: undefined,
  currency: undefined,
  timezone: undefined,
  mode: undefined,
  hardMaxMode: undefined,
  killSwitch: undefined,
  openApprovals: undefined,
  targets: undefined,
  refresh: () => {},
};
const Ctx = createContext<SessionValue>(empty);

const roleRank: Record<Role, number> = { viewer: 0, approver: 1, admin: 2 };
export function hasRole(user: User | undefined, required: Role): boolean {
  return !!user && roleRank[user.role] >= roleRank[required];
}

/** Loads the user and client-wide context (currency, timezone, mode, kill switch, targets). */
export function SessionProvider({ children }: { children: ReactNode }) {
  const me = useApi(() => api.me(), []);
  // trend_days=1: the session only needs the header facts; the Overview page loads the full view.
  const ov = useApi(() => api.overview("7d", 1), [], { refreshMs: 60_000 });
  const value: SessionValue = {
    user: me.data,
    clientName: ov.data?.client_name,
    currency: ov.data?.currency,
    timezone: ov.data ? (ov.data.platforms.find((p) => p.timezone)?.timezone ?? ov.data.timezone) : undefined,
    mode: ov.data?.mode,
    hardMaxMode: ov.data?.hard_max_mode,
    killSwitch: ov.data?.kill_switch,
    openApprovals: ov.data?.open_approvals,
    targets: ov.data ? targetsFromOverview(ov.data) : undefined,
    refresh: () => {
      me.reload();
      ov.reload();
    },
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionValue {
  return useContext(Ctx);
}
