"""Outreach acceptance Mission — the synthesis→configure→enroll→boundary→resume→verify case,
driven with a fake provider (no live Apollo). Proves: optional creative asset, the physical
activation boundary (provider-UI vs governance), durable pause→resume→verify, and receipts."""
from __future__ import annotations

from agentic_os.integrations.outreach import (
    ActivationCapability,
    Asset,
    Copy,
    OutreachRequest,
    StepStatus,
    ACTIVATE_SEQUENCE,
    GENERATE_ASSET,
    VERIFY_DELIVERY,
    resume_outreach,
    run_outreach,
)


class FakeResult:
    def __init__(self, ok=True, oid="x", error=""):
        self.ok = ok
        self.provider_object_id = oid
        self.error = error


class FakeObs:
    def __init__(self, status="delivered"):
        self.status = status


class FakeProvider:
    provider = "apollo"

    def __init__(self, *, automatable=True, human_required=False, strategy="api",
                 observe=("delivered",), activate_ok=True):
        self._act = ActivationCapability(automatable, human_required, strategy,
                                         "activation is UI-only" if strategy == "provider_ui" else "")
        self._observe = list(observe)
        self._activate_ok = activate_ok
        self.activated = False
        self.configured = None
        self.enrolled = None

    def configure_sequence(self, *, subject, body_html):
        self.configured = (subject, body_html)
        return FakeResult(oid="step_1")

    def enroll(self, *, recipient):
        self.enrolled = recipient
        return FakeResult(oid="contact_1")

    def activation(self):
        return self._act

    def activate(self):
        self.activated = True
        return FakeResult(ok=self._activate_ok)

    def observe(self, *, recipient):
        st = self._observe.pop(0) if len(self._observe) > 1 else self._observe[0]
        return FakeObs(status=st)


REQ = OutreachRequest(goal="promote the agentic apps stack", recipient="tasha@nutrients.tech",
                      company="Nutrients.tech", campaign_intent="cold outreach")
COPY = lambda _r: Copy(subject="[test] one governed system", body_html="<p>hi</p>")
ASSET = lambda _r: Asset(kind="image", ref="/tmp/hero.jpg", summary="conceptual hero")


def _step(rep, key):
    return next(s for s in rep.steps if s.key == key)


def test_full_auto_activation_verifies_with_a_receipt():
    p = FakeProvider(automatable=True, observe=("delivered",))
    rep = run_outreach(REQ, provider=p, generate_copy=COPY, governance="allow")
    assert p.activated is True and rep.verified and rep.passed
    assert rep.receipt and rep.receipt.object_id == "contact_1" and rep.receipt.provider == "apollo"


def test_creative_asset_is_optional():
    # not requested → the asset step is SKIPPED, copy still required and present
    rep = run_outreach(REQ, provider=FakeProvider(), generate_copy=COPY)
    assert _step(rep, GENERATE_ASSET).status is StepStatus.SKIPPED
    # requested → generated
    req2 = OutreachRequest(goal="x", recipient="t@x.co", creative_asset=True)
    rep2 = run_outreach(req2, provider=FakeProvider(), generate_copy=COPY, generate_asset=ASSET)
    assert _step(rep2, GENERATE_ASSET).status is StepStatus.DONE and "hero.jpg" in _step(rep2, GENERATE_ASSET).refs[0]


def test_copy_is_required():
    rep = run_outreach(REQ, provider=FakeProvider(), generate_copy=lambda _r: None)
    assert not rep.passed and any(s.status is StepStatus.REFUSED for s in rep.steps)
    assert p_configured(rep) is False  # never reached the provider


def p_configured(rep):
    from agentic_os.integrations.outreach import CONFIGURE_SEQUENCE
    return any(s.key == CONFIGURE_SEQUENCE for s in rep.steps)


def test_provider_ui_activation_pauses_then_resume_verifies():
    # Apollo-shape: activation is not automatable → durable pause at the boundary.
    p = FakeProvider(automatable=False, human_required=True, strategy="provider_ui", observe=("delivered",))
    rep = run_outreach(REQ, provider=p, generate_copy=COPY)
    act = _step(rep, ACTIVATE_SEQUENCE)
    assert rep.pending and rep.pending_reason == "provider_ui"
    assert act.status is StepStatus.PENDING_HUMAN and act.automated is False
    assert p.activated is False  # the Runtime did NOT try to activate — capability said it can't
    assert rep.passed  # pausing at the boundary is a correct outcome, not a failure

    # the human toggles it on in the provider UI → resume observes + verifies
    done = resume_outreach(rep, provider=p)
    assert done.verified and done.receipt and _step(done, ACTIVATE_SEQUENCE).automated is False


def test_governance_review_pauses_even_when_automatable():
    p = FakeProvider(automatable=True, observe=("delivered",))
    rep = run_outreach(REQ, provider=p, generate_copy=COPY, governance="require_review")
    assert rep.pending and rep.pending_reason == "governance" and p.activated is False
    # approval lets the Runtime activate via API, then verify
    done = resume_outreach(rep, provider=p, approved=True)
    assert p.activated is True and done.verified


def test_governance_rejection_sends_nothing():
    p = FakeProvider(automatable=True)
    rep = run_outreach(REQ, provider=p, generate_copy=COPY, governance="require_review")
    done = resume_outreach(rep, provider=p, approved=False)
    assert _step(done, ACTIVATE_SEQUENCE).status is StepStatus.REJECTED
    assert p.activated is False and not done.verified and done.passed


def test_durable_pause_resume_when_send_is_queued_then_delivered():
    # observe returns "active" (queued) first, then "delivered" on the next poll.
    p = FakeProvider(automatable=True, observe=("active", "delivered"))
    rep = run_outreach(REQ, provider=p, generate_copy=COPY)
    assert _step(rep, VERIFY_DELIVERY).status is StepStatus.OBSERVING  # not yet delivered
    assert rep.pending and rep.pending_reason == "awaiting_delivery"
    # resume (re-observe) → now delivered → verified
    done = resume_outreach(rep, provider=p)
    assert done.verified and done.receipt.status == "delivered"


def test_projection_shape():
    p = FakeProvider(automatable=False, strategy="provider_ui")
    proj = run_outreach(REQ, provider=p, generate_copy=COPY).to_projection()
    assert proj["pending"] and proj["pending_reason"] == "provider_ui"
    assert any(s["key"] == ACTIVATE_SEQUENCE and s["status"] == "pending_human" for s in proj["steps"])
    assert proj["verdict"]["passed"] is True
