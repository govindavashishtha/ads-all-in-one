import { describe, expect, it } from "vitest";
import { peerGroupDescription, peerGroupLabel, rankingParentLine } from "@/lib/rankings";

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
    expect(peerGroupLabel("ad|ad_group:1", 1)).toBe("Ads in ad group 1 · 1 entity");
    expect(peerGroupLabel("ad|ad_group:2", 2)).toBe("Ads in ad group 2 · 2 entities");
  });
  it("uses parent or campaign names when provided", () => {
    expect(
      peerGroupLabel("ad|ad_group:1", 2, { parent_name: "SYN ad set 910001", platform: "meta" }),
    ).toBe("Ads in SYN ad set 910001 · 2 entities");
    expect(
      peerGroupLabel("ad_group|campaign:1", 3, { campaign_name: "SYN Prospecting (CBO)", platform: "meta" }),
    ).toBe("Ad sets in SYN Prospecting (CBO) · 3 entities");
    expect(
      peerGroupLabel("ad_group|campaign:5", 2, { campaign_name: "SYN Brand Search", platform: "google" }),
    ).toBe("Ad groups in SYN Brand Search · 2 entities");
    expect(
      peerGroupLabel("keyword|ad_group:7|match:broad", 4, { parent_name: "SYN Brand" }),
    ).toBe("Keywords in SYN Brand · broad match · 4 entities");
  });
  it("labels campaign peer groups by platform and objective", () => {
    expect(peerGroupDescription("campaign|meta|objective:OUTCOME_SALES")).toBe("Meta campaigns · Sales");
    expect(peerGroupDescription("campaign|google|objective:PERFORMANCE_MAX")).toBe("Google campaigns · Performance Max");
    expect(peerGroupDescription("campaign|google|objective:SEARCH")).toBe("Google campaigns · Search");
    expect(peerGroupLabel("campaign|meta|objective:OUTCOME_SALES", 3)).toBe(
      "Meta campaigns · Sales · 3 entities",
    );
  });
});
