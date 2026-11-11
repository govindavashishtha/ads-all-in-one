# engine/notify

Owner: API agent. One `notify()` interface over Slack / SMTP / WhatsApp (§5, §14).

`outbox.enqueue` writes a `notifications` row; `dispatch.deliver_pending` (job
`notify_dispatch`) offers it to deliverable channels. Adapters are stubs: deliverable only
when configured AND a transport is injected — none is by default, so everything is logged
only (DECISIONS D-18). `digest.py` builds/stores the daily digest (text + HTML).
