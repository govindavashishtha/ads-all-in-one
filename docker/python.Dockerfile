# Shared image for the engine and api services.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY engine ./engine
COPY api ./api
COPY cli ./cli
COPY db ./db
COPY docker/engine-entrypoint.sh ./docker/engine-entrypoint.sh
RUN uv sync --frozen --no-dev && chmod +x docker/engine-entrypoint.sh \
    && useradd --system --uid 10001 app
USER app
