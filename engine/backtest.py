"""Backtest / replay (plan §22 "Replay / backtest", §23 Phase 2 deliverable).

Replays features → scores → rules (+ search-term rules) → guardrail over the stored daily
history, one ``as_of`` per day, entirely in memory (nothing is written to the database), and
writes a markdown report: proposals per rule per ISO week for human review before ``suggest``
goes live.

    uv run python -m engine.backtest --out docs/backtest-synthetic.md [--from D] [--to D]
        [--assume-managed]

Guardrail: the plan runs it in ``read_only`` (every proposal → ``mode_read_only``); the report
also shows a *shadow* run in ``suggest`` mode so the other block reasons are visible.
``--assume-managed`` treats every campaign as managed in the shadow run only (I-3 opt-ins are
not known yet). Uses the active rules/limits rows from the DB (never the YAML).

Limitations (also printed in the report): entity state (status, budgets, learning, managed)
is today's, not historical — ``entity_history`` only starts at the first sync; no changes are
simulated, so cooldowns and min-active-ads never evolve; Meta frequency exists only for the
dates the windows step ran.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import shlex
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from engine.config import get_settings
from engine.db.session import make_engine, make_sessionmaker
from engine.enums import Action, AutomationScope, BudgetLevel, EntityType, Mode, Platform
from engine.features.builder import compute_features
from engine.features.data import EntityInfo, MetricsStore, Universe, load_metrics, load_universe
from engine.guardrails.check import (
    Block,
    BudgetUnitSnapshot,
    EntitySnapshot,
    GuardrailResult,
    GuardrailState,
    NeedsApproval,
    ProposalView,
    check,
)
from engine.guardrails.limits_schema import LimitsConfig
from engine.jobs.common import active_limits, active_rules
from engine.proposals import ACTIONABLE, ProposalDraft, spend_direction
from engine.ranking.rank import compute_scores
from engine.rules.engine import run_rules
from engine.rules.schema import RulesConfig
from engine.rules.search_terms import aggregate_terms, load_term_days, run_search_term_rules


@dataclass
class Replayed:
    as_of: dt.date
    draft: ProposalDraft
    read_only: GuardrailResult | None
    shadow: GuardrailResult | None


@dataclass
class BacktestResult:
    start: dt.date
    end: dt.date
    rules_version: str
    limits_version: str
    inputs_confirmed: bool
    assume_managed: bool
    days: int = 0
    matches: Counter[str] = field(default_factory=Counter)
    items: list[Replayed] = field(default_factory=list)
    sufficient_by_type: Counter[str] = field(default_factory=Counter)
    scored_by_type: Counter[str] = field(default_factory=Counter)


def _outcome(r: GuardrailResult | None) -> str:
    if r is None:
        return "n/a (not actionable)"
    if isinstance(r, Block):
        return f"block:{r.reason.value}"
    if isinstance(r, NeedsApproval):
        return "needs_approval"
    return "allow"


def _snapshot(
    universe: Universe, d: ProposalDraft, managed_override: bool | None
) -> EntitySnapshot | None:
    if d.entity_type is EntityType.SEARCH_TERM:
        gid = d.params.get("ad_group_id")
        e: EntityInfo | None = universe.entities.get((EntityType.AD_GROUP, int(gid or -1)))
    else:
        e = universe.entities.get((d.entity_type, d.entity_id))
    if e is None:
        return None
    term = d.entity_type is EntityType.SEARCH_TERM
    return EntitySnapshot(
        platform=e.platform,
        entity_type=d.entity_type,
        entity_id=d.entity_id,
        campaign_id=e.campaign_id,
        campaign_managed=e.campaign_managed if managed_override is None else managed_override,
        automation_scope=AutomationScope(e.automation_scope),
        budget_level=BudgetLevel(e.budget_level),
        is_budget_unit=False if term else e.is_budget_unit,
        learning=e.learning_effective,
        status=e.status,
        current_daily_budget=None if term or not e.is_budget_unit else e.daily_budget,
        start_of_day_budget=None if term or not e.is_budget_unit else e.daily_budget,
        active_ads_in_group=universe.active_ads_in_group.get(e.ad_group_id or -1, 0)
        if e.ad_group_id is not None
        else None,
        last_applied_change_at=None,
        last_manual_change_at=None,
        budget_if_enabled=Decimal(0),
    )


def _budget_units(universe: Universe) -> tuple[BudgetUnitSnapshot, ...]:
    return tuple(
        BudgetUnitSnapshot(e.platform, e.entity_type, e.id, e.daily_budget)
        for e in universe.of_type(EntityType.CAMPAIGN, EntityType.AD_GROUP)
        if e.effective_active
        and e.daily_budget is not None
        # shared budgets: counted per campaign here (over-counts, conservative)
        and (e.is_budget_unit or e.budget_level == BudgetLevel.SHARED.value)
    )


def _mtd(universe: Universe, store: MetricsStore, today: dt.date) -> dict[Platform, Decimal]:
    out = {p: Decimal(0) for p in Platform}
    for acct, (pl, _tz) in universe.accounts.items():
        first = today.replace(day=1)
        if today > first:
            out[pl] += store.window(
                (EntityType.ACCOUNT, acct), first, today - dt.timedelta(days=1)
            ).spend
    return out


def _guard(
    d: ProposalDraft,
    *,
    universe: Universe,
    limits: LimitsConfig,
    store: MetricsStore,
    mode: Mode,
    managed_override: bool | None,
) -> GuardrailResult | None:
    if d.action not in ACTIONABLE:
        return None
    snap = _snapshot(universe, d, managed_override)
    if snap is None:
        return None
    today = d.as_of + dt.timedelta(days=1)  # the pipeline runs the morning after as_of
    now = dt.datetime.combine(today, dt.time(6), tzinfo=dt.UTC)
    state = GuardrailState(
        mode=mode,
        kill_switch_db=False,
        kill_switch_env=False,
        limits=limits,
        entity=snap,
        active_budget_units=_budget_units(universe),
        mtd_spend=_mtd(universe, store, today),
        remaining_days_in_month=calendar.monthrange(today.year, today.month)[1] - today.day + 1,
        auto_pauses_applied_today=0,
        now=now,
    )
    view = ProposalView(
        id=None,
        source=d.source,
        rule_key=d.rule_key,
        entity_type=d.entity_type,
        entity_id=d.entity_id,
        action=d.action,
        params=d.params,
        risk=d.risk,
        spend_direction=spend_direction(d.action, d.params),
    )
    return check(view, state)


def run_backtest(
    session: Session,
    client_id: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    *,
    assume_managed: bool = False,
) -> BacktestResult:
    rules: RulesConfig = active_rules(session, client_id)
    limits = active_limits(session, client_id)
    universe = load_universe(session, client_id)
    store = load_metrics(session, client_id, None, end or dt.date.max)
    term_days, negatives = load_term_days(session, client_id, None, end or dt.date.max)
    if store.min_date is None or store.max_date is None:
        raise SystemExit("no metrics_daily rows: run the backfill first")
    first_ok = store.min_date + dt.timedelta(
        days=rules.stats.prior_window_days + rules.stats.settle_days - 1
    )
    start = max(start or first_ok, first_ok)
    end = min(end or store.max_date, store.max_date)
    res = BacktestResult(
        start,
        end,
        rules_version=str(rules.version),
        limits_version=str(limits.version),
        inputs_confirmed=rules.inputs_confirmed and limits.inputs_confirmed,
        assume_managed=assume_managed,
    )
    override = True if assume_managed else None
    as_of = start
    while as_of <= end:
        feats = compute_features(universe, store, as_of, rules)
        scores = compute_scores(universe, store, feats, as_of, rules)
        for (et, _), s in scores.items():
            res.scored_by_type[et.value] += 1
            if s["data_sufficient"]:
                res.sufficient_by_type[et.value] += 1
        run = run_rules(rules, universe.of_type(*{k[0] for k in feats}), feats, scores, as_of)
        t_matches, t_drafts = run_search_term_rules(
            rules, universe, aggregate_terms(term_days, negatives, as_of), as_of
        )
        for m in [*run.matches, *t_matches]:
            res.matches[m.rule.key] += 1
        for d in [*run.drafts, *t_drafts]:
            res.items.append(
                Replayed(
                    as_of,
                    d,
                    _guard(
                        d,
                        universe=universe,
                        limits=limits,
                        store=store,
                        mode=Mode.READ_ONLY,
                        managed_override=None,
                    ),
                    _guard(
                        d,
                        universe=universe,
                        limits=limits,
                        store=store,
                        mode=Mode.SUGGEST,
                        managed_override=override,
                    ),
                )
            )
        res.days += 1
        as_of += dt.timedelta(days=1)
    return res


def _week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _header(res: BacktestResult, title: str) -> list[str]:
    managed = " with every campaign treated as managed (`--assume-managed`)"
    confirmed = "" if res.inputs_confirmed else " — targets/caps are placeholders (I-1, I-2)"
    lines = [
        f"# {title}",
        "",
        f"- Replayed account days (`as_of`): **{res.start} → {res.end}** ({res.days} days)",
        f"- Rules config version {res.rules_version}, limits version {res.limits_version} "
        "(active rows in `config_versions`)",
        f"- Business inputs confirmed: **{res.inputs_confirmed}**{confirmed}",
        "- Guardrail: `read_only` as the plan specifies, plus a shadow run in `suggest` mode"
        f"{managed if res.assume_managed else ''}.",
        "- Nothing was written to the database; nothing touched an ad platform.",
        "",
        "## Rule matches (all outcomes, incl. noop)",
        "",
        "| rule | matches |",
        "|---|---|",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(res.matches.items())]
    lines += [
        "",
        "## Data sufficiency (entity-days scored / sufficient, §10.6)",
        "",
        "| level | scored | data_sufficient |",
        "|---|---|---|",
    ]
    lines += [
        f"| {k} | {res.scored_by_type[k]} | {res.sufficient_by_type[k]} |"
        for k in sorted(res.scored_by_type)
    ]
    return lines


def _per_week(res: BacktestResult) -> list[str]:
    weeks = sorted({_week(i.as_of) for i in res.items})
    per: dict[str, Counter[str]] = defaultdict(Counter)
    entities: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for i in res.items:
        key = i.draft.rule_key or i.draft.source.value
        per[key][_week(i.as_of)] += 1
        entities[key].add((i.draft.entity_type.value, i.draft.entity_id))
    lines = ["", "## Proposals per rule per ISO week (escalations included)", ""]
    if not res.items:
        return [*lines, "No rule produced a proposal or escalation in this period."]
    lines.append("| rule | " + " | ".join(weeks) + " | total | distinct entities |")
    lines.append("|---|" + "---|" * (len(weeks) + 2))
    for key in sorted(per):
        row = " | ".join(str(per[key].get(w, 0)) for w in weeks)
        lines.append(f"| `{key}` | {row} | {sum(per[key].values())} | {len(entities[key])} |")
    return lines


def _outcomes(res: BacktestResult, label: str, attr: str) -> list[str]:
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for i in res.items:
        outcomes[i.draft.rule_key or i.draft.source.value][_outcome(getattr(i, attr))] += 1
    lines = ["", f"## Guardrail outcomes — {label}", ""]
    if not outcomes:
        return [*lines, "(no proposals)"]
    cols = sorted({o for c in outcomes.values() for o in c})
    lines.append("| rule | " + " | ".join(f"`{c}`" for c in cols) + " |")
    lines.append("|---|" + "---|" * len(cols))
    for key in sorted(outcomes):
        lines.append(
            f"| `{key}` | " + " | ".join(str(outcomes[key].get(c, 0)) for c in cols) + " |"
        )
    return lines


def _examples(res: BacktestResult, limit: int = 25) -> list[str]:
    lines = [
        "",
        f"## Examples (first proposal per rule and entity, up to {limit})",
        "",
        "| as_of | rule | action | entity | shadow guardrail | reason |",
        "|---|---|---|---|---|---|",
    ]
    seen: set[tuple[str, str, int]] = set()
    for i in res.items:
        ek = (i.draft.rule_key or "", i.draft.entity_type.value, i.draft.entity_id)
        if ek in seen or len(seen) >= limit:
            continue
        seen.add(ek)
        params = ""
        if i.draft.action is Action.BUDGET_CHANGE:
            params = f" {i.draft.params.get('change_pct')}%"
        if i.draft.entity_type is EntityType.SEARCH_TERM:
            params = f" '{i.draft.params.get('term')}'"
        reason = i.draft.reason.replace("|", "\\|")
        lines.append(
            f"| {i.as_of} | `{i.draft.rule_key}` | {i.draft.action.value}{params} | "
            f"{i.draft.entity_type.value} {i.draft.entity_id} | {_outcome(i.shadow)} | {reason} |"
        )
    return lines


LIMITATIONS = [
    "",
    "## How to read this / limitations",
    "",
    "- Rules are stateless: an entity that keeps matching is proposed again every day "
    "(each day has its own `dedupe_key`). In production, an executed change starts the "
    "7-day cooldown, so repeats are blocked; here nothing is executed.",
    "- Entity state (status, budgets, learning, `managed`, active ads per group) is "
    "**today's**, applied to every replayed day: `entity_history` starts at the first sync, "
    "so historical budgets/learning are unknown.",
    "- Cooldowns, manual-change cooldowns and auto-pause counters never trigger "
    "(no simulated changes).",
    "- Meta `frequency_7d` exists only for days the `windows` step ran, so "
    "`fatigue_flag` is usually false in a replay.",
    "- Escalations (`action=escalate`) go to LLM triage (Phase 4); until then they are "
    "listed in the daily digest for a human.",
    "",
]


def render_report(res: BacktestResult, title: str, command: str | None = None) -> str:
    cmd = [f"- Generated by: `{command}`"] if command else []
    head = _header(res, title)
    return "\n".join(
        [
            *head[:2],
            *cmd,
            *head[2:],
            *_per_week(res),
            *_outcomes(res, "read_only (plan)", "read_only"),
            *_outcomes(res, "shadow suggest", "shadow"),
            *_examples(res),
            *LIMITATIONS,
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m engine.backtest")
    ap.add_argument("--out", required=True, help="markdown report path")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="end", type=dt.date.fromisoformat)
    ap.add_argument(
        "--assume-managed",
        action="store_true",
        help="shadow guardrail treats every campaign as managed",
    )
    ap.add_argument("--title", default="Backtest report")
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as s:
        res = run_backtest(
            s, settings.client_id, args.start, args.end, assume_managed=args.assume_managed
        )
    command = "uv run python -m engine.backtest " + shlex.join(argv or sys.argv[1:])
    Path(args.out).write_text(render_report(res, args.title, command), encoding="utf-8")
    print(f"wrote {args.out}: {res.days} days, {len(res.items)} proposals/escalations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
