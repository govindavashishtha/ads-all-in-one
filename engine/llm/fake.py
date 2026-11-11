"""Deterministic FakeLLM (tests, golden set, local demo with ``LLM_PROVIDER=fake``).

It is a *stub*, not intelligence: fixed heuristics over the compact case JSON, so the golden
set and the pipeline can be exercised without a provider, a key or any cost. It never reads
``untrusted_text``. A ``script`` (callable request -> raw text) replaces the heuristics, which
is how tests feed schema violations and injected outputs through the real validation path.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

from engine.config import Settings
from engine.enums import LLMTier
from engine.llm.client import LLMClient, LLMRequest, LLMResponse

Script = Callable[[LLMRequest], str]


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class FakeLLM(LLMClient):
    def __init__(self, settings: Settings, script: Script | None = None) -> None:
        super().__init__(settings)
        self.script = script
        self.requests: list[LLMRequest] = []

    def model_for(self, tier: LLMTier) -> str:
        return f"fake-{tier.value}"  # not a real model name; marks ledger rows as fake

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.script is not None:
            text = self.script(request)
        else:
            payload = json.loads(request.messages[-1].content)
            handler = {
                "triage": _triage,
                "analyst": _analyst,
                "creative": _creative,
            }.get(request.purpose)
            text = json.dumps(handler(payload) if handler else {})
        prompt = request.system + "".join(m.content for m in request.messages)
        return LLMResponse(
            text=text,
            model=self.model_for(request.tier),
            input_tokens=_tokens(prompt),
            cached_input_tokens=0,
            output_tokens=_tokens(text),
            latency_ms=0,
            stop_reason="end_turn",
        )


def _decision(
    case: dict[str, Any], action: str, conf: float, why: str, pct: int | None = None
) -> dict[str, Any]:
    allowed = case.get("allowed_actions", [])
    if action not in allowed:
        action, pct, conf, why = "flag_human", None, 0.5, f"{why}; preferred action not allowed"
    out: dict[str, Any] = {"case_id": case["id"], "action": action, "confidence": conf}
    if action == "budget_change":
        out["change_pct"] = pct
    out["reason"] = f"FakeLLM stub: {why}"
    return out


def _triage(payload: dict[str, Any]) -> dict[str, Any]:
    results = []
    for case in payload.get("cases", []):
        reason = str(case.get("reason", ""))
        ent = case.get("entity", {})
        d1 = case.get("metrics", {}).get("d1", {})
        if ent.get("learning"):
            results.append(_decision(case, "noop", 0.6, "entity is learning; wait"))
        elif reason.startswith("cpa_spike"):
            if not d1.get("conv"):
                results.append(_decision(case, "escalate_large", 0.6, "spend with zero conv"))
            else:
                results.append(_decision(case, "budget_change", 0.7, "cpa spike", -15))
        elif reason.startswith("ctr_collapse"):
            results.append(_decision(case, "pause", 0.6, "ctr collapsed versus 28d"))
        else:
            results.append(_decision(case, "flag_human", 0.5, "unknown escalation"))
    return {"results": results}


def _analyst(payload: dict[str, Any]) -> dict[str, Any]:
    results = []
    for case in payload.get("cases", []):
        d = _decision(case, "budget_change", 0.65, "sustained spend without conversions", -10)
        if d["action"] == "flag_human" and "pause" in case.get("allowed_actions", []):
            d = _decision(case, "pause", 0.6, "sustained spend without conversions")
        d["root_cause"] = "FakeLLM stub root cause: conversions stopped while spend continued."
        results.append(d)
    return {"results": results}


def _creative(payload: dict[str, Any]) -> dict[str, Any]:
    drafts = []
    for case in payload.get("cases", []):
        drafts.append(
            {
                "case_id": case["id"],
                "variants": [
                    {"headline": "Draft headline A", "body": "FakeLLM placeholder copy A."},
                    {"headline": "Draft headline B", "body": "FakeLLM placeholder copy B."},
                ],
                "rationale": "FakeLLM stub: refresh fatigued creative with new angles.",
            }
        )
    return {"drafts": drafts}
