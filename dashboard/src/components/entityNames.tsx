import Link from "next/link";
import { humanize } from "@/lib/format";
import type { EntityType } from "@/lib/types";

/**
 * Entity name as returned by the API (`entity_name`, `campaign_id`, `campaign_name` on
 * proposals and changes). Campaign-level rows link to the campaign; lower levels show their
 * owning campaign underneath.
 */
export function EntityLabel({
  type,
  id,
  name,
  campaignId,
  campaignName,
}: {
  type: EntityType;
  id: number;
  name?: string | null;
  campaignId?: number | null;
  campaignName?: string | null;
}) {
  const label = name ?? `${humanize(type)} #${id}`;
  if (type === "campaign") {
    return (
      <Link href={`/campaigns/${id}`} className="font-medium text-slate-800 hover:underline">
        {label}
      </Link>
    );
  }
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1">
      <span className="text-xs text-slate-500">{humanize(type)}</span>
      <span className="font-medium text-slate-800">{label}</span>
      {campaignId != null && (
        <Link href={`/campaigns/${campaignId}`} className="text-xs text-slate-500 hover:underline">
          ({campaignName ?? `campaign #${campaignId}`})
        </Link>
      )}
    </span>
  );
}
