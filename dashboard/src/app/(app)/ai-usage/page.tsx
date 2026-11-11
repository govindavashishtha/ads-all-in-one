"use client";

import { api } from "@/lib/api";
import { capRatio, formatNumber, formatUsd, humanize, paceTone } from "@/lib/format";
import type { LlmUsageTotals } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { TokensChart } from "@/components/charts/charts";
import { CapBar } from "@/components/charts/BudgetGauge";
import { ExampleTag } from "@/components/MockBanner";
import { NotEnabled, QueryState } from "@/components/states";
import { Card, CardBody, CardHeader, PageHeader, Stat, Td, Th } from "@/components/ui";

const tokens = (t: LlmUsageTotals) => (t.input_tokens ?? 0) + (t.output_tokens ?? 0);

export default function AiUsagePage() {
  const q = useApi(() => api.llmUsage("30d"), []);
  return (
    <>
      <PageHeader title="AI usage" subtitle="LLM tokens and cost against the daily and monthly caps (§13.3). Costs are in USD, as set by the LLM_* env prices." />
      <QueryState state={q} feature="AI usage" phase={4}>
        {(u) => {
          if (!u.llm_enabled && u.days.length === 0) {
            return (
              <NotEnabled feature="AI usage" phase={4}>
                The LLM triage layer is off (LLM_ENABLED=false). Caps when enabled: {formatNumber(u.daily_token_cap)} tokens/day,{" "}
                {formatUsd(u.daily_cost_cap_usd)}/day, {formatUsd(u.monthly_cost_cap_usd)}/month.
              </NotEnabled>
            );
          }
          const tokRatio = u.daily_token_cap ? tokens(u.today) / u.daily_token_cap : null;
          const purposes = Object.entries(u.by_purpose).sort((a, b) => tokens(b[1]) - tokens(a[1]));
          return (
            <div className="space-y-6">
              <Card>
                <CardHeader title="Today and month to date" right={<ExampleTag />} />
                <CardBody className="grid grid-cols-2 gap-6 md:grid-cols-4">
                  <Stat
                    label="Tokens today"
                    value={formatNumber(tokens(u.today))}
                    sub={u.daily_token_cap ? `of ${formatNumber(u.daily_token_cap)} cap` : "no cap"}
                    tone={paceTone(tokRatio) === "danger" ? "danger" : paceTone(tokRatio) === "warn" ? "warn" : undefined}
                  />
                  <Stat label="Cost today" value={formatUsd(u.today.cost_usd, 3)} sub={`${u.today.calls ?? 0} calls`} />
                  <Stat label="Tokens MTD" value={formatNumber(tokens(u.month_to_date))} sub={`${u.month_to_date.calls ?? 0} calls`} />
                  <Stat
                    label="Cost MTD"
                    value={formatUsd(u.month_to_date.cost_usd)}
                    tone={paceTone(capRatio(u.month_to_date.cost_usd, u.monthly_cost_cap_usd)) === "danger" ? "danger" : undefined}
                  />
                </CardBody>
                <CardBody className="grid gap-4 border-t border-slate-100 md:grid-cols-2">
                  <CapBar label="Daily cost cap (USD)" spend={u.today.cost_usd} cap={u.daily_cost_cap_usd} currency="USD" />
                  <CapBar label="Monthly cost cap (USD)" spend={u.month_to_date.cost_usd} cap={u.monthly_cost_cap_usd} currency="USD" />
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Tokens per day by tier" subtitle={`Input + output tokens, last ${u.range_days} days`} right={<ExampleTag />} />
                <CardBody>
                  <TokensChart days={u.days} tokenCap={u.daily_token_cap} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="By purpose" subtitle={`Last ${u.range_days} days`} />
                <table className="min-w-full divide-y divide-slate-100">
                  <thead className="bg-slate-50/60">
                    <tr>
                      <Th>Purpose</Th>
                      <Th className="text-right">Calls</Th>
                      <Th className="text-right">Input tokens</Th>
                      <Th className="text-right">Cached input</Th>
                      <Th className="text-right">Output tokens</Th>
                      <Th className="text-right">Cost</Th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {purposes.map(([purpose, r]) => (
                      <tr key={purpose}>
                        <Td>{humanize(purpose)}</Td>
                        <Td className="text-right tabular-nums">{formatNumber(r.calls)}</Td>
                        <Td className="text-right tabular-nums">{formatNumber(r.input_tokens)}</Td>
                        <Td className="text-right tabular-nums">{formatNumber(r.cached_input_tokens)}</Td>
                        <Td className="text-right tabular-nums">{formatNumber(r.output_tokens)}</Td>
                        <Td className="text-right tabular-nums">{formatUsd(r.cost_usd, 3)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
