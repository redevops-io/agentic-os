# Gate notifications (ER-07)

Pushes a parked human-approval gate to people through the existing `channels.py` channels, durably and
without pretending to be exactly-once. Implemented in `agentic_os/mission/gate_notify.py`. No new dependency
or service (stdlib SQLite for the outbox).

## Enabling (default OFF)
Nothing is installed unless `AGENTIC_OS_GATE_NOTIFY=1` **and** a recipient on a configured channel exists.
- `AGENTIC_OS_GATE_NOTIFY_RECIPIENTS="alice:slack:C123,bob:telegram:555"` (`id:channel[:address]`)
- `AGENTIC_OS_GATE_NOTIFY_ESCALATE_TO="boss:slack:C999"`
- `AGENTIC_OS_GATE_NOTIFY_LINK_BASE=https://console/needs-you`
- or `AGENTIC_OS_GATE_NOTIFY_CONFIG=/path.json` with `recipients`, `escalation`, `link_base`, `outbox` (sqlite path),
  `reminder_after` (900s), `escalate_after` (1800s), `expire_after` (86400s), `backoff_base`/`backoff_cap`,
  `max_attempts` (8), `rate_max`/`rate_per` (5 per 60s per recipient).
- Channel credentials stay in the existing `AGENTIC_OS_*` channel env vars.

## Flow
`on_gate_reached` (LifecycleContributor) enqueues one outbox row per recipient. It ignores `GateReached`
fired for policy-denied blocks (no human can act). `tick(now)` (and the optional daemon thread, `start()`)
sends due rows, retries with exponential backoff to a cap, enqueues ONE reminder (skipped if any notice was
acknowledged), ONE escalation to the secondary recipients, and after the TTL appends a durable
`GateExpired` event plus an "expired" notice.

## Guarantees, stated honestly
- Transport is **at-least-once**; processing is **idempotent**. Key = sha256(mission, node, gate_version,
  recipient, channel, kind); `gate_version` = seq of the `NodeParked` event. Duplicate hook calls, ticks and
  restarts never create a second outbox row, and a row with a recorded receipt is never re-sent.
- **Duplicate external messages are still possible**: if a provider delivers but the ack is lost (timeout),
  the retry sends again. The harness measures this (see below). We do not promise zero duplicates.
- A crash between a successful send and the receipt write leaves the row retryable (same duplicate risk).
- Retry classification: timeouts/network, 408/425/429, 5xx and Slack `ratelimited` are retried; other 4xx and
  provider rejections are permanent (`failed`). Errors are redacted; tokens are never logged or stored.

## Approval is never granted by a notice
Notices contain capability, mission id and a link to the authenticated console. They never contain the
signed approval token or any secret, and text says replying does not approve. Nothing parses chat replies.
Approval happens only through the existing authenticated control path (`control_auth`, `runtime.approve`).

## Expiry
`GateExpired {node_id, gate_version, expired_at, auto_decision: null}` is appended to the mission log. It is
not a decision: the mission stays `waiting_human`, `NodeParked` remains open, and `approve` still works.
`MissionRepository.pending_human` adds `expired: true` (a read-only fold); other folds ignore the event.

## Receipts and Needs You
`GET /api/needs-you` items gain `expired` and `notices[]` (kind, recipient, channel, state
`pending|retrying|delivered|failed|cancelled|acknowledged`, attempts, provider_msg_id). No addresses or text.

## Inbound callbacks (acknowledge only)
`POST /api/gate-notify/callback` (503 unless the notifier and `AGENTIC_OS_GATE_NOTIFY_CALLBACK_SECRET` are set).
Headers `X-AOS-Timestamp`, `X-AOS-Signature` = hex HMAC-SHA256 of `v0:<ts>:<raw body>`; 5-minute window;
signatures are single-use (durable replay table). Body `{"action":"ack","key":"<notification key>"}`.
Any other action, including approve/reject, is refused. This is a relay-signed callback, not a native
Slack/Teams signature verifier; the collaboration providers' button callbacks remain unwired.

## Channel fixes
Twilio now form-encodes; 10 s timeouts; `send()` returns a `SendResult` (ok/status/message_id/permanent);
the legacy approval text no longer promises "reply: approve <id>".

## Metrics harness (`tests/test_gate_notify.py::test_harness_er07_targets`)
Fake transport + fake clock. Healthy transport: 20/20 gates delivered within 60 s. Duplicate hook calls x3 plus
repeated ticks: exactly 1 outbox row. External sends under 0/1/2 injected lost-acks: 1/2/3 (duplicates track
ambiguous acks). Forged/unsigned/stale/replayed callbacks accepted: 0. Old pending gate: exactly one reminder,
one escalation, one `GateExpired`, zero approval events. `time_to_decision()` reports per-gate seconds.
