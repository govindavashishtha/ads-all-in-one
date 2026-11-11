import type { KillSwitchState, Mode, Platform } from "@/lib/types";
import { humanize } from "@/lib/format";
import { Badge, type Tone } from "./ui";

const modeTone: Record<Mode, Tone> = { read_only: "slate", suggest: "blue", auto_small: "violet", auto: "amber" };

export function ModeBadge({ mode, hardMax }: { mode: Mode; hardMax?: Mode }) {
  return (
    <Badge tone={modeTone[mode] ?? "slate"} title={hardMax ? `Hard max mode: ${humanize(hardMax)}` : undefined}>
      Mode: {humanize(mode)}
    </Badge>
  );
}

export function KillSwitchBadge({ state }: { state: KillSwitchState }) {
  if (state.env_override) return <Badge tone="red" title="KILL_SWITCH=true in env: the API cannot clear it">Kill switch ON (env)</Badge>;
  return state.effective ? <Badge tone="red">Kill switch ON</Badge> : <Badge tone="green">Kill switch off</Badge>;
}

export function PlatformBadge({ platform }: { platform: Platform | string | null | undefined }) {
  if (platform === "meta") return <Badge tone="blue">Meta</Badge>;
  if (platform === "google") return <Badge tone="amber">Google</Badge>;
  return <Badge>{humanize(platform ?? "—")}</Badge>;
}

const statusTone: Record<string, Tone> = {
  healthy: "green",
  stale: "red",
  active: "green",
  enabled: "green",
  paused: "slate",
  removed: "slate",
  archived: "slate",
  success: "green",
  ok: "green",
  applied: "green",
  executed: "green",
  approved: "green",
  better: "green",
  running: "blue",
  applying: "blue",
  executing: "blue",
  awaiting_approval: "amber",
  pending: "amber",
  degraded: "amber",
  warn: "amber",
  neutral: "slate",
  insufficient_data: "slate",
  expired: "slate",
  skipped: "slate",
  unconfigured: "slate",
  rejected: "slate",
  rolled_back: "violet",
  blocked: "red",
  failed: "red",
  verify_failed: "red",
  error: "red",
  worse: "red",
  critical: "red",
  emergency: "red",
  info: "blue",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const key = (status ?? "").toLowerCase();
  return <Badge tone={statusTone[key] ?? "slate"}>{humanize(key || "unknown")}</Badge>;
}

export function RiskBadge({ risk }: { risk: string }) {
  return <Badge tone={risk === "high" ? "red" : risk === "medium" ? "amber" : "green"}>{humanize(risk)} risk</Badge>;
}
