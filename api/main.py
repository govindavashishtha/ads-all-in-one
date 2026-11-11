"""FastAPI app (plan §17). All routes live under /api/v1; every route except /health depends
on ``api.auth.adapter.get_current_user`` (via ``require(role)``). Prometheus is served at root
``/metrics`` (bearer METRICS_TOKEN), which the proxy never routes (§18) — see
docs/AGENT_GUIDE.md."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy.orm import Session, sessionmaker

from api.routes import (
    auth,
    changes,
    config,
    entities,
    llm,
    metrics,
    ops,
    overview,
    proposals,
    rankings,
)
from engine.config import Settings, get_settings
from engine.db.session import make_engine, make_sessionmaker

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX)


@router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness only, no data (§17)."""
    return {"status": "ok"}


for _module in (auth, overview, entities, metrics, rankings, proposals, changes, config, llm, ops):
    router.include_router(_module.router)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="ads-autopilot",
        version="0.1.0",
        description="ads-autopilot API (BUILD_PLAN v1.1 §17). See docs/API.md.",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.session_factory = session_factory or make_sessionmaker(
        make_engine(settings.database_url)
    )
    registry = CollectorRegistry()
    requests_total = Counter(
        "api_requests_total", "API requests", ["method", "status"], registry=registry
    )

    @app.middleware("http")
    async def _count(request: Request, call_next: Callable) -> Response:  # type: ignore[type-arg]
        response: Response = await call_next(request)
        requests_total.labels(request.method, str(response.status_code)).inc()
        return response

    @app.get("/metrics", include_in_schema=False)
    def prometheus(request: Request) -> Response:
        """Prometheus scrape (§21). Bearer METRICS_TOKEN; 404 if unset. Not proxied (§18)."""
        token = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
        if not token:
            raise HTTPException(404, "Not Found")
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "bad metrics token")
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
    return app


app = create_app()
