import { describe, expect, it } from "vitest";
import { capRatio, formatMoney, formatPct, formatRoas, localDateMinus, paceTone, toNum } from "@/lib/format";

describe("format", () => {
  it("formats INR with Indian digit grouping from the account currency", () => {
    expect(formatMoney("129670.00", "INR")).toBe("₹1,29,670");
    expect(formatMoney("406.77", "INR")).toBe("₹406.77");
  });
  it("does not hardcode the currency", () => {
    expect(formatMoney(1234.5, "USD")).toMatch(/\$1,235|US\$1,235/);
    expect(formatMoney(12, "EUR")).toContain("12.00");
  });
  it("handles missing values", () => {
    expect(formatMoney(null, "INR")).toBe("—");
    expect(formatMoney("abc", "INR")).toBe("—");
    expect(toNum("")).toBeNull();
    expect(formatRoas(undefined)).toBe("—");
  });
  it("percent and ROAS", () => {
    expect(formatPct(0.1234)).toBe("12.3%");
    expect(formatRoas("3")).toBe("3.00×");
  });
  it("cap ratio and pace tone mirror warn/emergency bands", () => {
    expect(capRatio("850", "1000")).toBeCloseTo(0.85);
    expect(capRatio("1", null)).toBeNull();
    expect(capRatio("1", "0")).toBeNull();
    expect(paceTone(0.5)).toBe("ok");
    expect(paceTone(0.86)).toBe("warn");
    expect(paceTone(0.97)).toBe("danger");
    expect(paceTone(null)).toBe("unknown");
  });
  it("computes account-local dates", () => {
    // 2026-11-11 20:00 UTC is already 12 Nov in Kolkata (UTC+5:30)
    const now = new Date("2026-11-11T20:00:00Z");
    expect(localDateMinus(0, "Asia/Kolkata", now)).toBe("2026-11-12");
    expect(localDateMinus(0, "UTC", now)).toBe("2026-11-11");
    expect(localDateMinus(1, "Asia/Kolkata", now)).toBe("2026-11-11");
  });
});
