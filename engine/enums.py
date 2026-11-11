"""Shared closed vocabularies (plan §8, §12). Imported by every layer; no logic beyond ordering."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    META = "meta"
    GOOGLE = "google"


class EntityType(StrEnum):
    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    AD_GROUP = "ad_group"
    AD = "ad"
    KEYWORD = "keyword"
    SEARCH_TERM = "search_term"


class Mode(StrEnum):
    """Operating mode (§12.1). Ordered: read_only < suggest < auto_small < auto."""

    READ_ONLY = "read_only"
    SUGGEST = "suggest"
    AUTO_SMALL = "auto_small"
    AUTO = "auto"

    @property
    def rank(self) -> int:
        return _MODE_ORDER.index(self)

    def exceeds(self, other: Mode) -> bool:
        return self.rank > other.rank


_MODE_ORDER: list[Mode] = [Mode.READ_ONLY, Mode.SUGGEST, Mode.AUTO_SMALL, Mode.AUTO]


class Access(StrEnum):
    """Credential access level (§3 credentials provider)."""

    READ = "read"
    WRITE = "write"


class Role(StrEnum):
    """API roles (§17). Ordered: viewer < approver < admin."""

    VIEWER = "viewer"
    APPROVER = "approver"
    ADMIN = "admin"

    def includes(self, required: Role) -> bool:
        order = [Role.VIEWER, Role.APPROVER, Role.ADMIN]
        return order.index(self) >= order.index(required)


class CampaignKind(StrEnum):
    STANDARD = "standard"
    PMAX = "pmax"
    ADVANTAGE_PLUS = "advantage_plus"
    OTHER = "other"


class AutomationScope(StrEnum):
    FULL = "full"
    CAMPAIGN_ONLY = "campaign_only"


class BudgetLevel(StrEnum):
    CAMPAIGN = "campaign"
    AD_SET = "ad_set"
    SHARED = "shared"


class AdGroupKind(StrEnum):
    AD_SET = "ad_set"
    AD_GROUP = "ad_group"


class Action(StrEnum):
    """Proposal actions referenced by the plan (§11.2, §12.2, §12.3, §15). Stored as TEXT."""

    NOOP = "noop"
    ESCALATE = "escalate"
    PAUSE = "pause"
    ENABLE = "enable"
    BUDGET_CHANGE = "budget_change"
    ADD_NEGATIVE = "add_negative"
    REMOVE_NEGATIVE = "remove_negative"  # rollback of add_negative (§15)
    NEW_AD = "new_ad"
    NEW_CAMPAIGN = "new_campaign"
    ROLLBACK_WITH_STATE_DRIFT = "rollback_with_state_drift"


class ProposalSource(StrEnum):
    RULE = "rule"
    LLM_TRIAGE = "llm_triage"
    LLM_ANALYST = "llm_analyst"
    HUMAN = "human"
    ALLOCATION = "allocation"
    EVALUATOR = "evaluator"
    EMERGENCY = "emergency"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXPIRED = "expired"
    FAILED = "failed"
    # Rule outcome ``escalate`` (§11): waits for LLM triage (§13). With the LLM disabled /
    # over budget it stays here and is surfaced to a human (digest + flag_human). Never executed.
    ESCALATED = "escalated"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SpendDirection(StrEnum):
    REDUCING = "reducing"
    INCREASING = "increasing"
    NEUTRAL = "neutral"


class ChangeStatus(StrEnum):
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFY_FAILED = "verify_failed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ChangeOrigin(StrEnum):
    """Why a ``change_log`` row exists (migration 0004, DECISIONS D-27)."""

    PROPOSAL = "proposal"  # an approved proposal applied by the executor (§15)
    EMERGENCY = "emergency"  # pacing emergency stop (§12.4 step 5)
    PAUSE_ALL = "pause_all"  # POST /control/pause-all (§17)
    ROLLBACK = "rollback"  # §15 rollback (evaluator or manual)
    RESTORE = "restore"  # emergency restore (§12.4 step 6)


class WriteRequestKind(StrEnum):
    """API → engine queue for platform writes the API may not perform itself (0004)."""

    ROLLBACK = "rollback"
    RESTORE = "restore"
    PAUSE_ALL = "pause_all"


class HistorySource(StrEnum):
    HOURLY_PULL = "hourly_pull"
    EXECUTOR = "executor"
    EMERGENCY = "emergency"


class EmergencyTrigger(StrEnum):
    DAILY_CAP = "daily_cap"
    MONTHLY_CAP = "monthly_cap"
    PAUSE_ALL = "pause_all"


class RestorePolicy(StrEnum):
    AUTO_NEXT_DAY = "auto_next_day"
    MANUAL = "manual"


class Verdict(StrEnum):
    BETTER = "better"
    NEUTRAL = "neutral"
    WORSE = "worse"
    INSUFFICIENT_DATA = "insufficient_data"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class LLMTier(StrEnum):
    SMALL = "small"
    LARGE = "large"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobRequestStatus(StrEnum):
    """``job_requests.status`` (API → engine manual trigger queue, migration 0003)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"  # not runnable here (unknown job / not allowed) — never ran


class SessionKind(StrEnum):
    BROWSER = "browser"
    CLI = "cli"


class ConfigKind(StrEnum):
    RULES = "rules"
    LIMITS = "limits"


class NotificationKind(StrEnum):
    """Rows in the ``notifications`` outbox (engine/notify)."""

    APPROVAL_REQUEST = "approval_request"
    APPROVAL_REMINDER = "approval_reminder"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_BLOCKED = "approval_blocked"
    DIGEST = "digest"
    ALERT = "alert"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"  # delivered (or logged, for log-only adapters) on >= 1 channel
    FAILED = "failed"
