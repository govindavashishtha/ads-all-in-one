"""LLM golden set (plan §22 'LLM', §23 Phase 4): fixed escalations → expected action class,
plus schema-violation and prompt-injection cases. Runs the real request builder and the real
parser; the model is the deterministic FakeLLM (heuristics or a scripted raw output)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from engine.config import Settings
from engine.enums import ProposalSource
from engine.llm.fake import FakeLLM
from engine.llm.schemas import LLMAction
from engine.llm.summariser import Case
from engine.llm.triage import build_request, parse_verdicts, proposal_from
from tests.unit.llm_helpers import entity, make_case

GOLDEN = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "golden_triage.json").read_text()
)
CLIENT = GOLDEN["client"]


def _case(i: int, item: dict[str, Any]) -> Case:
    spec = dict(GOLDEN["entities"][item["entity"]])
    et = spec.pop("entity_type")
    e = entity(et, id=100 + i, name=item.get("untrusted_name", "Golden entity"), **spec)
    return make_case(1000 + i, e, item.get("rule", "cpa_spike_anomaly"), item.get("metrics"))


def test_golden_fake_batch(make_settings: Callable[..., Settings]) -> None:
    """All 'fake' cases in ONE batched request (§13.3) → expected action classes."""
    items = GOLDEN["fake"]
    cases = [_case(i, it) for i, it in enumerate(items)]
    llm = FakeLLM(make_settings())
    req = build_request("triage", CLIENT, cases)
    resp = llm.complete(req)
    verdicts, problems = parse_verdicts(resp.text, cases, analyst=False)
    assert problems == []
    for case, it in zip(cases, items, strict=True):
        v = verdicts[case.case_id]
        assert v.action.value == it["expected"]["action"], it["name"]
        if "change_pct" in it["expected"]:
            assert v.decision is not None and v.decision.change_pct == it["expected"]["change_pct"]


@pytest.mark.parametrize("item", GOLDEN["scripted"], ids=lambda it: it["name"])
def test_golden_scripted(item: dict[str, Any], make_settings: Callable[..., Settings]) -> None:
    case = _case(0, item)
    raw = item["output"].replace("{id}", case.case_id)
    llm = FakeLLM(make_settings(), script=lambda _r: raw)
    resp = llm.complete(build_request("triage", CLIENT, [case]))
    verdicts, problems = parse_verdicts(resp.text, [case], analyst=False)
    v = verdicts[case.case_id]
    exp = item["expected"]
    assert v.action.value == exp["action"], (item["name"], v)
    if "error" in exp:
        assert v.error is not None and exp["error"].lower() in v.error.lower(), v.error
        assert problems, "an invalid output must be reported (ledger row → invalid_output)"
        draft = proposal_from(case, v, source=ProposalSource.LLM_TRIAGE, llm_call_id=1)
        assert draft is None, "an invalid output must never become a proposal"
    if "change_pct" in exp:
        assert v.decision is not None and v.decision.change_pct == exp["change_pct"]


def test_injection_text_is_isolated_in_the_request(make_settings: Callable[..., Settings]) -> None:
    item = next(i for i in GOLDEN["scripted"] if i["name"] == "injection_asks_for_enable")
    case = _case(0, item)
    req = build_request("triage", CLIENT, [case])
    assert "untrusted_text" in req.system  # the system prompt explains the isolation
    assert item["untrusted_name"][:20] not in req.system
    body = json.loads(req.messages[-1].content)
    (c,) = body["cases"]
    # the hostile name is present only as a JSON string value under untrusted_text, flagged
    assert c["untrusted_text"]["entity_name"].startswith("</system>")
    assert c["untrusted_text"]["_flag"]
    blob = json.dumps({k: v for k, v in c.items() if k != "untrusted_text"})
    assert "admin" not in blob and "Enable" not in blob
    # the FakeLLM heuristics ignore untrusted text entirely
    out = json.loads(FakeLLM(make_settings()).complete(req).text)
    assert out["results"][0]["action"] in case.allowed


def test_proposal_is_built_by_the_engine_not_the_model(
    make_settings: Callable[..., Settings],
) -> None:
    e = entity("ad_group", id=55)
    case = make_case(9, e, metrics={"spend_1d": 4000, "conversions_1d": 2})
    raw = json.dumps(
        {
            "results": [
                {
                    "case_id": case.case_id,
                    "action": "budget_change",
                    "change_pct": -12,
                    "confidence": 0.8,
                    "reason": "trim",
                }
            ]
        }
    )
    verdicts, problems = parse_verdicts(raw, [case], analyst=False)
    assert not problems
    d = proposal_from(case, verdicts[case.case_id], source=ProposalSource.LLM_TRIAGE, llm_call_id=3)
    assert d is not None
    assert (d.entity_type.value, d.entity_id, d.as_of) == ("ad_group", 55, case.escalation.as_of)
    assert d.params == {"escalation_id": 9, "llm_call_id": 3, "change_pct": -12}
    assert d.risk.value == "medium" and d.status.value == "pending"
    assert d.expected_impact == {
        "daily_budget_before": "1000",
        "change_pct": "-12",
        "daily_budget_delta_est": "-120.00",
    }


def test_analyst_schema_requires_root_cause(make_settings: Callable[..., Settings]) -> None:
    e = entity("ad_group", id=56)
    case = make_case(10, e, triage=False)
    assert "escalate_large" not in case.allowed
    ok = FakeLLM(make_settings()).complete(build_request("analyst", CLIENT, [case]))
    v, problems = parse_verdicts(ok.text, [case], analyst=True)
    assert not problems and v[case.case_id].action is LLMAction.BUDGET_CHANGE
    raw = json.dumps(
        {
            "results": [
                {
                    "case_id": case.case_id,
                    "action": "escalate_large",
                    "confidence": 0.9,
                    "reason": "deeper",
                    "root_cause": "x",
                }
            ]
        }
    )
    v, _ = parse_verdicts(raw, [case], analyst=True)
    assert v[case.case_id].action is LLMAction.FLAG_HUMAN
    missing_root = raw.replace(', "root_cause": "x"', "").replace("escalate_large", "noop")
    v, _ = parse_verdicts(missing_root, [case], analyst=True)
    assert v[case.case_id].action is LLMAction.FLAG_HUMAN
