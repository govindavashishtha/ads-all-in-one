"""``pacing`` job, every 15 min — Phase 1 = ALERTS ONLY (plan §12.4 steps 1–5, §21).

Reads today's spend (account + campaign) from the platform, projects end-of-day spend with the
intraday curve, and raises deduped ``warn`` / ``critical`` alerts. It never writes to a
platform and never imports a WriteConnector: in Phase 1 an emergency condition only raises the
``critical`` alert (in every mode). The executor agent adds the pause path in
``engine/pacing/emergency.py`` at Phase 3 (hook: ``EMERGENCY_HANDLER``). Pings
``HEARTBEAT_URL`` after a successful run (§21 dead-man's switch).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from engine.connectors.base import EntityStatus, SpendSnapshot
from engine.db.models import AdAccount, AdGroup, Campaign
from engine.enums import BudgetLevel, EntityType, Mode, Platform, Severity
from engine.guardrails.limits_schema import LimitsConfig
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_limits, client_mode, enabled_connectors
from engine.jobs.runner import JobContext, StepOutcome, StepResult, job_lock, run_step
from engine.system_status import HEARTBEAT, pacing_key, put_status

log = structlog.get_logger(__name__)

CURVE_DAYS = 28  # §12.4 step 2
CURVE_MIN_DAYS = 14
CURVE_FLOOR = 0.05  # §12.4 step 3


@dataclass(frozen=True, slots=True)
class EmergencyCondition:
    platform: Platform | None  # None = total across platforms
    trigger: str  # daily_cap | monthly_cap
    spent: Decimal
    cap: Decimal


# Phase 3 hook (executor agent): called with the conditions after alerts are raised.
EMERGENCY_HANDLER: Callable[[Session, Sequence[EmergencyCondition], Mode], None] | None = None


# ----------------------------------------------------------------------- pure functions


def intraday_curve(day_hours: Sequence[Sequence[Decimal]]) -> list[float] | None:
    """Average cumulative share by hour over complete days (§12.4 step 2).

    ``day_hours`` = per day, 24 hourly spend values. Returns ``c[h]`` = mean over days of
    (spend in hours ``< h`` ÷ day total) for h = 0..24, or None if fewer than 14 usable days.
    """
    usable = [d for d in day_hours if len(d) == 24 and sum(d) > 0]
    if len(usable) < CURVE_MIN_DAYS:
        return None
    acc = [0.0] * 25
    for d in usable:
        total = float(sum(d))
        running = 0.0
        for h in range(25):
            acc[h] += running / total
            if h < 24:
                running += float(d[h])
    return [a / len(usable) for a in acc]


def curve_at(curve: Sequence[float] | None, elapsed_hours: float) -> float:
    """Expected spent fraction after ``elapsed_hours`` (linear h/24 fallback, §12.4 step 2)."""
    h = min(max(elapsed_hours, 0.0), 24.0)
    if curve is None:
        return h / 24.0
    lo = int(h)
    if lo >= 24:
        return curve[24]
    return curve[lo] + (curve[lo + 1] - curve[lo]) * (h - lo)


def project_eod(spend_today: Decimal, fraction: float) -> Decimal:
    return (spend_today / Decimal(str(max(fraction, CURVE_FLOOR)))).quantize(Decimal("0.01"))


@dataclass
class PlatformPacing:
    platform: Platform
    local_date: dt.date
    elapsed_hours: float
    spend_today: Decimal
    mtd_spend: Decimal
    daily_cap: Decimal
    monthly_cap: Decimal
    projected_eod: Decimal | None
    curve_source: str = "linear"  # "learned" (§12.4 step 2) or the h/24 fallback
    warn_reasons: list[str] = field(default_factory=list)
    emergency: list[EmergencyCondition] = field(default_factory=list)


def evaluate_platform(
    snap: SpendSnapshot,
    *,
    limits: LimitsConfig,
    elapsed_hours: float,
    curve: Sequence[float] | None,
    mtd_before_today: Decimal,
    campaign_budgets: dict[str, tuple[str | None, Decimal | None]],
) -> PlatformPacing:
    """§12.4 steps 3–5 for one platform. ``campaign_budgets``: ext id → (name, daily budget)."""
    cap = limits.caps.for_platform(snap.platform)
    p = limits.pacing
    spend = snap.account_spend
    out = PlatformPacing(
        platform=snap.platform,
        local_date=snap.account_local_date,
        elapsed_hours=elapsed_hours,
        spend_today=spend,
        mtd_spend=mtd_before_today + spend,
        daily_cap=cap.daily,
        monthly_cap=cap.monthly,
        projected_eod=None,
        curve_source="linear" if curve is None else "learned",
    )
    if Decimal(str(elapsed_hours)) >= p.min_elapsed_hours:
        out.projected_eod = project_eod(spend, curve_at(curve, elapsed_hours))
    if spend >= p.warn_spent_fraction * cap.daily:
        out.warn_reasons.append(
            f"spend today {spend} ≥ {p.warn_spent_fraction:.0%} of daily cap {cap.daily}"
        )
    if out.projected_eod is not None and out.projected_eod > p.warn_projected_ratio * cap.daily:
        out.warn_reasons.append(
            f"projected end-of-day {out.projected_eod} > {p.warn_projected_ratio}× daily cap "
            f"{cap.daily}"
        )
    flex = getattr(p.platform_flex, snap.platform.value)
    for ext, camp_spend in sorted(snap.campaign_spend.items()):
        name, budget = campaign_budgets.get(ext, (None, None))
        if budget is not None and budget > 0 and camp_spend > flex * budget:
            out.warn_reasons.append(
                f"campaign {name or ext} spent {camp_spend} > {flex}× its daily budget {budget}"
            )
    if spend >= p.emergency_spent_fraction * cap.daily:
        out.emergency.append(EmergencyCondition(snap.platform, "daily_cap", spend, cap.daily))
    if out.mtd_spend >= p.emergency_spent_fraction * cap.monthly:
        out.emergency.append(
            EmergencyCondition(snap.platform, "monthly_cap", out.mtd_spend, cap.monthly)
        )
    return out


# ----------------------------------------------------------------------- DB reads


def _account(s: Session, client_id: str, platform: Platform) -> AdAccount | None:
    return s.scalars(
        select(AdAccount).where(AdAccount.client_id == client_id, AdAccount.platform == platform)
    ).first()


def load_curve(
    s: Session, client_id: str, account_id: int, platform: Platform, today: dt.date
) -> list[float] | None:
    rows = s.execute(
        text(
            "SELECT ts_hour, spend FROM metrics_hourly WHERE client_id = :c AND platform = "
            "CAST(:p AS platform) AND entity_type = 'account' AND entity_id = :e "
            "AND ts_hour >= :start AND ts_hour < :end"
        ),
        {
            "c": client_id,
            "p": platform.value,
            "e": account_id,
            "start": dt.datetime.combine(today - dt.timedelta(days=CURVE_DAYS), dt.time(0)),
            "end": dt.datetime.combine(today, dt.time(0)),
        },
    ).all()
    days: dict[dt.date, list[Decimal]] = {}
    for ts, spend in rows:
        days.setdefault(ts.date(), [Decimal(0)] * 24)[ts.hour] = Decimal(spend)
    return intraday_curve(list(days.values()))


def mtd_before(
    s: Session, client_id: str, account_id: int, platform: Platform, today: dt.date
) -> Decimal:
    value = s.execute(
        text(
            "SELECT COALESCE(SUM(spend), 0) FROM metrics_daily WHERE client_id = :c AND "
            "platform = CAST(:p AS platform) AND entity_type = 'account' AND entity_id = :e "
            "AND date >= :start AND date < :today"
        ),
        {
            "c": client_id,
            "p": platform.value,
            "e": account_id,
            "start": today.replace(day=1),
            "today": today,
        },
    ).scalar()
    return Decimal(value or 0)


def campaign_budgets(
    s: Session, client_id: str, platform: Platform
) -> dict[str, tuple[str | None, Decimal | None]]:
    """Daily budget per campaign; ad-set-budget campaigns use the sum of active ad sets."""
    out: dict[str, tuple[str | None, Decimal | None]] = {}
    camps = s.execute(
        select(
            Campaign.id,
            Campaign.external_id,
            Campaign.name,
            Campaign.budget_level,
            Campaign.daily_budget,
        ).where(Campaign.client_id == client_id, Campaign.platform == platform)
    ).all()
    sums: dict[int, Decimal] = {}
    for cid, budget in s.execute(
        select(AdGroup.campaign_id, AdGroup.daily_budget).where(
            AdGroup.client_id == client_id,
            AdGroup.platform == platform,
            AdGroup.status == EntityStatus.ACTIVE.value,
            AdGroup.daily_budget.is_not(None),
        )
    ):
        sums[cid] = sums.get(cid, Decimal(0)) + Decimal(str(budget))
    for cid, ext, name, level, budget in camps:
        b = sums.get(cid) if level == BudgetLevel.AD_SET.value else budget
        out[str(ext)] = (name, None if b is None else Decimal(b))
    return out


# ----------------------------------------------------------------------- job


def pacing(ctx: JobContext, *, ping: Callable[[str], None] | None = None) -> StepResult | None:
    now = ctx.now()
    local = now.astimezone(ZoneInfo(ctx.settings.timezone))
    slot = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    window = slot.strftime("%Y-%m-%dT%H:%M")

    def body(s: Session) -> StepOutcome:
        mode = client_mode(s, ctx.client_id)
        limits = active_limits(s, ctx.client_id)
        enabled = enabled_connectors(ctx, s)
        results: list[PlatformPacing] = []
        for pc in enabled.connectors:
            snap = pc.connector.spend_today()
            acct = _account(s, ctx.client_id, pc.platform)
            if acct is None:
                log.warning("pacing.account_not_synced", platform=pc.platform.value)
                continue
            acct_local = now.astimezone(ZoneInfo(acct.timezone))
            elapsed = acct_local.hour + acct_local.minute / 60
            results.append(
                evaluate_platform(
                    snap,
                    limits=limits,
                    elapsed_hours=elapsed,
                    curve=load_curve(
                        s, ctx.client_id, acct.id, pc.platform, snap.account_local_date
                    ),
                    mtd_before_today=mtd_before(
                        s, ctx.client_id, acct.id, pc.platform, snap.account_local_date
                    ),
                    campaign_budgets=campaign_budgets(s, ctx.client_id, pc.platform),
                )
            )
        alerts = _raise_pacing_alerts(ctx, s, results, limits, mode)
        for r in results:
            put_status(s, ctx.client_id, pacing_key(r.platform.value), snapshot_json(r), now)
        return StepOutcome(
            rows=alerts,
            skipped_reason="; ".join(f"{p.value}: {w}" for p, w in enabled.skipped.items()) or None,
            details={r.platform.value: str(r.spend_today) for r in results},
        )

    with job_lock(ctx.engine, ctx.client_id, "pacing") as got:
        if not got:
            return None
        result = run_step(ctx, "pacing", None, window, body)
    if result.status is not None:
        _heartbeat(ctx, ping, now)
    return result


def snapshot_json(r: PlatformPacing) -> dict[str, object]:
    """``system_status['pacing:<platform>']`` — read by GET /overview (projected EOD)."""
    return {
        "local_date": r.local_date.isoformat(),
        "elapsed_hours": round(r.elapsed_hours, 4),
        "spend_today": str(r.spend_today),
        "projected_eod": None if r.projected_eod is None else str(r.projected_eod),
        "daily_cap": str(r.daily_cap),
        "curve_source": r.curve_source,
        "warn": bool(r.warn_reasons),
        "emergency": bool(r.emergency),
    }


def _heartbeat(ctx: JobContext, ping: Callable[[str], None] | None, now: dt.datetime) -> None:
    """Ping ``HEARTBEAT_URL`` (§21) and publish the outcome for GET /system."""
    url = ctx.settings.heartbeat_url
    status = "not_configured"
    error: str | None = None
    if url:
        try:
            (ping or _http_ping)(url)
            status = "ok"
        except Exception as exc:  # a failed ping must never fail the pacing job
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            log.warning("pacing.heartbeat_failed", error=error)
    try:
        with ctx.sessions() as s, s.begin():
            put_status(
                s,
                ctx.client_id,
                HEARTBEAT,
                {
                    "configured": bool(url),
                    "last_status": status,
                    "last_ping_at": now.isoformat() if url else None,
                    "last_pacing_run_at": now.isoformat(),
                    "error": error,
                },
                now,
            )
    except Exception:
        log.exception("pacing.heartbeat_status_write_failed")


def _raise_pacing_alerts(
    ctx: JobContext, s: Session, results: list[PlatformPacing], limits: LimitsConfig, mode: Mode
) -> int:
    n = 0
    for r in results:
        if r.warn_reasons:
            raise_alert(
                s,
                ctx.client_id,
                dedupe_key=f"pacing_warn:{r.platform.value}:{r.local_date.isoformat()}",
                severity=Severity.WARN,
                kind="pacing_warn",
                entity_type=EntityType.ACCOUNT,
                message=f"{r.platform.value} pacing: " + "; ".join(r.warn_reasons),
            )
            n += 1
    conditions = [c for r in results for c in r.emergency]
    if results:
        total_spend = sum((r.spend_today for r in results), Decimal(0))
        total_mtd = sum((r.mtd_spend for r in results), Decimal(0))
        frac = limits.pacing.emergency_spent_fraction
        if total_spend >= frac * limits.caps.total.daily:
            conditions.append(
                EmergencyCondition(None, "daily_cap", total_spend, limits.caps.total.daily)
            )
        if total_mtd >= frac * limits.caps.total.monthly:
            conditions.append(
                EmergencyCondition(None, "monthly_cap", total_mtd, limits.caps.total.monthly)
            )
    day = results[0].local_date.isoformat() if results else ""
    for c in conditions:
        scope = c.platform.value if c.platform else "total"
        why = (
            "mode read_only: alert only"
            if mode is Mode.READ_ONLY
            else "Phase 1: emergency stop not wired yet — alert only, NOTHING was paused"
        )
        raise_alert(
            s,
            ctx.client_id,
            dedupe_key=f"emergency:{scope}:{c.trigger}:{day}",
            severity=Severity.CRITICAL,
            kind="emergency_stop",
            message=f"{scope} {c.trigger}: spent {c.spent} ≥ "
            f"{limits.pacing.emergency_spent_fraction:.0%} of cap {c.cap} ({why})",
        )
        n += 1
    if conditions and EMERGENCY_HANDLER is not None and limits.pacing.emergency_stop_enabled:
        EMERGENCY_HANDLER(s, conditions, mode)
    return n


def _http_ping(url: str) -> None:
    """Raises on transport errors and non-2xx answers (recorded as ``failed``)."""
    httpx.get(url, timeout=10.0).raise_for_status()
