import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, setTransport } from "@/lib/api";
import type { Proposal } from "@/lib/types";
import { BudgetGauge } from "@/components/charts/BudgetGauge";
import { MockBanner } from "@/components/MockBanner";
import { ProposalCard, diffRows, guardrailText } from "@/components/ProposalCard";
import { QueryState } from "@/components/states";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

afterEach(() => setTransport(null));

const proposal: Proposal = {
  id: 41,
  as_of: "2026-11-10",
  source: "rule",
  rule_key: "scale_winner",
  entity_type: "campaign",
  entity_id: 102,
  action: "budget_change",
  params: { change_pct: 15, current_budget: "2500.00" },
  dedupe_key: "k41",
  expected_impact: null,
  expires_at: null,
  created_at: "2026-11-11T00:40:00Z",
  reason: "Winner",
  risk: "low",
  spend_direction: "increasing",
  status: "awaiting_approval",
  guardrail_result: { result: "needs_approval", reasons: ["mode_suggest"], reason: null, approved: false, mode: "suggest", new_daily_budget: "2875.00", checked_at: null, detail: { current: "2500.00" } },
  entity_name: "Example campaign",
  platform: "google",
  campaign_id: 102,
  campaign_name: "Example campaign",
  change: { field: "daily_budget", before: "2500.00", after: "2875.00", after_is_estimate: true },
};

describe("BudgetGauge", () => {
  it("shows utilisation and colours by band", () => {
    render(<BudgetGauge label="Meta" spend="11000" cap="12000" currency="INR" />);
    expect(screen.getByText("92%")).toBeInTheDocument();
    expect(screen.getByTestId("budget-gauge")).toHaveAttribute("data-tone", "warn");
    expect(screen.getByText(/₹11,000/)).toBeInTheDocument();
  });
  it("handles a missing cap", () => {
    render(<BudgetGauge label="Total" spend="100" cap={null} currency="INR" />);
    expect(screen.getByTestId("budget-gauge")).toHaveAttribute("data-tone", "unknown");
    expect(screen.getByText(/no cap/)).toBeInTheDocument();
  });
});

describe("QueryState", () => {
  const base = { loading: false, reload: () => {} };
  it("renders a 'not enabled yet' state when the phase's API is missing", () => {
    render(
      <QueryState state={{ ...base, data: undefined, error: new ApiError(501, "x", { detail: { code: "not_enabled", phase: "2", message: "x" } }) }} feature="Rankings" phase={2}>
        {() => <div>never</div>}
      </QueryState>,
    );
    expect(screen.getByTestId("not-enabled")).toHaveTextContent("Rankings is not enabled yet");
  });
  it("renders a retryable error otherwise", () => {
    render(
      <QueryState state={{ ...base, data: undefined, error: new ApiError(500, "boom") }} feature="Rankings" phase={2}>
        {() => <div>never</div>}
      </QueryState>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });
  it("renders children with data", () => {
    render(<QueryState state={{ ...base, data: 3, error: null }}>{(n) => <div>value {n}</div>}</QueryState>);
    expect(screen.getByText("value 3")).toBeInTheDocument();
  });
});

describe("MockBanner", () => {
  it("is hidden when mock mode is off", () => {
    const { container } = render(<MockBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ProposalCard", () => {
  it("shows before → after and approves with a POST", async () => {
    const calls: [string, string][] = [];
    setTransport(async (method, path) => {
      calls.push([method, path]);
      return { proposal: { ...proposal, status: "approved" }, next_step: "The executor runs it on its next pass." };
    });
    const onDecided = vi.fn();
    render(<ProposalCard p={proposal} currency="INR" canDecide onDecided={onDecided} />);
    expect(screen.getByText("₹2,500")).toBeInTheDocument();
    expect(screen.getByText("₹2,875")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalled());
    expect(onDecided.mock.calls[0][0].proposal.status).toBe("approved");
    expect(screen.getByText(/executor runs it/)).toBeInTheDocument();
    expect(calls).toEqual([["POST", "/proposals/41/approve"]]);
  });

  it("requires a reason to reject and hides actions without the approver role", async () => {
    setTransport(async () => ({ proposal: { ...proposal, status: "rejected" }, next_step: "" }));
    const { rerender } = render(<ProposalCard p={proposal} canDecide />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    const confirm = screen.getByRole("button", { name: "Confirm reject" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Rejection reason"), "too aggressive");
    expect(confirm).toBeEnabled();
    rerender(<ProposalCard p={proposal} canDecide={false} />);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("renders the API-provided before → after", () => {
    expect(diffRows(proposal)).toEqual([{ key: "daily_budget", before: "2500.00", after: "2875.00", estimate: true }]);
    const pause = { ...proposal, action: "pause" as const, params: null, change: { field: "status" as const, before: "ACTIVE", after: "paused", after_is_estimate: false } };
    expect(diffRows(pause)).toEqual([{ key: "status", before: "ACTIVE", after: "paused", estimate: false }]);
    expect(diffRows({ ...proposal, change: null })).toEqual([]);
  });
});

describe("guardrailText", () => {
  it("summarises the typed guardrail result", () => {
    expect(guardrailText(proposal.guardrail_result)).toBe("Needs approval (Mode suggest)");
    expect(guardrailText({ result: "block", reason: "learning_phase", approved: false })).toBe("Blocked (Learning phase)");
    expect(guardrailText(null)).toBeNull();
  });
});
