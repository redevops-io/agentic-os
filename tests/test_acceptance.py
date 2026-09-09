"""The cross-system acceptance harness — the P0 gate. Driven with fake adapters/sources
(no live provider), it proves the whole composition: actions + evidence + a governed
approval gate + verification + a Projects projection, and that a missing provider ABSTAINS
rather than fails. The money leg is exercised in all three gate states."""
from __future__ import annotations

from agentic_os.integrations.acceptance import LegStatus, run_acceptance


class FakeResult:
    def __init__(self, ok=True, oid="obj_1", error=""):
        self.ok = ok
        self.provider_object_id = oid
        self.error = error


class FakeObs:
    def __init__(self, found=True):
        self.found = found


class FakeAdapter:
    """An AdapterPort double: records writes and requires an envelope for them."""
    def __init__(self, provider, ok=True):
        self.provider = provider
        self.ok = ok
        self.calls = []

    def execute(self, capability, request, envelope):
        self.calls.append((capability, dict(request), envelope is not None))
        return FakeResult(ok=self.ok, oid=f"{self.provider}:{capability}")

    def observe(self, ref):
        return FakeObs(found=True)


class FakeEvidence:
    def __init__(self, provider, refs, record_count=None, observed_at=""):
        self.provider = provider
        self.refs = refs
        self.record_count = record_count
        self.observed_at = observed_at


def _all_actions():
    return {p: FakeAdapter(p) for p in ("whatsapp_business", "hubspot", "polar", "slack")}


def _all_evidence():
    return {
        "db": FakeEvidence("postgres", [{"ref": "postgres:customers#0", "summary": "id=8821"}],
                           record_count=3, observed_at="2026-09-09T11:31:00Z"),
        "policy": FakeEvidence("google_drive", [{"ref": "gdrive:f1", "summary": "Refund Policy.pdf"}]),
    }


ENV = object()


def test_gate_halts_without_approval_and_no_money_moves():
    rep = run_acceptance(actions=_all_actions(), evidence=_all_evidence(), approved=None, envelope=ENV)
    refund = next(l for l in rep.legs if l.capability == "billing.refund.execute")
    assert refund.status is LegStatus.AWAITING_APPROVAL   # halted at the human gate
    assert rep.gate_enforced and rep.passed                # composition holds; nothing refused
    # evidence was actually gathered (query + file)
    assert any(l.status is LegStatus.RETRIEVED and "records retrieved" in l.detail for l in rep.legs)


def test_approved_but_safe_mode_withholds_the_refund():
    rep = run_acceptance(actions=_all_actions(), evidence=_all_evidence(), approved=True, envelope=ENV,
                         execute_writes=False)
    refund = next(l for l in rep.legs if l.capability == "billing.refund.execute")
    assert refund.status is LegStatus.WITHHELD             # approved, but money withheld by default
    assert rep.passed and rep.gate_enforced


def test_approved_sandbox_executes_and_verifies_under_an_envelope():
    actions = _all_actions()
    rep = run_acceptance(actions=actions, evidence=_all_evidence(), approved=True, envelope=ENV,
                         execute_writes=True)
    refund = next(l for l in rep.legs if l.capability == "billing.refund.execute")
    verify = next(l for l in rep.legs if l.name == "Verify refund")
    assert refund.status is LegStatus.EXECUTED and verify.status is LegStatus.VERIFIED
    assert rep.passed
    # the refund write went through WITH the governed envelope
    polar_writes = [c for c in actions["polar"].calls if c[0] == "billing.refund.execute"]
    assert polar_writes and polar_writes[0][2] is True     # envelope present on the write


def test_rejection_moves_no_money_and_is_not_a_failure():
    rep = run_acceptance(actions=_all_actions(), evidence=_all_evidence(), approved=False, envelope=ENV,
                         execute_writes=True)
    refund = next(l for l in rep.legs if l.capability == "billing.refund.execute")
    assert refund.status is LegStatus.REJECTED and rep.passed  # a clean "no", not REFUSED


def test_missing_providers_abstain_and_the_mission_still_runs_its_connected_legs():
    # Only Slack (action) + the DB (evidence) are connected — the live state today.
    rep = run_acceptance(actions={"slack": FakeAdapter("slack")},
                         evidence={"db": _all_evidence()["db"]}, approved=None, envelope=ENV)
    status = {l.name: l.status for l in rep.legs}
    assert status["Intake WhatsApp message"] is LegStatus.ABSTAINED   # WhatsApp not connected
    assert status["Refund policy (Drive)"] is LegStatus.ABSTAINED     # Drive not connected
    assert status["Request approval"] is LegStatus.EXECUTED           # Slack ran
    assert any(l.status is LegStatus.RETRIEVED for l in rep.legs)     # Postgres evidence retrieved
    assert rep.passed                                                 # abstain is not failure


def test_projection_shape_for_projects():
    rep = run_acceptance(actions=_all_actions(), evidence=_all_evidence(), approved=True, envelope=ENV,
                         execute_writes=True)
    proj = rep.to_projection()
    assert proj["verdict"]["passed"] is True
    assert proj["verdict"]["evidence_count"] >= 3          # the 3 retrieved DB records
    assert any(s["capability"] == "billing.refund.execute" for s in proj["steps"])
    assert proj["context_used"]  # evidence legs surface as context used


def test_a_live_adapter_failure_is_refused_and_fails_the_gate():
    actions = _all_actions()
    actions["slack"] = FakeAdapter("slack", ok=False)     # approval request fails
    rep = run_acceptance(actions=actions, evidence=_all_evidence(), approved=None, envelope=ENV)
    assert any(l.status is LegStatus.REFUSED for l in rep.legs)
    assert rep.passed is False                             # a real failure fails the P0 gate
