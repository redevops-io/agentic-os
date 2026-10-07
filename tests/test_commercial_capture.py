"""Unified Capture Gateway — capture → activity → review-by-exception CRM projection (plan §21/§6/§30).

Proves: change kinds classify to the right policy (auto-safe vs review vs approval vs manual); the gateway runs
one extractor and emits a policy-classified CRMProjection whose auto-safe/needs-review split is correct; evidence
unions; and polling providers ingests + acknowledges. Channel-neutral: a Slack and an email event flow the SAME
pipeline.
"""
from __future__ import annotations

from agentic_os.commercial import (
    CaptureChannel, CaptureEvent, CaptureExtraction, CaptureGateway, ChangeProposal, CommercialActivity,
    ProjectionPolicy, classify_change,
)


def test_change_classification_review_by_exception():
    assert classify_change("log_activity") is ProjectionPolicy.AUTO_SAFE
    assert classify_change("suggest_stage_transition") is ProjectionPolicy.REVIEW_RECOMMENDED
    assert classify_change("send_quote") is ProjectionPolicy.APPROVAL_REQUIRED
    assert classify_change("write_off") is ProjectionPolicy.MANUAL_ONLY
    assert classify_change("something_new") is ProjectionPolicy.REVIEW_RECOMMENDED   # conservative default


def _extractor(ev: CaptureEvent) -> CaptureExtraction:
    act = CommercialActivity(activity_id="act_" + ev.event_id, channel=ev.channel, customer="acme",
                             opportunity="opportunity:acme", occurred_at=ev.occurred_at,
                             evidence=(ev.raw_content_ref,), summary="met acme")
    changes = (
        ChangeProposal("opportunity:acme", "last_contacted", ev.occurred_at, "update_last_contacted",
                       evidence=(ev.raw_content_ref,)),
        ChangeProposal("opportunity:acme", "stage", "negotiation", "suggest_stage_transition",
                       evidence=(ev.raw_content_ref,)),
        ChangeProposal("opportunity:acme", "quote", "Q-123", "send_quote", evidence=(ev.raw_content_ref,)),
    )
    return CaptureExtraction(activity=act, changes=changes, record="opportunity:acme")


def test_gateway_ingest_classifies_and_splits():
    gw = CaptureGateway(extractor=_extractor)
    ev = CaptureEvent(event_id="e1", channel=CaptureChannel.EMAIL, raw_content_ref="ev:thread/1",
                      occurred_at="2026-10-07")
    activity, proj = gw.ingest(ev)
    assert activity.customer == "acme"
    assert proj.record == "opportunity:acme"
    # policies re-stamped by the gateway classifier
    kinds = {c.field: c.policy for c in proj.proposed_changes}
    assert kinds["last_contacted"] is ProjectionPolicy.AUTO_SAFE
    assert kinds["stage"] is ProjectionPolicy.REVIEW_RECOMMENDED
    assert kinds["quote"] is ProjectionPolicy.APPROVAL_REQUIRED
    # review-by-exception split
    assert {c.field for c in proj.auto_safe} == {"last_contacted"}
    assert {c.field for c in proj.needs_review} == {"stage", "quote"}
    assert "ev:thread/1" in proj.evidence


def test_channel_neutral_same_pipeline():
    gw = CaptureGateway(extractor=_extractor)
    for ch in (CaptureChannel.SLACK, CaptureChannel.EMAIL, CaptureChannel.CHATWOOT):
        _, proj = gw.ingest(CaptureEvent(event_id="x", channel=ch, raw_content_ref="r"))
        assert len(proj.proposed_changes) == 3   # identical business logic regardless of channel


def test_poll_all_ingests_and_acknowledges():
    acked = []

    class _Provider:
        channel = CaptureChannel.CHATWOOT
        def __init__(self): self._events = [CaptureEvent(event_id="p1", channel=self.channel, raw_content_ref="r")]
        def poll(self): return list(self._events)
        def acknowledge(self, event_id): acked.append(event_id)

    gw = CaptureGateway(extractor=_extractor)
    gw.register(_Provider())
    results = gw.poll_all()
    assert len(results) == 1 and acked == ["p1"]
    _, proj = results[0]
    assert proj.record == "opportunity:acme"
