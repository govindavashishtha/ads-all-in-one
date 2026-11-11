"""FastAPI app (plan §17). All routes live under /api/v1; every route except /health depends
on ``api.auth.adapter.get_current_user``. Prometheus is served at root ``/metrics`` (bearer
METRICS_TOKEN), which the proxy never routes (§18) — see docs/AGENT_GUIDE.md."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX)


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only, no data (§17)."""
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(
        title="ads-autopilot",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.include_router(router)
    return app


app = create_app()
