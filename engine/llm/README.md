# engine/llm — Phase 4 LLM layer (plan §13, §20, §26)

Owner: LLM agent. `client.py` is the shared interface (lead-owned); everything else is here.

| Module | Does |
|---|---|
| `providers.py` | `AnthropicClient` (Messages API, system prompt marked cacheable), `OpenAICompatibleClient` (Chat Completions, any compatible gateway), `build_client(settings)` |
| `fake.py` | deterministic `FakeLLM` (tests, golden set, local demo); optional `script` for raw outputs |
| `prices.py` | price table from `LLM_*_PRICE_PER_MTOK` (INPUT I-8, never hard-coded) |
| `budget.py` | token budget controller: daily token cap, daily + monthly cost caps, priority shares |
| `gateway.py` | the only path to a provider: 24 h `prompt_hash` cache → budget check → call → `llm_calls` row (own transaction, advisory lock) |
| `summariser.py` | compact-JSON cases (rounded numbers, short keys, ≤ 20 per call, no raw rows) + per-entity action whitelist |
| `untrusted.py` | untrusted-text isolation: names/ad copy only under `untrusted_text`, cleaned, capped, flagged |
| `schemas.py` | strict pydantic output schemas (extra keys forbidden, `change_pct` int in [-20, 20], ≤ 40-word reasons, no links/markup) |
| `triage.py` | request builder + parser for triage (small) and analyst (large); engine-built proposals |
| `creative.py` | weekly creative drafts → `new_ad` proposal per ad group, `risk=high`, `create_status=PAUSED` |
| `steps.py` | `triage_step` (daily_pipeline step 7) and `creative_step` (weekly_strategy) |
| `status.py` | `llm_status(settings)` for the AI usage page (`llm_enabled`, why off, caps) |
| `prompts/` | static system prompts (cacheable prefix) |

## Configuration (INPUT I-8 — nothing is defaulted)

`LLM_PROVIDER` turns the layer on and picks the adapter (no extra env keys, because
`.env.example` must stay identical to plan §7.2 — DECISIONS D-24):

| `LLM_PROVIDER` | Meaning |
|---|---|
| empty / `disabled` | LLM off. Escalations → `flag_human` alerts; every pipeline completes |
| `anthropic` or `anthropic:<https base>` | Anthropic Messages API |
| `openai` or `openai:<https base>` | OpenAI-compatible Chat Completions (e.g. a gateway) |
| `fake` | `FakeLLM`, only with `APP_ENV=development` |

A real provider also needs `LLM_API_KEY`, `LLM_SMALL_MODEL`, `LLM_LARGE_MODEL` and all four
`LLM_{SMALL,LARGE}_{IN,OUT}_PRICE_PER_MTOK`; if any is missing the layer stays off (it never
calls a model whose cost it cannot cap). Caps: `LLM_DAILY_TOKEN_CAP`,
`LLM_DAILY_COST_CAP_USD`, `LLM_MONTHLY_COST_CAP_USD` (client-timezone day / month).

## Flow (daily `triage` step)

```
escalated proposals (as_of, not fatigue, not yet triaged)
  → summariser (≤ 20 cases/request, allowed_actions per case)
  → gateway: cache? → budget ok? → small model → ledger row
  → strict parse + whitelist + confidence ≥ 0.5
      pause / budget_change → new proposal (source llm_triage, risk medium, status pending)
      escalate_large → analyst (large model, same path, source llm_analyst)
      noop → annotated; flag_human / invalid / over budget / error → warn alert
  → guardrail step (next) judges every new proposal like any other
```

The escalated proposal keeps status `escalated` and gets `params.triage`
(`outcome`, `action`, `confidence`, `llm_call_id`, `proposal_id`, `cause`).

## Security (§20, §26)

1. Untrusted text only under `untrusted_text` (JSON-escaped, cleaned, ≤ 200 chars, flagged when
   it looks like an instruction); the system prompts say it is data.
2. Strict output schema; the model never supplies ids, budgets, URLs or free parameters.
3. Two whitelists: the global action enum and the engine-computed per-case `allowed_actions`.
4. Every LLM proposal passes the guardrail; LLM proposals are `risk=medium`.
5. Generated copy is only a `new_ad` proposal with `risk=high` (human in every mode), created
   PAUSED, with links/markup rejected and platform length limits enforced.

## Not done yet

- Provider batch API for the weekly job (§13.3): weekly requests are sent synchronously.
- Daily digest narrative, search-term relevance batch, weekly analyst on top movers (§13.1).
- `/llm/usage` still returns `llm_enabled=false`; wire it to `engine.llm.status.llm_status`.
- Nightly golden run against the real model (needs I-8).
