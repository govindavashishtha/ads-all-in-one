"""Strict output schemas and the action whitelist (plan §13.2, §20, §26).

The model never supplies entity ids, budgets in currency or free-form parameters. It answers
per ``case_id`` with one action from that case's ``allowed_actions``; anything that fails
validation is discarded and the case becomes ``flag_human``. The engine then builds the
proposal itself, and the proposal still passes the guardrail (§12.3).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MAX_REASON_WORDS = 40  # §13.2 "reason": "max 40 words"
MAX_CHANGE_PCT = 20  # §13.2 change_pct integer in [-20, 20]
MAX_CASES_PER_CALL = 20  # §13.3 summariser sends at most 20 entities per call

# Generated text that reaches a human or an ad must not carry links, markup or control chars.
_FORBIDDEN_IN_TEXT = re.compile(r"(https?://|www\.|<\s*/?\s*[a-z]|[\x00-\x08\x0b-\x1f\x7f])", re.I)


class LLMAction(StrEnum):
    """Every action an LLM may ever name. ``escalate_large`` only exists for triage."""

    NOOP = "noop"
    PAUSE = "pause"
    BUDGET_CHANGE = "budget_change"
    ESCALATE_LARGE = "escalate_large"
    FLAG_HUMAN = "flag_human"


TRIAGE_ACTIONS: frozenset[LLMAction] = frozenset(LLMAction)
ANALYST_ACTIONS: frozenset[LLMAction] = TRIAGE_ACTIONS - {LLMAction.ESCALATE_LARGE}


def _clean_text(v: str, *, max_words: int | None = None) -> str:
    v = " ".join(v.split())
    if _FORBIDDEN_IN_TEXT.search(v):
        raise ValueError("links, markup and control characters are not allowed")
    if max_words is not None and len(v.split()) > max_words:
        raise ValueError(f"more than {max_words} words")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


CaseId = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,4}_[0-9]{1,12}$")]


class CaseDecision(_Strict):
    """One answer per case (§13.2 output)."""

    case_id: CaseId
    action: LLMAction
    change_pct: int | None = Field(default=None, ge=-MAX_CHANGE_PCT, le=MAX_CHANGE_PCT)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=400)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        return _clean_text(v, max_words=MAX_REASON_WORDS)


class TriageOutput(_Strict):
    results: list[CaseDecision] = Field(max_length=MAX_CASES_PER_CALL)


class AnalystDecision(CaseDecision):
    root_cause: str = Field(min_length=1, max_length=600)

    @field_validator("root_cause")
    @classmethod
    def _root(cls, v: str) -> str:
        return _clean_text(v, max_words=80)


class AnalystOutput(_Strict):
    results: list[AnalystDecision] = Field(max_length=MAX_CASES_PER_CALL)


# --------------------------------------------------------------------------- creative

# VERIFY-P0 (Phase 5): Google RSA headline ≤ 30 chars, description ≤ 90 chars. Meta has no
# hard limit for these fields; the same conservative limits are used for both in v1.
HEADLINE_MAX_CHARS = 30
BODY_MAX_CHARS = 90
MAX_VARIANTS = 3


class CopyVariant(_Strict):
    headline: str = Field(min_length=1, max_length=HEADLINE_MAX_CHARS)
    body: str = Field(min_length=1, max_length=BODY_MAX_CHARS)

    @field_validator("headline", "body")
    @classmethod
    def _text(cls, v: str) -> str:
        return _clean_text(v)


class CreativeDraft(_Strict):
    case_id: CaseId
    variants: list[CopyVariant] = Field(min_length=1, max_length=MAX_VARIANTS)
    rationale: str = Field(min_length=1, max_length=400)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, v: str) -> str:
        return _clean_text(v, max_words=MAX_REASON_WORDS)


class CreativeOutput(_Strict):
    drafts: list[CreativeDraft] = Field(max_length=MAX_CASES_PER_CALL)


Purpose = Literal["triage", "analyst", "creative", "digest", "search_terms"]
