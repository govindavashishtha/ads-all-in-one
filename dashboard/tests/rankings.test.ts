import { describe, expect, it } from "vitest";
import { peerGroupLabel, rankingParentLine } from "@/lib/rankings";

describe("rankingParentLine", () => {
  it("keeps the ad-set name when parent_id equals campaign_id (separate id sequences)", () => {
    expect(
      rankingParentLine({
        campaign_id: 1,
        campaign_name: "SYN Prospecting (CBO)",
        parent_id: 1,
        parent_name: "SYN ad set 910001",
      }),
    ).toBe("SYN Prospecting (CBO) › SYN ad set 910001");
  });
  it("does not repeat the campaign name when it is also the parent", () => {
    expect(
      rankingParentLine({
        campaign_id: 5,
        campaign_name: "SYN Brand Search",
        parent_id: 5,
        parent_name: "SYN Brand Search",
      }),
    ).toBe("SYN Brand Search");
  });
  it("falls back to the campaign name when no parent is given", () => {
    expect(rankingParentLine({ campaign_id: 3, campaign_name: "SYN Advantage+ Shopping" })).toBe("SYN Advantage+ Shopping");
  });
});

describe("peerGroupLabel", () => {
  it("uses a singular noun for a single entity", () => {
    expect(peerGroupLabel("ad|ad_group:1", 1)).toBe("Peer group: ad|ad_group:1 · 1 entity");
    expect(peerGroupLabel("ad|ad_group:2", 2)).toBe("Peer group: ad|ad_group:2 · 2 entities");
  });
});
