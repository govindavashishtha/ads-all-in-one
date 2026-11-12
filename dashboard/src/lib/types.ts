/**
 * API types for /api/v1 — aliases of the schemas generated from docs/openapi.json
 * (`npm run gen:api` → openapi.gen.ts). Do not hand-edit response shapes here: regenerate.
 *
 * Money is a JSON string in major units (Decimal); ratios are numbers.
 */
import type { components } from "./openapi.gen";

type S = components["schemas"];

export type Money = string | number;

export type Platform = S["Platform"];
export type Role = S["Role"];
export type Mode = S["Mode"];
export type EntityType = S["EntityType"];
export type Severity = S["Severity"];
export type ProposalStatus = S["ProposalStatus"];
export type Risk = S["Risk"];

export type User = S["MeResponse"];
export type LoginResponse = S["LoginResponse"];
export type KillSwitchState = S["KillSwitchState"];

export type Overview = S["OverviewResponse"];
export type SpendBlock = S["SpendBlock"];
export type TrendPoint = S["TrendPoint"];
export type IntradayPoint = S["IntradayPoint"];
export type TargetsOut = S["TargetsOut"];
export type RangeMetrics = S["RangeMetrics"];
export type AlertCounts = S["AlertCounts"];
export type Alert = S["AlertOut"];
export type EmergencyAction = S["EmergencyActionOut"];

export type Campaign = S["CampaignOut"];
export type CampaignDetail = S["CampaignDetail"];
export type AdGroup = S["AdGroupOut"];
export type AdGroupDetail = S["AdGroupDetail"];
export type Ad = S["AdOut"];
export type Keyword = S["KeywordOut"];
export type Score = S["ScoreOut"];

export type MetricsSeries = S["MetricsSeries"];
export type MetricPoint = S["MetricsPoint"];

export type RankLevel = "ad" | "ad_group" | "keyword" | "campaign";
export type Rankings = S["RankingsResponse"];
export type RankingRow = S["RankingRow"];

export type Proposal = S["ProposalOut"];
export type GuardrailResult = S["GuardrailResultOut"];
export type Triage = S["TriageOut"];
export type ValueChange = S["ValueChange"];
export type Approval = S["ApprovalOut"];
export type DecisionResponse = S["DecisionResponse"];
export type BatchDecisionResponse = S["BatchDecisionResponse"];

export type Change = S["ChangeOut"];
export type Evaluation = S["EvaluationOut"];

export type LlmUsage = S["LLMUsageResponse"];
export type LlmUsageDay = S["LLMUsageDay"];
export type LlmUsageTotals = S["LLMUsageTotals"];

export type SystemStatus = S["SystemResponse"];
export type JobRun = S["JobRunOut"];
export type AdAccount = S["AdAccountOut"];
export type Heartbeat = S["HeartbeatOut"];
export type PlatformOperations = S["PlatformOperationsOut"];
export type JobRequest = S["JobRequestOut"];
export type WriteRequest = S["WriteRequestOut"];
export type ExecutionState = S["ExecutionState"];
export type DryRun = S["DryRunResponse"];
export type DryRunOp = S["DryRunOp"];

export type ConfigResponse = S["ConfigResponse"];

/** Objective + targets of the active rules version (§11.2); from `overview.targets` /
 * `rankings.targets`, or parsed from `GET /rules` (session-wide default). */
export interface Targets {
  objective: "conversions" | "value" | "traffic" | null;
  target_cpa: Money | null;
  target_roas: Money | null;
}
