"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Proposal, ProposalStatus } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { ExampleTag } from "@/components/MockBanner";
import { useCampaignIndex } from "@/components/entityNames";
import { ProposalCard } from "@/components/ProposalCard";
import { hasRole, useSession } from "@/components/session";
import { QueryState } from "@/components/states";
import { Button, Card, EmptyState, PageHeader } from "@/components/ui";

const TABS: { id: ProposalStatus | "all"; label: string }[] = [
  { id: "awaiting_approval", label: "Awaiting approval" },
  { id: "blocked", label: "Blocked" },
  { id: "executed", label: "Executed" },
  { id: "rejected", label: "Rejected" },
  { id: "all", label: "All" },
];

export default function ApprovalsPage() {
  const s = useSession();
  const [tab, setTab] = useState<ProposalStatus | "all">("awaiting_approval");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const q = useApi(() => api.proposals(tab === "all" ? undefined : tab), [tab]);
  const canDecide = hasRole(s.user, "approver");
  const campaigns = useCampaignIndex();

  async function approveSelected() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.approveBatch([...selected]);
      const ok = r.results.filter((x) => x.ok).length;
      const failed = r.results.filter((x) => !x.ok).map((x) => `#${x.proposal_id}: ${x.error ?? x.status}`);
      setMsg(`${ok} of ${r.results.length} approved. ${failed.length ? `Not approved: ${failed.join("; ")}. ` : ""}${r.next_step}`);
      setSelected(new Set());
      q.reload();
      s.refresh();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Batch approval failed");
    } finally {
      setBusy(false);
    }
  }

  function onDecided() {
    q.reload();
    s.refresh();
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Proposals the guardrail routed to a human (§14). Links in notifications only open this page; decisions are an authenticated POST."
        right={<ExampleTag />}
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <Button key={t.id} size="sm" variant={t.id === tab ? "primary" : "secondary"} onClick={() => { setTab(t.id); setSelected(new Set()); }}>
            {t.label}
          </Button>
        ))}
        {canDecide && tab === "awaiting_approval" && (
          <Button size="sm" variant="primary" className="ml-auto" disabled={!selected.size || busy} onClick={approveSelected}>
            {busy ? "Approving…" : `Approve selected (${selected.size})`}
          </Button>
        )}
      </div>
      {msg && <div role="status" className="mb-4 rounded-lg bg-slate-100 px-4 py-2 text-sm text-slate-700">{msg}</div>}
      <QueryState state={q} feature="Approvals" phase={2}>
        {(list: Proposal[]) =>
          list.length === 0 ? (
            <Card>
              <EmptyState title={tab === "awaiting_approval" ? "Nothing waiting for you" : "No proposals"}>
                New proposals are created by the daily pipeline.
              </EmptyState>
            </Card>
          ) : (
            <div className="space-y-3">
              {list.map((p) => (
                <ProposalCard
                  key={p.id}
                  p={p}
                  currency={s.currency}
                  timezone={s.timezone}
                  canDecide={canDecide}
                  selectable={canDecide && tab === "awaiting_approval"}
                  selected={selected.has(p.id)}
                  onSelect={(v) => {
                    const n = new Set(selected);
                    if (v) n.add(p.id);
                    else n.delete(p.id);
                    setSelected(n);
                  }}
                  onDecided={onDecided}
                  campaigns={campaigns}
                />
              ))}
            </div>
          )
        }
      </QueryState>
    </>
  );
}
