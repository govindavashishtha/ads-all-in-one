# engine/jobs

Owner: whoever owns the job's steps; scheduler wiring in engine/main.py. Each job takes a Postgres advisory lock and writes job_runs by window_key (§9).
