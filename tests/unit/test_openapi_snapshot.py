"""docs/openapi.json must match the app (regenerate: `uv run python -m api.openapi_export`)."""

from __future__ import annotations

from api.openapi_export import OUT, render


def test_openapi_json_is_current() -> None:
    assert OUT.read_text(encoding="utf-8") == render(), (
        "docs/openapi.json is stale; run `uv run python -m api.openapi_export`"
    )
