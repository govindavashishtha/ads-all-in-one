"""``rule_stats`` bookkeeping (plan §16.3 last bullet, §16.4).

- ``ensure_rows``: one zero row per actionable rule (so the dashboard/LLM see every rule).
- ``record_verdict``: called by the evaluator (Phase 3) for D+7 / D+14 final verdicts only.
- ``learning_notes``: §16.4 — human-review notes only; nothing changes rules automatically.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import RuleStat
from engine.enums import Action, Severity, Verdict
from engine.ingest.alerts import raise_alert
from engine.rules.schema import RulesConfig

MIN_CHANGES = 20  # §16.4
WORSE_SHARE = 0.40
BETTER_SHARE = 0.70
COUNTED_HORIZONS = frozenset({7, 14})  # §16.3: only D+7/D+14 verdicts count


def actionable_rule_keys(config: RulesConfig) -> list[str]:
    return sorted(r.key for r in config.rules if r.action not in (Action.NOOP, Action.ESCALATE))


def ensure_rows(session: Session, client_id: str, keys: Iterable[str]) -> None:
    vals = [{"client_id": client_id, "rule_key": k} for k in keys]
    if vals:
        session.execute(insert(RuleStat).values(vals).on_conflict_do_nothing())


def record_verdict(
    session: Session, client_id: str, rule_key: str, verdict: Verdict, horizon_days: int
) -> bool:
    """Count one final verdict for ``rule_key``. ``insufficient_data`` and D+3 don't count.
    Returns True when a counter changed. Idempotency is the evaluator's job (one call per
    evaluation row it finalises)."""
    if horizon_days not in COUNTED_HORIZONS or verdict is Verdict.INSUFFICIENT_DATA:
        return False
    col = {Verdict.BETTER: "n_better", Verdict.NEUTRAL: "n_neutral", Verdict.WORSE: "n_worse"}[
        verdict
    ]
    stmt = insert(RuleStat).values(client_id=client_id, rule_key=rule_key, n_changes=1, **{col: 1})
    stmt = stmt.on_conflict_do_update(
        index_elements=["client_id", "rule_key"],
        set_={
            "n_changes": RuleStat.n_changes + 1,
            col: getattr(RuleStat, col) + 1,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)
    return True


@dataclass(frozen=True, slots=True)
class LearningNote:
    rule_key: str
    direction: str  # tighten | loosen
    n_changes: int
    share: float
    message: str


def learning_notes(session: Session, client_id: str) -> list[LearningNote]:
    notes: list[LearningNote] = []
    for s in session.scalars(
        select(RuleStat).where(RuleStat.client_id == client_id).order_by(RuleStat.rule_key)
    ):
        if s.n_changes < MIN_CHANGES:
            continue
        worse, better = s.n_worse / s.n_changes, s.n_better / s.n_changes
        if worse > WORSE_SHARE:
            notes.append(
                LearningNote(
                    s.rule_key,
                    "tighten",
                    s.n_changes,
                    worse,
                    f"rule {s.rule_key}: {s.n_worse}/{s.n_changes} changes judged worse "
                    f"({worse:.0%} > 40%). Consider a tighter threshold "
                    "(human decision; regression to the mean inflates 'worse').",
                )
            )
        elif better > BETTER_SHARE:
            notes.append(
                LearningNote(
                    s.rule_key,
                    "loosen",
                    s.n_changes,
                    better,
                    f"rule {s.rule_key}: {s.n_better}/{s.n_changes} changes judged better "
                    f"({better:.0%} > 70%). Consider loosening the threshold (human decision).",
                )
            )
    return notes


def raise_learning_alerts(
    session: Session, client_id: str, week_of: dt.date, notes: list[LearningNote]
) -> int:
    for n in notes:
        raise_alert(
            session,
            client_id,
            dedupe_key=f"rule_review:{n.rule_key}:{n.direction}:{week_of.isoformat()}",
            severity=Severity.WARN if n.direction == "tighten" else Severity.INFO,
            kind="rule_review",
            message=n.message,
        )
    return len(notes)
