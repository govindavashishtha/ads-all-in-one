# cli

Owner: API/CLI agent. Typer `ads` (§19). Calls the API only — never imports engine.db or
SQLAlchemy (enforced by tests/unit/test_import_boundaries.py). See docs/API.md § CLI.
