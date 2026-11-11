"""LLM status for the AI usage page / ``/llm/usage`` (plan §18): enabled flag, provider kind,
why it is off, and the configured caps. No secrets, no model keys."""

from __future__ import annotations

from typing import Any

from engine.config import Settings
from engine.llm.providers import build_client


def llm_status(settings: Settings) -> dict[str, Any]:
    choice = build_client(settings)
    return {
        "llm_enabled": choice.enabled,
        "provider": choice.reason if choice.enabled else None,
        "disabled_reason": None if choice.enabled else choice.reason,
        "small_model": settings.llm_small_model if choice.enabled else None,
        "large_model": settings.llm_large_model if choice.enabled else None,
        "daily_token_cap": settings.llm_daily_token_cap,
        "daily_cost_cap_usd": settings.llm_daily_cost_cap_usd,
        "monthly_cost_cap_usd": settings.llm_monthly_cost_cap_usd,
    }
