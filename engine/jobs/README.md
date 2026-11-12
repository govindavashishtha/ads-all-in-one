# engine/jobs

Owner: whoever owns the job's steps; scheduler wiring in engine/main.py. Each job takes a Postgres advisory lock and writes job_runs by window_key (§9).

Phase 3 jobs (executor): `execute_approved` (every 5 min), `evaluate` (:40),
`reconcile` (:20), `emergency_restore` (:10), `token_reset` (00:00), `write_requests`
(every minute). All platform writes go through the executor write gate (D-28).
