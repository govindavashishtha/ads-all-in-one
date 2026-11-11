"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Proposal } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useCampaignIndex } from "@/components/entityNames";
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
  const [dry, setDry] = useState<string | null>(null);
  const campaigns = useCampaignIndex();

  async function loadDryRun() {
    try {
      setDry(JSON.stringify(await api.dryRun(Number(id)), null, 2));
    } catch (e) {
      setDry(
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
              campaigns={campaigns}
              onDecided={(r) => {
                setDecided(r.proposal);
                s.refresh();
              }}
              linkToDetail={false}
            />
            <Card>
              <CardHeader title="Dry run" subtitle="The payload the executor would send (GET /proposals/{id}/dry-run)" right={<Button size="sm" onClick={loadDryRun}>Load</Button>} />
              {dry && (
                <CardBody>
                  <pre className="overflow-x-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">{dry}</pre>
                </CardBody>
              )}
            </Card>
          </div>
        )}
      </QueryState>
    </>
  );
}
