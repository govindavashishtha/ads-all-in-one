"""Provider-agnostic LLM client interface (plan §5, §13). No provider is implemented here.

Model names and prices come ONLY from env (``LLM_*``, INPUT I-8) — never hard-code them.
Every call must pass the token budget check (engine/llm/budget.py) first and be recorded in
``llm_calls``. Output is only ever a proposal (validated with Pydantic, §13.2).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from engine.config import Settings
from engine.enums import LLMTier
from engine.errors import MissingInputError


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    tier: LLMTier
    purpose: str  # triage | analyst | creative | digest | search_terms
    system: str  # static system prompt + playbook first (cacheable, §13.3)
    messages: tuple[LLMMessage, ...]
    max_output_tokens: int
    temperature: float = 0.0
    json_output: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str | None = None


class LLMClient(abc.ABC):
    """Implement one subclass per provider (LLM agent, Phase 4)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def model_for(self, tier: LLMTier) -> str:
        model = (
            self.settings.llm_small_model
            if tier is LLMTier.SMALL
            else self.settings.llm_large_model
        )
        if not model:
            raise MissingInputError("I-8", f"LLM_{tier.value.upper()}_MODEL is not set")
        return model

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one request. Raise on transport errors; callers degrade to `flag_human`."""
