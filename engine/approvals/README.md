# engine/approvals

Owner: API agent (service) + executor agent (hand-off). §14.

`service.py` is the single source of truth: `decide`, `request_approvals`,
`expire_and_remind` (remind at 12 h, expire at 24 h), `recheck_approved` (guardrail with
`approved=True` on fresh state; Block → `blocked`). The executor applies only an `approved`
proposal whose re-check returned Allow.
