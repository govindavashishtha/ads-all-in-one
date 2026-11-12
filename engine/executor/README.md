# engine/executor

Owner: executor/evaluator agent. The only package (plus `pacing/emergency.py`) that may import
a WriteConnector.

Write path (plan §15, D-27..D-34), against **fake platforms** until the Phase 3 gate:

- `plan.py` — turn a proposal into `WriteOp`s (pause/enable, budget, negatives, new ads).
- `gate.py` — write gate: `execution_enabled`, mode, `HARD_MAX_MODE`, kill switch (D-28).
- `clients.py` — `WriteClientProvider`; `RealWriteClients` (write credential only) and
  `FakeWriteClients`.
- `apply.py` — write-ahead `change_log` row, validate-only, apply, read-back, entity history.
- `execute.py` — lock approved proposal, recheck guardrail, apply planned ops, evaluations.
- `reconcile.py` — finish `applying` rows and stale `executing` proposals (D-30).
- `rollback.py` — guarded rollback from `before`/`after` (D-33); drift → proposal.
- `requests.py` — poller for the `write_requests` queue (API 202, D-29).
- `jobs.py` — `execute_approved` (every 5 min), `evaluate`, `reconcile`, `emergency_restore`,
  `token_reset`, `write_requests`, startup reconcile.
- `fake_world.py` — `fake_platforms_from_db` for demos and tests.

The API may import `plan` and `gate` only (dry-run / write-gate status). Demo on a copy DB
with fakes: [`scripts/exec_demo.py`](../../scripts/exec_demo.py), [docs/EXEC_DEMO.md](../../docs/EXEC_DEMO.md).
