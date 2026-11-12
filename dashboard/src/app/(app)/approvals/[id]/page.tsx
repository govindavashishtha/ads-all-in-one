"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { DryRun, Proposal } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { ProposalCard } from "@/components/ProposalCard";
import { hasRole, useSession } from "@/components/session";
import { QueryState } from "@/components/states";
import { Button, Card, CardBody, CardHeader, PageHeader } from "@/components/ui";

/**
 * Target of approval links in notifications (§14). A GET only shows the page — chat apps
 * pre-fetch links — and approving needs a logged-in POST with the CSRF token.
 */
export default function ApprovalDetailPage() {
  const { id } = useParams<{ id: string }>();
  const s = useSession();
  const q = useApi(() => api.proposal(Number(id)), [id]);
  const [decided, setDecided] = useState<Proposal | null>(null);
  const [dry, setDry] = useState<DryRun | null>(null);
  const [dryErr, setDryErr] = useState<string | null>(null);

  async function loadDryRun() {
    setDryErr(null);
    try {
      setDry(await api.dryRun(Number(id)));
    } catch (e) {
      setDry(null);
      setDryErr(
        e instanceof ApiError && e.notEnabled
          ? `Dry run is not enabled yet${e.phase ? ` (Phase ${e.phase}, executor)` : ""}: ${e.message}`
          : `Dry run unavailable: ${(e as Error).message}`,
      );
    }
  }

  return (
    <>
      <div className="mb-2 text-sm">
        <Link href="/approvals" className="text-slate-500 hover:text-slate-800">← Approvals</Link>
      </div>
      <PageHeader title={`Proposal #${id}`} />
      <QueryState state={q} feature="Approvals" phase={2}>
        {(p) => (
          <div className="space-y-4">
            <ProposalCard
              p={decided ?? p}
              currency={s.currency}
              timezone={s.timezone}
              canDecide={hasRole(s.user, "approver")}
              onDecided={(r) => {
                setDecided(r.proposal);
                s.refresh();
              }}
              linkToDetail={false}
            />
            <Card>
              <CardHeader title="Dry run" subtitle="The payload the executor would send (GET /proposals/{id}/dry-run)" right={<Button size="sm" onClick={loadDryRun}>Load</Button>} />
              {(dry || dryErr) && (
                <CardBody className="space-y-3 text-sm">
                  {dryErr && <p className="text-red-700">{dryErr}</p>}
                  {dry && (
                    <>
                      <p>
                        Guardrail: <span className="font-medium">{String(dry.guardrail?.result ?? "—")}</span>
                        {dry.guardrail?.reason ? ` (${String(dry.guardrail.reason)})` : ""}
                        {dry.new_daily_budget != null ? ` · planned daily budget ${dry.new_daily_budget}` : ""}
                      </p>
                      <p className="text-slate-600">
                        Write gate {dry.execution.write_gate_open ? "open" : `CLOSED — ${dry.execution.write_gate_reason}`}
                      </p>
                      {dry.plan_error && <p className="text-red-700">Plan error: {dry.plan_error}</p>}
                      {(dry.ops ?? []).length === 0 ? (
                        <p className="text-slate-500">No writes would be sent.</p>
                      ) : (
                        <ul className="space-y-1 font-mono text-xs">
                          {(dry.ops ?? []).map((op, i) => (
                            <li key={i}>
                              {op.platform} {op.entity_type} {op.external_id}: {op.op}
                              {op.would_change ? "" : " (already so: no-op)"}
                            </li>
                          ))}
                        </ul>
                      )}
                      <pre className="overflow-x-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">{JSON.stringify(dry, null, 2)}</pre>
                    </>
                  )}
                </CardBody>
              )}
            </Card>
          </div>
        )}
      </QueryState>
    </>
  );
}
