# api/routes

Owner: API agent. All routes under /api/v1 (§17), every route except /health behind
`api.auth.adapter.require(role)`. Endpoint list + status: [docs/API.md](../../docs/API.md).
Regenerate docs/openapi.json after changing any route or schema:
`uv run python -m api.openapi_export`.
