# cli

Owner: API/CLI agent. Typer `ads` (§19). Calls the API only — never imports engine.db or
SQLAlchemy (enforced by tests/unit/test_import_boundaries.py). See docs/API.md § CLI.

Write-path commands (Phase 3, docs/API.md): `dry-run`, `rollback`, `pause-all`,
`restore`, `write-request`, `execution on|off`. Rollback / pause-all / restore print the
queued `write_request` id (API 202); they do not wait for the engine to apply it.
