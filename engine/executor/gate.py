"""The write gate: may this process write to an ad platform at all? (DECISIONS D-28)

Checked before every platform write (proposal execution, emergency stop, pause-all,
rollback, restore), in addition to the guardrail:

1. ``clients.execution_enabled`` (DB, default **false**, admin-only ``POST /control/execution``)
   — the master switch; nothing is ever written while it is off. Plan §0.5: it stays off until
   Govinda signs off the Phase 2 exit criteria.
2. mode ≠ ``read_only`` (§12.1: read_only never uses a write credential) and mode ≤
   ``HARD_MAX_MODE`` (§7.1 ceiling, re-checked here so a DB edit can't exceed it).
3. Kill switch (DB or env) blocks anything that is not spend-reducing (§12.3 step 2).

A missing / read-only write credential is the next barrier (``engine.executor.clients``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import Client
from engine.enums import Mode


@dataclass(frozen=True, slots=True)
class Gate:
    open: bool
    reason: str | None
    mode: Mode
    execution_enabled: bool
    kill_switch: bool


def write_gate(session: Session, settings: Settings, *, reducing: bool) -> Gate:
    client = session.get(Client, settings.client_id)
    if client is None:
        return Gate(False, "clients row missing", Mode.READ_ONLY, False, False)
    mode = Mode(client.mode)
    enabled = bool(client.execution_enabled)
    kill = settings.effective_kill_switch(bool(client.kill_switch))

    def closed(reason: str) -> Gate:
        return Gate(False, reason, mode, enabled, kill)

    if not enabled:
        return closed("execution disabled (clients.execution_enabled = false)")
    if mode is Mode.READ_ONLY:
        return closed("mode read_only")
    if not settings.mode_allowed(mode):
        return closed(f"mode {mode.value} above HARD_MAX_MODE={settings.hard_max_mode.value}")
    if kill and not reducing:
        return closed("kill switch on")
    return Gate(True, None, mode, enabled, kill)
