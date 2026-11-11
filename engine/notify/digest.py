"""Daily digest (plan §9.1 step 11): yesterday's spend vs cap, CPA/ROAS vs target, top 3
winners/losers, changes made, rollbacks, pending approvals, LLM cost — plus what the rules
proposed and why the guardrail blocked. Rendered as text + HTML and stored in the
``notifications`` outbox (``kind='digest'``, one per ``as_of``); delivery via engine/notify.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import (
    AdAccount,
    ChangeLog,
    Client,
    LLMCall,
    MetricsDaily,
    Proposal,
    Score,
)
from engine.enums import EntityType, NotificationKind, Platform, ProposalStatus
from engine.features.data import load_universe
from engine.features.windows import trailing
from engine.jobs.common import active_limits, active_rules
from engine.notify.outbox import enqueue

CENT = Decimal("0.01")


@dataclass
class Digest:
    as_of: dt.date
    data: dict[str, Any]
    subject: str
    text: str
    html: str


def _q(x: Decimal | None) -> str:
    return "-" if x is None else str(Decimal(x).quantize(CENT))


def _platform_totals(
    session: Session, cid: str, start: dt.date, end: dt.date
) -> dict[str, dict[str, Decimal]]:
    rows = session.execute(
        select(
            AdAccount.platform,
            func.coalesce(func.sum(MetricsDaily.spend), 0),
            func.coalesce(func.sum(MetricsDaily.conversions), 0),
            func.coalesce(func.sum(MetricsDaily.conv_value), 0),
        )
        .join(AdAccount, AdAccount.id == MetricsDaily.entity_id)
        .where(
            MetricsDaily.client_id == cid,
            MetricsDaily.entity_type == EntityType.ACCOUNT,
            MetricsDaily.date >= start,
            MetricsDaily.date <= end,
        )
        .group_by(AdAccount.platform)
    )
    out = {
        p.value: {"spend": Decimal(0), "conv": Decimal(0), "value": Decimal(0)} for p in Platform
    }
    for pl, s, c, v in rows:
        out[Platform(pl).value] = {"spend": Decimal(s), "conv": Decimal(c), "value": Decimal(v)}
    tot = {k: sum((out[p][k] for p in out), Decimal(0)) for k in ("spend", "conv", "value")}
    out["total"] = tot
    return out


def _cpa(t: dict[str, Decimal]) -> Decimal | None:
    return (t["spend"] / t["conv"]).quantize(CENT) if t["conv"] > 0 else None


def _roas(t: dict[str, Decimal]) -> float | None:
    return round(float(t["value"] / t["spend"]), 2) if t["spend"] > 0 else None


def build_digest(session: Session, settings: Settings, as_of: dt.date) -> Digest:
    cid = settings.client_id
    client = session.get(Client, cid)
    rules = active_rules(session, cid)
    limits = active_limits(session, cid)
    tz = ZoneInfo(settings.timezone)
    day_start = dt.datetime.combine(as_of, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    day_end = day_start + dt.timedelta(days=1)
    y = _platform_totals(session, cid, as_of, as_of)
    w7 = trailing(as_of, 7)
    d7 = _platform_totals(session, cid, w7.start, w7.end)
    mtd = _platform_totals(session, cid, as_of.replace(day=1), as_of)
    caps = {"meta": limits.caps.meta, "google": limits.caps.google, "total": limits.caps.total}
    spend_rows = [
        {
            "scope": k,
            "spend": _q(y[k]["spend"]),
            "daily_cap": _q(caps[k].daily),
            "pct_of_cap": round(float(y[k]["spend"] / caps[k].daily) * 100, 1),
            "mtd": _q(mtd[k]["spend"]),
            "monthly_cap": _q(caps[k].monthly),
            "cpa_1d": _q(_cpa(y[k])),
            "cpa_7d": _q(_cpa(d7[k])),
            "roas_7d": _roas(d7[k]),
        }
        for k in ("meta", "google", "total")
    ]

    universe = load_universe(session, cid)
    scores = session.execute(
        select(
            Score.entity_type,
            Score.entity_id,
            Score.p_beats_target,
            Score.cpa_shrunk,
            Score.rank_in_peer,
            Score.peer_size,
        ).where(
            Score.client_id == cid,
            Score.as_of == as_of,
            Score.data_sufficient.is_(True),
            Score.p_beats_target.is_not(None),
        )
    ).all()

    def ent(r: Any) -> dict[str, Any]:
        e = universe.entities.get((EntityType(r[0]), int(r[1])))
        return {
            "entity": f"{EntityType(r[0]).value} {r[1]}",
            "name": e.name if e else None,
            "platform": e.platform.value if e else None,
            "p_beats_target": round(float(r[2] or 0), 3),
            "cpa_shrunk": _q(r[3]),
            "rank": f"{r[4]}/{r[5]}",
        }

    ordered = sorted(scores, key=lambda r: (-float(r[2] or 0), r[0], r[1]))
    winners = [ent(r) for r in ordered[:3]]
    losers = [ent(r) for r in sorted(scores, key=lambda r: (float(r[2] or 0), r[0], r[1]))[:3]]

    changes = session.execute(
        select(
            ChangeLog.id,
            ChangeLog.action,
            ChangeLog.entity_type,
            ChangeLog.entity_id,
            ChangeLog.status,
            ChangeLog.rollback_of,
        )
        .where(
            ChangeLog.client_id == cid,
            ChangeLog.applied_at >= day_start,
            ChangeLog.applied_at < day_end,
        )
        .order_by(ChangeLog.id)
    ).all()
    made = [
        f"#{c[0]} {c[1]} {EntityType(c[2]).value} {c[3]} ({c[4]})" for c in changes if c[5] is None
    ]
    rollbacks = [f"#{c[0]} rollback of #{c[5]}" for c in changes if c[5] is not None]

    props = session.scalars(
        select(Proposal)
        .where(Proposal.client_id == cid, Proposal.as_of == as_of)
        .order_by(Proposal.id)
    ).all()
    by_status: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for p in props:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        by_rule[p.rule_key or p.source] = by_rule.get(p.rule_key or p.source, 0) + 1
        if p.status == ProposalStatus.BLOCKED.value:
            reason = (p.guardrail_result or {}).get("reason", "?")
            blocked[reason] = blocked.get(reason, 0) + 1
    awaiting = session.scalars(
        select(Proposal)
        .where(Proposal.client_id == cid, Proposal.status == ProposalStatus.AWAITING_APPROVAL.value)
        .order_by(Proposal.expires_at, Proposal.id)
        .limit(10)
    ).all()
    escalated = [p for p in props if p.status == ProposalStatus.ESCALATED.value]
    llm_cost = session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            LLMCall.client_id == cid, LLMCall.created_at >= day_start, LLMCall.created_at < day_end
        )
    )
    data: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "currency": rules.targets.currency,
        "mode": client.mode if client else None,
        "kill_switch": bool(client.kill_switch) if client else None,
        "objective": rules.targets.objective,
        "target_cpa": str(rules.targets.target_cpa),
        "target_roas": str(rules.targets.target_roas) if rules.targets.target_roas else None,
        "inputs_confirmed": rules.inputs_confirmed and limits.inputs_confirmed,
        "spend": spend_rows,
        "winners": winners,
        "losers": losers,
        "changes": made,
        "rollbacks": rollbacks,
        "proposals_by_status": dict(sorted(by_status.items())),
        "proposals_by_rule": dict(sorted(by_rule.items())),
        "blocked_by_reason": dict(sorted(blocked.items())),
        "awaiting_approval": [
            {
                "id": p.id,
                "action": p.action,
                "entity": f"{p.entity_type.value} {p.entity_id}",
                "expires_at": p.expires_at.astimezone(ZoneInfo(settings.timezone)).strftime(
                    "%Y-%m-%d %H:%M %Z"
                )
                if p.expires_at
                else None,
                "reason": p.reason,
            }
            for p in awaiting
        ],
        "escalated": [
            {
                "id": p.id,
                "rule": p.rule_key,
                "entity": f"{p.entity_type.value} {p.entity_id}",
                "reason": p.reason,
            }
            for p in escalated
        ],
        "llm_cost_usd": str(Decimal(llm_cost or 0).quantize(Decimal("0.00001"))),
    }
    subject = (
        f"[ads-autopilot] Daily digest {as_of.isoformat()} — spend "
        f"{spend_rows[2]['spend']} {data['currency']} ({spend_rows[2]['pct_of_cap']}% of cap), "
        f"{len(props)} proposals, {len(awaiting)} awaiting approval"
    )
    return Digest(as_of, data, subject, render_text(data), render_html(data, subject))


def render_text(d: dict[str, Any]) -> str:
    cur = d["currency"]
    out = [
        f"ads-autopilot daily digest — {d['as_of']} (account day)",
        f"Mode: {d['mode']} · kill switch: {'ON' if d['kill_switch'] else 'off'} · "
        f"objective: {d['objective']} · target CPA {d['target_cpa']} {cur}",
    ]
    if not d["inputs_confirmed"]:
        out.append("NOTE: targets/caps are still placeholders (inputs I-1/I-2 not confirmed).")
    out.append("")
    out.append("Spend vs cap (yesterday) · MTD · CPA 1d / 7d · ROAS 7d")
    for r in d["spend"]:
        out.append(
            f"  {r['scope']:<7} {r['spend']} / {r['daily_cap']} {cur} ({r['pct_of_cap']}%) · "
            f"MTD {r['mtd']} / {r['monthly_cap']} · CPA {r['cpa_1d']} / {r['cpa_7d']} · "
            f"ROAS {r['roas_7d'] if r['roas_7d'] is not None else '-'}"
        )

    def ents(title: str, rows: list[dict[str, Any]]) -> None:
        out.append("")
        out.append(title)
        if not rows:
            out.append("  (no entity has enough data yet)")
        for e in rows:
            out.append(
                f"  {e['entity']} {e['name'] or ''} [{e['platform']}] "
                f"P(beats target)={e['p_beats_target']} CPA~{e['cpa_shrunk']} "
                f"rank {e['rank']}"
            )

    ents("Top 3 winners", d["winners"])
    ents("Top 3 losers", d["losers"])
    out.append("")
    out.append(f"Changes made: {', '.join(d['changes']) or 'none'}")
    out.append(f"Rollbacks: {', '.join(d['rollbacks']) or 'none'}")
    out.append(f"Proposals today by status: {d['proposals_by_status'] or 'none'}")
    out.append(f"Proposals today by rule: {d['proposals_by_rule'] or 'none'}")
    out.append(f"Guardrail blocks by reason: {d['blocked_by_reason'] or 'none'}")
    out.append("")
    out.append(f"Awaiting approval ({len(d['awaiting_approval'])} shown):")
    for a in d["awaiting_approval"]:
        out.append(f"  #{a['id']} {a['action']} {a['entity']} (expires {a['expires_at']})")
    if d["escalated"]:
        out.append("")
        out.append(f"Escalations for human review (LLM triage not enabled): {len(d['escalated'])}")
        for x in d["escalated"]:
            out.append(f"  #{x['id']} {x['rule']} {x['entity']}")
    out.append("")
    out.append(f"LLM cost yesterday: ${d['llm_cost_usd']}")
    return "\n".join(out) + "\n"


def render_html(d: dict[str, Any], subject: str) -> str:
    cur = escape(d["currency"])

    def table(head: list[str], rows: list[list[Any]]) -> str:
        th = "".join(f"<th>{escape(h)}</th>" for h in head)
        trs = "".join(
            "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
        )
        return f"<table border='1' cellpadding='4'><tr>{th}</tr>{trs}</table>"

    parts = [
        f"<h2>{escape(subject)}</h2>",
        f"<p>Mode <b>{escape(str(d['mode']))}</b> · kill switch "
        f"{'ON' if d['kill_switch'] else 'off'} · target CPA {escape(d['target_cpa'])} "
        f"{cur}</p>",
    ]
    if not d["inputs_confirmed"]:
        parts.append("<p><b>Targets/caps are placeholders (I-1/I-2 not confirmed).</b></p>")
    parts.append(
        table(
            [
                "scope",
                "spend",
                "daily cap",
                "% cap",
                "MTD",
                "monthly cap",
                "CPA 1d",
                "CPA 7d",
                "ROAS 7d",
            ],
            [
                [
                    r["scope"],
                    r["spend"],
                    r["daily_cap"],
                    r["pct_of_cap"],
                    r["mtd"],
                    r["monthly_cap"],
                    r["cpa_1d"],
                    r["cpa_7d"],
                    r["roas_7d"] if r["roas_7d"] is not None else "-",
                ]
                for r in d["spend"]
            ],
        )
    )
    for title, key in (("Top 3 winners", "winners"), ("Top 3 losers", "losers")):
        parts.append(f"<h3>{title}</h3>")
        parts.append(
            table(
                ["entity", "name", "platform", "P(beats target)", "CPA", "rank"],
                [
                    [
                        e["entity"],
                        e["name"] or "",
                        e["platform"],
                        e["p_beats_target"],
                        e["cpa_shrunk"],
                        e["rank"],
                    ]
                    for e in d[key]
                ],
            )
            if d[key]
            else "<p>No entity has enough data yet.</p>"
        )
    parts.append("<h3>Changes / rollbacks</h3>")
    parts.append(
        f"<p>{escape(', '.join(d['changes']) or 'none')} · "
        f"{escape(', '.join(d['rollbacks']) or 'no rollbacks')}</p>"
    )
    parts.append("<h3>Proposals</h3>")
    parts.append(
        f"<p>By status: {escape(str(d['proposals_by_status']))}<br>"
        f"By rule: {escape(str(d['proposals_by_rule']))}<br>"
        f"Blocked by reason: {escape(str(d['blocked_by_reason']))}</p>"
    )
    parts.append(
        table(
            ["id", "action", "entity", "expires"],
            [[a["id"], a["action"], a["entity"], a["expires_at"]] for a in d["awaiting_approval"]],
        )
    )
    if d["escalated"]:
        parts.append("<h3>Escalations for human review (LLM triage not enabled)</h3>")
        parts.append(
            table(
                ["id", "rule", "entity"],
                [[x["id"], x["rule"], x["entity"]] for x in d["escalated"]],
            )
        )
    parts.append(f"<p>LLM cost yesterday: ${escape(d['llm_cost_usd'])}</p>")
    return "<html><body>" + "".join(parts) + "</body></html>"


def store_digest(session: Session, settings: Settings, digest: Digest) -> int | None:
    return enqueue(
        session,
        settings.client_id,
        kind=NotificationKind.DIGEST,
        dedupe_key=f"digest:{digest.as_of.isoformat()}",
        subject=digest.subject,
        body_text=digest.text,
        body_html=digest.html,
        payload=digest.data,
        refresh_if_queued=True,
    )
