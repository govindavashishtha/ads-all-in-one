"use client";

import Link from "next/link";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, formatMoney, humanize, relativeTime } from "@/lib/format";
import type { DecisionResponse, GuardrailResult, Proposal } from "@/lib/types";
import { EntityLabel } from "./entityNames";
import { PlatformBadge, RiskBadge, StatusBadge } from "./badges";
import { Badge, Button, Card } from "./ui";

const MONEY_KEYS = /budget|cost|spend|cpa|amount/i;

export function fmtValue(key: string, v: unknown, currency?: string): string {
  if (v === null || v === undefined) return "—";
  if (MONEY_KEYS.test(key) && (typeof v === "number" || (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))))) {
    return formatMoney(v as string, currency);
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** before → after from the API's `change` (field, before, after; money in major units). */
export function diffRows(p: Proposal): { key: string; before: unknown; after: unknown; estimate: boolean }[] {
  const c = p.change;
  if (!c || c.field === "none") return [];
  return [{ key: c.field, before: c.before, after: c.after, estimate: !!c.after_is_estimate }];
}

/** One-line guardrail summary from the typed `guardrail_result`. */
export function guardrailText(g: GuardrailResult | null | undefined): string | null {
  if (!g) return null;
  if (g.result === "block") return `Blocked (${humanize(g.reason ?? "unknown")})`;
  if (g.result === "needs_approval") return `Needs approval (${(g.reasons ?? []).map(humanize).join(", ") || "—"})`;
  return "Allowed";
}

export function ProposalCard({
  p,
  currency,
  timezone,
  canDecide,
  selectable,
  selected,
  onSelect,
  onDecided,
  linkToDetail = true,
}: {
  p: Proposal;
  currency?: string;
  timezone?: string;
  canDecide: boolean;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (v: boolean) => void;
  onDecided?: (r: DecisionResponse) => void;
  linkToDetail?: boolean;
}) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const awaiting = p.status === "awaiting_approval";
  const g = p.guardrail_result;
  const params = (p.params ?? {}) as Record<string, unknown>;
  const pctRaw = params.change_pct;
  const pct = typeof pctRaw === "number" ? pctRaw : typeof pctRaw === "string" && pctRaw.trim() !== "" && !Number.isNaN(Number(pctRaw)) ? Number(pctRaw) : null;
  const [nextStep, setNextStep] = useState<string | null>(null);

  async function decide(kind: "approve" | "reject") {
    setBusy(kind);
    setErr(null);
    try {
      const r = kind === "approve" ? await api.approve(p.id) : await api.reject(p.id, reason.trim());
      setRejecting(false);
      setNextStep(r.next_step);
      onDecided?.(r);
    } catch (e) {
      setErr(e instanceof ApiError ? (e.forbidden ? "Approver role required." : e.message) : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card className={selected ? "ring-2 ring-slate-900" : undefined}>
      <div className="flex gap-4 px-5 py-4" data-testid={`proposal-${p.id}`}>
        {selectable && (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4"
            aria-label={`Select proposal ${p.id}`}
            checked={!!selected}
            disabled={!awaiting}
            onChange={(e) => onSelect?.(e.target.checked)}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-400">#{p.id}</span>
            {p.platform && <PlatformBadge platform={p.platform} />}
            <span className="font-semibold text-slate-900">
              {humanize(p.action)}
              {pct !== null && ` ${pct > 0 ? "+" : ""}${pct}%`}
            </span>
            <span className="text-slate-500">on</span>
            <EntityLabel type={p.entity_type} id={p.entity_id} name={p.entity_name} campaignId={p.campaign_id} campaignName={p.campaign_name} />
            <StatusBadge status={p.status} />
            <RiskBadge risk={p.risk} />
            <Badge tone={p.spend_direction === "increasing" ? "amber" : p.spend_direction === "reducing" ? "green" : "slate"}>
              {humanize(p.spend_direction)} spend
            </Badge>
          </div>

          <div className="mt-3 grid gap-4 md:grid-cols-3">
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Before → after</div>
              {diffRows(p).length === 0 ? (
                <div className="text-sm text-slate-500">—</div>
              ) : (
                diffRows(p).map((d) => (
                  <div key={d.key} className="mt-1 text-sm">
                    <span className="text-slate-500">{humanize(d.key)}: </span>
                    <span className="tabular-nums text-slate-500 line-through decoration-slate-300">{fmtValue(d.key, d.before, currency)}</span>
                    <span className="mx-1 text-slate-400">→</span>
                    <span className="font-medium tabular-nums text-slate-900">{fmtValue(d.key, d.after, currency)}</span>
                    {d.estimate && awaiting && <span className="ml-1 text-xs text-slate-400" title="The executor re-runs the guardrail on fresh data before applying">(re-checked before applying)</span>}
                  </div>
                ))
              )}
            </div>
            <div className="md:col-span-2">
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Reason</div>
              <p className="mt-1 text-sm text-slate-700">{p.reason}</p>
              {p.expected_impact && Object.keys(p.expected_impact).length > 0 && (
                <p className="mt-1 text-xs text-slate-500">
                  Expected:{" "}
                  {Object.entries(p.expected_impact)
                    .map(([k, v]) => `${humanize(k)} ${fmtValue(k, v, currency)}`)
                    .join(" · ")}
                </p>
              )}
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <span>Source: {humanize(p.source)}{p.rule_key ? ` · rule ${p.rule_key}` : ""}</span>
            {g && (
              <span title={g.detail && typeof g.detail.why === "string" ? g.detail.why : undefined}>
                Guardrail: {guardrailText(g)}
                {g.checked_at ? ` · checked ${formatDateTime(g.checked_at, timezone)}` : ""}
              </span>
            )}
            {p.expires_at && awaiting && <span>Expires {relativeTime(p.expires_at)} ({formatDateTime(p.expires_at, timezone)})</span>}
            {p.created_at && <span>Created {formatDateTime(p.created_at, timezone)}</span>}
            {p.approval?.decision && (
              <span>
                {humanize(p.approval.decision)} by {p.approval.decided_by ?? "—"} {p.approval.decided_at ? `· ${formatDateTime(p.approval.decided_at, timezone)}` : ""}
                {p.approval.channel ? ` via ${p.approval.channel}` : ""}
                {p.approval.comment ? ` — “${p.approval.comment}”` : ""}
              </span>
            )}
            {linkToDetail && (
              <Link href={`/approvals/${p.id}`} className="text-slate-700 underline-offset-2 hover:underline">
                Details
              </Link>
            )}
          </div>

          {nextStep && (
            <div role="status" className="mt-2 text-sm text-slate-600">
              Next: {nextStep}
            </div>
          )}
          {err && (
            <div role="alert" className="mt-2 text-sm text-red-600">
              {err}
            </div>
          )}

          {awaiting && canDecide && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {!rejecting ? (
                <>
                  <Button variant="primary" size="sm" disabled={!!busy} onClick={() => decide("approve")}>
                    {busy === "approve" ? "Approving…" : "Approve"}
                  </Button>
                  <Button size="sm" disabled={!!busy} onClick={() => setRejecting(true)}>
                    Reject
                  </Button>
                  <span className="text-xs text-slate-400">The guardrail runs again with fresh data before anything executes.</span>
                </>
              ) : (
                <>
                  <input
                    autoFocus
                    aria-label="Rejection reason"
                    placeholder="Reason (required)"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="w-72 rounded-lg border border-slate-300 px-2.5 py-1 text-sm"
                  />
                  <Button variant="danger" size="sm" disabled={!!busy || !reason.trim()} onClick={() => decide("reject")}>
                    {busy === "reject" ? "Rejecting…" : "Confirm reject"}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setRejecting(false)}>
                    Cancel
                  </Button>
                </>
              )}
            </div>
          )}
          {awaiting && !canDecide && <div className="mt-3 text-xs text-slate-500">Approver or admin role is needed to decide.</div>}
        </div>
      </div>
    </Card>
  );
}
