"""Creative drafts for the weekly refresh (plan §9 weekly_strategy, §13.1, §13.2, §20).

Fatigued ads (rule ``creative_fatigue``, reason ``fatigue``) get 1–3 new copy variants from the
large model. The current copy is untrusted input and only appears under ``untrusted_text``.
Output is schema-validated (length limits, no links/markup) and only ever stored as ONE
``new_ad`` proposal per ad group with ``risk = high`` — so it needs a human in every mode,
whatever ``approval_required_for`` says — and ``create_status = PAUSED`` (§13.2, §15).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from engine.db.models import Ad, Proposal
from engine.enums import Action, AutomationScope, EntityType, LLMTier, ProposalSource, Risk
from engine.features.data import EntityInfo
from engine.llm.client import LLMMessage, LLMRequest
from engine.llm.schemas import (
    BODY_MAX_CHARS,
    HEADLINE_MAX_CHARS,
    MAX_VARIANTS,
    CreativeDraft,
)
from engine.llm.summariser import _compact, _num
from engine.llm.triage import _extract_json, system_prompt
from engine.llm.untrusted import untrusted_block
from engine.proposals import ProposalDraft

OUT_TOKENS_PER_CASE = 220
LIMITS = {
    "headline_max_chars": HEADLINE_MAX_CHARS,
    "body_max_chars": BODY_MAX_CHARS,
    "max_variants": MAX_VARIANTS,
}


@dataclass
class CreativeCase:
    case_id: str
    ad: EntityInfo
    escalations: list[Proposal]
    payload: dict[str, Any]


def skip_reason(e: EntityInfo | None) -> str | None:
    if e is None:
        return "ad not found"
    if not e.effective_active:
        return "ad or a parent is not active"
    if e.automation_scope == AutomationScope.CAMPAIGN_ONLY.value:
        return "campaign_only scope: no ad-level actions (§8.2)"
    if not e.campaign_managed:
        return "campaign not managed"
    if e.ad_group_id is None:
        return "ad has no ad group"
    return None


def build_case(
    e: EntityInfo, ad_row: Ad | None, f: Mapping[str, Any], escalations: list[Proposal]
) -> CreativeCase:
    payload = _compact(
        {
            "id": f"ad_{e.id}",
            "platform": e.platform.value,
            "format": ad_row.format if ad_row else None,
            "metrics": {
                "ctr_7d": _num(f.get("ctr_7d"), "ratio"),
                "ctr_28d": _num(f.get("ctr_28d"), "ratio"),
                "freq_7d": _num(f.get("frequency_7d"), "p"),
                "days_live": _num(f.get("days_live"), "count"),
            },
            "untrusted_text": untrusted_block(
                ad_name=e.name,
                headline=ad_row.headline if ad_row else None,
                body=ad_row.body if ad_row else None,
                cta=ad_row.cta if ad_row else None,
            ),
        }
    )
    return CreativeCase(f"ad_{e.id}", e, escalations, payload)


def build_request(client: Mapping[str, Any], cases: list[CreativeCase]) -> LLMRequest:
    body = {"client": dict(client), "limits": LIMITS, "cases": [c.payload for c in cases]}
    return LLMRequest(
        tier=LLMTier.LARGE,
        purpose="creative",
        system=system_prompt("creative"),
        messages=(LLMMessage("user", json.dumps(body, separators=(",", ":"), ensure_ascii=False)),),
        max_output_tokens=64 + OUT_TOKENS_PER_CASE * len(cases),
        temperature=0.0,
        json_output=True,
    )


def parse_drafts(
    text: str | None, cases: list[CreativeCase]
) -> tuple[dict[str, CreativeDraft], list[str]]:
    by_id = {c.case_id for c in cases}
    if text is None:
        return {}, ["no output"]
    try:
        doc = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return {}, ["not json"]
    if not isinstance(doc, dict) or set(doc) != {"drafts"} or not isinstance(doc["drafts"], list):
        return {}, ["schema"]
    out: dict[str, CreativeDraft] = {}
    problems: list[str] = []
    dupes: set[str] = set()
    for item in doc["drafts"][: len(cases) * 2]:
        try:
            d = CreativeDraft.model_validate_json(json.dumps(item))  # strict JSON mode
        except ValidationError as exc:
            problems.append(f"draft invalid: {exc.errors()[0].get('msg', '?')}")
            if isinstance(item, dict) and item.get("case_id") in by_id:
                dupes.add(item["case_id"])  # a broken draft disqualifies its case
            continue
        if d.case_id not in by_id:
            problems.append(f"unknown case_id {d.case_id!r}")
        elif d.case_id in out:
            dupes.add(d.case_id)
        else:
            out[d.case_id] = d
    for cid in dupes:
        out.pop(cid, None)
        problems.append(f"{cid}: invalid or duplicate draft")
    return out, problems


def proposals_by_group(
    cases: list[CreativeCase], drafts: Mapping[str, CreativeDraft], llm_call_id: int | None
) -> list[ProposalDraft]:
    groups: dict[int, list[tuple[CreativeCase, CreativeDraft]]] = {}
    for c in cases:
        if c.case_id in drafts and c.ad.ad_group_id is not None:
            groups.setdefault(c.ad.ad_group_id, []).append((c, drafts[c.case_id]))
    out: list[ProposalDraft] = []
    for group_id, items in sorted(groups.items()):
        as_of = max(p.as_of for c, _ in items for p in c.escalations)
        out.append(
            ProposalDraft(
                as_of=as_of,
                source=ProposalSource.LLM_ANALYST,
                rule_key=None,
                entity_type=EntityType.AD_GROUP,
                entity_id=group_id,
                action=Action.NEW_AD,
                risk=Risk.HIGH,  # always a human, in every mode (§12.1, §13.2)
                reason=(
                    "LLM creative refresh for fatigued ad(s) "
                    + ", ".join(f"#{c.ad.id}" for c, _ in items)
                    + ": "
                    + " / ".join(d.rationale for _, d in items)
                    + " Generated copy: review before approving; created PAUSED."
                ),
                params={
                    "create_status": "PAUSED",
                    "generated_by": "llm",
                    "llm_call_id": llm_call_id,
                    "drafts": [
                        {
                            "replaces_ad_id": c.ad.id,
                            "escalation_ids": sorted(int(p.id) for p in c.escalations),
                            "variants": [v.model_dump() for v in d.variants],
                        }
                        for c, d in items
                    ],
                },
            )
        )
    return out
