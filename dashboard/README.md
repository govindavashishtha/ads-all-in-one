# dashboard

Next.js 16 (App Router) + TypeScript + Tailwind 4 + Recharts — BUILD_PLAN §5, §18.
Owner: dashboard agent. Types are generated from `../docs/openapi.json` (`npm run gen:api` → `src/lib/openapi.gen.ts`); contract gaps the dashboard works around: [API_CONTRACT.md](API_CONTRACT.md).

## Build-time configuration (Docker build args)
| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_BASE_PATH` | `/ads-autopilot` | Next.js `basePath`; the proxy forwards `/ads-autopilot/*` unchanged |
| `NEXT_PUBLIC_API_URL` | `/ads-autopilot/api/v1` | all fetches go here (browser-side, same origin) |
| `NEXT_PUBLIC_MOCK_API` | `false` | `true` = no backend; in-browser fake API with **EXAMPLE DATA** banner on every page |
| `NEXT_PUBLIC_CSRF_COOKIE` | `aa_csrf` | name of the double-submit CSRF cookie set by the API |

All are inlined at build time; changing any of them needs a rebuild. The container listens on 3000.

## Run
```bash
npm ci
npm run dev                         # http://localhost:3000/ads-autopilot (needs the API, or:)
NEXT_PUBLIC_MOCK_API=true npm run dev   # demo with example data, any login works
npm run build && npm start          # production (output: standalone)
npm run lint && npm run typecheck && npm test
docker build --build-arg NEXT_PUBLIC_MOCK_API=true -t ads-dashboard-demo .
```
Screenshots + browser smoke test (mock build served behind the proxy):
`BASE_URL=http://127.0.0.1:8080/ads-autopilot npm run screenshots` (needs `npx playwright install chromium`).
Mock knob: `localStorage['ads-autopilot:mockPhase'] = '1'..'5'` (default 5) makes the fake API behave
like the real one before that phase ships: empty rankings/proposals before 2, empty change log and
501 `not_enabled` for dry-run/rollback before 3, `llm_enabled: false` before 4.

## Layout
- `src/lib/api.ts` — the only module with URLs/methods/CSRF; `src/lib/types.ts` — aliases of the
  generated OpenAPI schemas; `src/lib/overview.ts` — composes the Overview from /overview, /metrics,
  /system, /rules and /alerts.
- `src/lib/mock/` — example data + fake transport (mock mode only; lazily loaded).
- `src/app/login` — login; `src/app/(app)/*` — authenticated pages: Overview, Campaigns
  (+ `[id]` drill-down to ad sets/ad groups and ads), Rankings, Approvals (+ `[id]` = approval
  link target; GET only shows, decisions are POST), Change log, AI usage, System.
- Phase 1 pages (Overview, Campaigns, System, login) are fully wired. Later-phase pages
  render a "not enabled yet" card when their API returns 501 `not_enabled` (or 404), or
  when the response says the feature is off (rankings `as_of: null`, `llm_enabled: false`).
- Currency formatting follows `overview.currency` (the ad accounts' currency; INR → en-IN
  grouping); nothing is hardcoded. LLM costs are USD by design (§13.3).
