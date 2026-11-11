"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { Campaign, EntityType } from "@/lib/types";
import { useApi } from "@/lib/useApi";

/**
 * ProposalOut / ChangeOut carry only entity_type + entity_id (no name, no platform).
 * Campaign names and platforms are resolved from GET /campaigns; other levels show "Ad #id".
 */
export function useCampaignIndex(): Map<number, Campaign> {
  const q = useApi(() => api.campaigns({ range: "7d" }), []);
  return new Map((q.data ?? []).map((c) => [c.id, c]));
}

export function EntityLabel({ type, id, campaigns }: { type: EntityType; id: number; campaigns: Map<number, Campaign> }) {
  if (type === "campaign") {
    const c = campaigns.get(id);
    return (
      <Link href={`/campaigns/${id}`} className="font-medium text-slate-800 hover:underline">
        {c?.name ?? `Campaign #${id}`}
      </Link>
    );
  }
  return (
    <span className="font-medium text-slate-800">
      {humanize(type)} #{id}
    </span>
  );
}
