# dashboard

Owner: dashboard agent. Next.js (App Router) + TypeScript + Recharts + Tailwind (§5, §18).

- `basePath = NEXT_PUBLIC_BASE_PATH` and all fetches go to `NEXT_PUBLIC_API_URL`; both are
  **build-time** Docker build args (changing them needs an image rebuild).
- Must listen on port 3000 inside the container; the proxy forwards `/ads-autopilot/*` to it
  without rewriting.
- The current `Dockerfile` + `server.js` are a placeholder so `docker compose up` works;
  replace both with the real Next.js app (keep the same build args and port).
