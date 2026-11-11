"""Write the OpenAPI document to docs/openapi.json: ``uv run python -m api.openapi_export``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from api.main import create_app
from engine.config import Settings

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def build_openapi() -> dict[str, Any]:
    # No DB connection is made: the session factory is never called while building the schema.
    app = create_app(settings=Settings(_env_file=None), session_factory=sessionmaker())
    return app.openapi()


def render() -> str:
    return json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
