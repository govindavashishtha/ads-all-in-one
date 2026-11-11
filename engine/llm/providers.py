"""Provider adapters behind ``LLMClient`` (plan §5, §13) and the client factory.

``LLM_PROVIDER`` (INPUT I-8) selects the adapter; model names, key and prices come only from
the ``LLM_*`` env values via ``Settings`` — nothing is defaulted here:

    (empty) | disabled | none      LLM off: no calls, escalations become flag_human (§13.1)
    anthropic[:<base_url>]         Anthropic Messages API (default base https://api.anthropic.com)
    openai[:<base_url>]            OpenAI-compatible Chat Completions (default
                                   https://api.openai.com/v1; any compatible gateway via base_url)
    fake                           deterministic FakeLLM, APP_ENV=development only

The API key is unwrapped only inside the adapter that sends it (DECISIONS D-20); it is never
logged. Requests carry no PII, account ids or tokens (§20) — callers send compact JSON only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from engine.config import Settings
from engine.enums import LLMTier
from engine.errors import MissingInputError
from engine.llm.client import LLMClient, LLMRequest, LLMResponse
from engine.llm.prices import require_tier_price

log = structlog.get_logger(__name__)

ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"  # VERIFY at I-8: API version header
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
TIMEOUT_S = 60.0
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MAX_ATTEMPTS = 2

DISABLED_VALUES = frozenset({"", "disabled", "none", "off", "false"})


class LLMProviderError(RuntimeError):
    """Transport / provider failure. Callers degrade to ``flag_human``."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    kind: str  # anthropic | openai | fake | disabled
    base_url: str | None


def parse_provider(value: str | None) -> ProviderSpec:
    raw = (value or "").strip()
    if raw.lower() in DISABLED_VALUES:
        return ProviderSpec("disabled", None)
    kind, _, base = raw.partition(":")
    kind = kind.strip().lower()
    base = base.strip()
    if kind not in {"anthropic", "openai", "fake"}:
        raise ValueError(f"unknown LLM_PROVIDER {raw!r} (anthropic | openai | fake | disabled)")
    if base and not base.startswith("https://") and not base.startswith("http://localhost"):
        raise ValueError("LLM_PROVIDER base URL must be https:// (or http://localhost)")
    return ProviderSpec(kind, base.rstrip("/") or None)


class _HTTPClient(LLMClient):
    default_base: str

    def __init__(
        self,
        settings: Settings,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        super().__init__(settings)
        if settings.llm_api_key is None:
            raise MissingInputError("I-8", "LLM_API_KEY is not set")
        for tier in LLMTier:  # fail fast: models and prices are INPUT I-8
            self.model_for(tier)
            require_tier_price(settings, tier)
        self.base_url = base_url or self.default_base
        self._transport = transport
        self._sleep = sleep

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=TIMEOUT_S, transport=self._transport) as http:
                    resp = http.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if resp.status_code < 400:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise LLMProviderError("provider returned a non-object JSON body")
                    return data
                last = LLMProviderError(f"provider HTTP {resp.status_code}")
                if resp.status_code not in RETRY_STATUSES:
                    break
            if attempt + 1 < MAX_ATTEMPTS:
                self._sleep(2.0 * (attempt + 1))
        raise LLMProviderError(str(last)) from last

    def _key(self) -> str:
        assert self.settings.llm_api_key is not None
        return self.settings.llm_api_key.get_secret_value()  # D-20: adapter-only unwrap


class AnthropicClient(_HTTPClient):
    """Anthropic Messages API. The static system prompt is marked cacheable (§13.3)."""

    default_base = ANTHROPIC_DEFAULT_BASE

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.model_for(request.tier)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            # prompt-caching hook: system prompt + playbook first, per-case JSON last
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        headers = {
            "x-api-key": self._key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        t0 = time.monotonic()
        data = self._post(f"{self.base_url}/v1/messages", headers, body)
        latency = int((time.monotonic() - t0) * 1000)
        text = "".join(
            b.get("text", "")
            for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        u = data.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_write = int(u.get("cache_creation_input_tokens") or 0)
        return LLMResponse(
            text=text,
            model=str(data.get("model") or model),
            input_tokens=int(u.get("input_tokens") or 0) + cache_read + cache_write,
            cached_input_tokens=cache_read,
            output_tokens=int(u.get("output_tokens") or 0),
            latency_ms=latency,
            stop_reason=data.get("stop_reason"),
        )


class OpenAICompatibleClient(_HTTPClient):
    """OpenAI-compatible Chat Completions. Prefix caching is automatic on the provider side;
    ``cached_tokens`` is read back from usage when reported."""

    default_base = OPENAI_DEFAULT_BASE

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.model_for(request.tier)
        body: dict[str, Any] = {
            "model": model,
            "temperature": request.temperature,  # VERIFY at I-8: some models reject it
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": m.role, "content": m.content} for m in request.messages),
            ],
        }
        # official endpoint: max_completion_tokens; generic gateways commonly accept max_tokens
        limit_key = (
            "max_completion_tokens" if self.base_url == OPENAI_DEFAULT_BASE else "max_tokens"
        )
        body[limit_key] = request.max_output_tokens
        if request.json_output:
            body["response_format"] = {"type": "json_object"}
        headers = {"authorization": f"Bearer {self._key()}", "content-type": "application/json"}
        t0 = time.monotonic()
        data = self._post(f"{self.base_url}/chat/completions", headers, body)
        latency = int((time.monotonic() - t0) * 1000)
        choices = data.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        return LLMResponse(
            text=str(msg.get("content") or ""),
            model=str(data.get("model") or model),
            input_tokens=int(u.get("prompt_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            latency_ms=latency,
            stop_reason=choices[0].get("finish_reason"),
        )


@dataclass(frozen=True, slots=True)
class ClientChoice:
    client: LLMClient | None
    reason: str  # why disabled, or the provider kind

    @property
    def enabled(self) -> bool:
        return self.client is not None


def build_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> ClientChoice:
    """Never raises: a missing/invalid I-8 input disables the LLM (pipelines still complete)."""
    try:
        spec = parse_provider(settings.llm_provider)
        if spec.kind == "disabled":
            return ClientChoice(None, "LLM_PROVIDER not set (LLM disabled)")
        if spec.kind == "fake":
            if settings.app_env != "development":
                return ClientChoice(None, "LLM_PROVIDER=fake is only allowed in development")
            from engine.llm.fake import FakeLLM

            return ClientChoice(FakeLLM(settings), "fake")
        cls = AnthropicClient if spec.kind == "anthropic" else OpenAICompatibleClient
        return ClientChoice(cls(settings, spec.base_url, transport=transport), spec.kind)
    except (MissingInputError, ValueError) as exc:
        log.warning("llm.disabled", reason=str(exc))
        return ClientChoice(None, str(exc))
