"""Integration plane Phase 1 — durable event ingestion + cursors + DLQ/replay.

Phase-1 exit (§39): an intentionally dropped destination write is detected automatically — and here it goes
further, through the DURABLE loop: webhook ingest (signature + dedup) → normalize → obligation → silent failure
→ ranked exception in the DLQ → fix → replay from the failed point → satisfied + resolved. Offline.
"""
from __future__ import annotations

from agentic_os.integration import (
    ActionResult, CursorStore, EventInbox, EventRejected, ExceptionStore, InMemoryIntegrationProvider,
    NormalizedEvent, Obligation, ObligationEngine, ObligationStatus, RetryPolicy, detect_missed_events,
    hmac_signature, priority_score, verify_hmac,
)
from agentic_os.integration.contracts import ExceptionCategory, IntegrationException


# ── event inbox: signature, append-only, dedup ───────────────────────────────────────────────────────────
def test_inbox_validates_signature_appends_and_dedupes():
    inbox = EventInbox(secrets={"stripe": "whsec_test"})
    body = b'{"id":"evt_1","type":"payout.created"}'
    good_sig = "sha256=" + hmac_signature("whsec_test", body)

    # bad signature → rejected, nothing appended
    try:
        inbox.ingest("stripe", "payout.created", "evt_1", body, headers={"x-signature": "sha256=deadbeef"})
        assert False, "expected EventRejected"
    except EventRejected:
        pass
    assert inbox.count() == 0

    # good signature → appended as immutable raw evidence
    raw = inbox.ingest("stripe", "payout.created", "evt_1", body, headers={"x-signature": good_sig})
    assert raw is not None and raw.body_digest and inbox.count("stripe") == 1

    # a replayed webhook (same event id) → deduped, not appended again (effectively-once)
    dup = inbox.ingest("stripe", "payout.created", "evt_1", body, headers={"x-signature": good_sig})
    assert dup is None and inbox.count() == 1


def test_verify_hmac_helper():
    body = b"payload"
    assert verify_hmac("s", body, hmac_signature("s", body))
    assert not verify_hmac("s", body, "nope")


# ── cursors + poll reconciliation ────────────────────────────────────────────────────────────────────────
def test_cursor_lag_and_lagging_list():
    cur = CursorStore()
    cur.advance("stripe", "cursor_100", at=1000.0, delta_events=3)
    assert cur.get("stripe").events_seen == 3
    assert cur.lag_seconds("stripe", now=1120.0) == 120.0
    assert "stripe" in cur.lagging(60.0, now=1120.0) and cur.lagging(600.0, now=1120.0) == []
    assert cur.lag_seconds("never_synced") == float("inf")


def test_detect_missed_events_finds_lost_webhooks():
    source = {"po_1", "po_2", "po_3"}          # what the provider actually has
    ingested = {"po_1", "po_3"}                # what the inbox saw (po_2's webhook was lost)
    assert detect_missed_events(source, ingested) == {"po_2"}


# ── the Phase-1 exit: silent failure → DLQ → replay → satisfied ──────────────────────────────────────────
def _normalizer(raw):
    return NormalizedEvent(provider="stripe", trigger="stripe.payout.created", object_type="journal_entry",
                           external_id="je_evt", raw_event_id=raw.event_id)


def test_durable_loop_dropped_write_detected_then_replayed_to_satisfaction():
    inbox = EventInbox(secrets={"stripe": "whsec"})
    dlq = ExceptionStore()
    accounting = InMemoryIntegrationProvider("accounting", drop_writes=True)   # silent failure

    body = b'{"id":"evt_9","type":"payout.created"}'
    raw = inbox.ingest("stripe", "payout.created", "evt_9", body,
                       headers={"x-signature": "sha256=" + hmac_signature("whsec", body)})
    ev = inbox.normalize(raw, _normalizer)
    assert ev.trigger == "stripe.payout.created"

    obl = Obligation(trigger=ev.trigger, destination_resource="accounting", workflow_id="payout-recon",
                     retry_policy=RetryPolicy(max_attempts=1),
                     expected_state={"journal_entry": {"payout": "po_evt", "balanced": True}})

    def _post():
        accounting.create_object("journal_entry", {"id": "je_evt", "payout": "po_evt", "balanced": True},
                                 idempotency_key="je_evt")
        return ActionResult(ok=True)

    res = ObligationEngine().discharge(obl, accounting, action=_post, targets={"journal_entry": "je_evt"})
    assert not res.satisfied                                   # the write 200'd but nothing landed
    dlq.append(res.exception)
    assert dlq.ranked()[0].category == ExceptionCategory.OBLIGATION_UNSATISFIED

    # the connector/outage is fixed → writes land again → replay from the failed obligation
    accounting._drop = False

    def _replay(exc: IntegrationException):
        return ObligationEngine().discharge(obl, accounting, action=_post, targets={"journal_entry": "je_evt"})

    replayed = dlq.replay(dlq.ranked()[0].exception_id, _replay)
    assert replayed.satisfied and replayed.obligation.status == ObligationStatus.SATISFIED
    assert dlq.open() == []                                    # the exception is resolved after a clean replay
    assert accounting.read_object("journal_entry", "je_evt") is not None


def test_exception_ranking_money_first():
    dlq = ExceptionStore()
    dlq.append(IntegrationException(category=ExceptionCategory.IDENTITY_CONFLICT))
    dlq.append(IntegrationException(category=ExceptionCategory.RECONCILIATION_VARIANCE, business_impact="$1,370"))
    dlq.append(IntegrationException(category=ExceptionCategory.ACTION_FAILED))
    order = [e.category for e in dlq.ranked()]
    assert order[0] == ExceptionCategory.RECONCILIATION_VARIANCE   # money/variance surfaces first
    assert priority_score(dlq.ranked()[0]) > priority_score(dlq.ranked()[-1])
