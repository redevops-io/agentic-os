"""Email delivery adapters + governed bridge (moat plan §3.11, §6 P1). All offline via injected fetch."""
from __future__ import annotations

from runtime_contracts.protocol import EmailDeliveryStatus, EmailMessageRef, EmailSendRequest, EmailSubmitFailure

from agentic_os.intelligence.email import (
    PostmarkProvider, ReceiptStore, SesProvider, SuppressionList, send_email,
)


def _fetch(status, body):
    return lambda method, url, headers=None, body_=None: (status, body)


def _req(key="k1", recipient="a@example.com", tenant="t", cost=0.0):
    return EmailSendRequest(
        tenant=tenant, campaign_id="c1", idempotency_key=key, max_cost=cost,
        message=EmailMessageRef(recipient=recipient, sender_domain="mg.acme.com",
                                subject_ref="Hi", body_ref="<p>hi</p>", stream="broadcast"),
        metadata=(("list_id", "42"),))


# ── adapters ──────────────────────────────────────────────────────────────────────────────────────────────
def test_postmark_accepts_on_errorcode_zero():
    p = PostmarkProvider(credential="tok", fetch=_fetch(200, {"ErrorCode": 0, "MessageID": "pm-1"}))
    r = p.send(_req())
    assert r.status == EmailDeliveryStatus.ACCEPTED and r.provider_message_id == "pm-1"
    assert r.cost == p._price and r.has_provenance()


def test_postmark_inactive_recipient_is_dropped():
    p = PostmarkProvider(credential="tok", fetch=_fetch(200, {"ErrorCode": 406, "Message": "inactive"}))
    r = p.send(_req())
    assert r.status == EmailDeliveryStatus.DROPPED and r.should_suppress()


def test_postmark_is_byo():
    p = PostmarkProvider()   # no credential
    r = p.send(_req())
    assert r.status == EmailDeliveryStatus.FAILED and r.submit_failure == EmailSubmitFailure.NOT_ENTITLED
    assert not p.check_entitlement("t")


def test_http_429_maps_to_rate_limited():
    p = PostmarkProvider(credential="tok", fetch=_fetch(429, {"Message": "too many"}))
    r = p.send(_req())
    assert r.status == EmailDeliveryStatus.FAILED and r.submit_failure == EmailSubmitFailure.RATE_LIMITED


def test_ses_accepts_on_message_id():
    p = SesProvider(credential="tok", fetch=_fetch(200, {"MessageId": "ses-9"}))
    r = p.send(_req())
    assert r.status == EmailDeliveryStatus.ACCEPTED and r.provider_message_id == "ses-9"


def test_max_cost_ceiling_blocks_send():
    p = PostmarkProvider(credential="tok", fetch=_fetch(200, {"ErrorCode": 0, "MessageID": "x"}))
    r = p.send(_req(cost=0.0001))   # below the postmark price
    assert r.status == EmailDeliveryStatus.FAILED and r.submit_failure == EmailSubmitFailure.QUOTA_EXCEEDED


# ── governed bridge ───────────────────────────────────────────────────────────────────────────────────────
def test_bridge_records_receipt_and_updates_suppression(tmp_path):
    receipts = ReceiptStore(str(tmp_path / "receipts.jsonl"))
    supp = SuppressionList(str(tmp_path / "supp.jsonl"))
    p = PostmarkProvider(credential="tok", fetch=_fetch(200, {"ErrorCode": 406, "Message": "inactive"}))
    r = send_email(p, _req(recipient="bad@x.com"), receipts=receipts, suppression=supp)
    assert r.status == EmailDeliveryStatus.DROPPED
    assert supp.contains("t", "bad@x.com")                     # governed side effect recorded
    assert receipts.summary()["postmark"]["sends"] == 1


def test_bridge_refuses_suppressed_recipient_without_calling_provider(tmp_path):
    receipts = ReceiptStore(str(tmp_path / "r.jsonl"))
    supp = SuppressionList(str(tmp_path / "s.jsonl"))
    supp.add("t", "blocked@x.com", reason="bounced")

    def _boom(*a, **k):
        raise AssertionError("provider must not be called for a suppressed recipient")
    p = PostmarkProvider(credential="tok", fetch=_boom)
    r = send_email(p, _req(recipient="blocked@x.com"), receipts=receipts, suppression=supp)
    assert r.status == EmailDeliveryStatus.DROPPED and r.submit_failure == EmailSubmitFailure.SUPPRESSED


def test_bridge_idempotency_does_not_double_send(tmp_path):
    receipts = ReceiptStore(str(tmp_path / "r.jsonl"))
    calls = {"n": 0}

    def _count(method, url, headers=None, body=None):
        calls["n"] += 1
        return 200, {"ErrorCode": 0, "MessageID": "pm-1"}
    p = PostmarkProvider(credential="tok", fetch=_count)
    first = send_email(p, _req(key="dup"), receipts=receipts)
    second = send_email(p, _req(key="dup"), receipts=receipts)
    assert first.status == EmailDeliveryStatus.ACCEPTED and second.status == EmailDeliveryStatus.ACCEPTED
    assert calls["n"] == 1                                       # provider hit once
    assert second.cost == 0.0 and "replay" in second.reason


def test_receipt_summary_tallies_by_provider(tmp_path):
    receipts = ReceiptStore(str(tmp_path / "r.jsonl"))
    p = PostmarkProvider(credential="tok", fetch=_fetch(200, {"ErrorCode": 0, "MessageID": "pm"}))
    for i in range(3):
        send_email(p, _req(key=f"k{i}", recipient=f"u{i}@x.com"), receipts=receipts)
    s = receipts.summary()["postmark"]
    assert s["sends"] == 3 and s["accepted"] == 3
