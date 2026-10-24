"""Lago reconciliation probes: what counts as proof, what never does, and the supervisor resolving a real unknown outcome with them."""
from __future__ import annotations

import time

import pytest

from agentic_os.mission.probe_adapters.lago import LagoProbes, QUALIFICATION, SKEW_S, _cents, register_lago, subscription_external_id
from agentic_os.mission.probes import ProbeContext, ProbeOutcome as O, ProbeRegistry

NOW = 1_800_000_000.0
DISPATCHED = NOW - 600.0


def iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Fake:
    """A scripted Lago: (path, status-param or invoice) -> (status, body, headers). Records every request: probes must only ever GET."""

    def __init__(self, routes: dict):
        self.routes, self.requests = routes, []

    def __call__(self, path, params):
        self.requests.append((path, dict(params)))
        key = (path, params.get("status") or params.get("invoice_number") or "")
        hit = self.routes.get(key) or self.routes.get(path)
        if isinstance(hit, Exception):
            raise hit
        return hit or (404, {"error": "not found"}, {})


def ctx(ref, dispatched=DISPATCHED, now=NOW, attempt=1):
    return ProbeContext(lease={"meta": {}}, attempt=attempt, now=now, dispatched_at=dispatched, effect_ref=ref)


def sub(ext, status="active", created=NOW - 100, **kw):
    return {"subscription": {"lago_id": "sub-uuid-1", "external_id": ext, "status": status, "created_at": iso(created), **kw}}


EXT = subscription_external_id("acme-co", "pro")
SUBREF = {"customer": "acme-co", "plan_code": "pro"}


# ───────────────────────── subscriptions ─────────────────────────
def test_an_active_subscription_is_positive_evidence_with_the_provider_record():
    p = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "active"): (200, sub(EXT), {})}))
    r = p.create_subscription(ctx(SUBREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.provider_ref == "sub-uuid-1"
    assert r.evidence["kind"] == "provider_record" and EXT in r.evidence["reference"]
    assert r.result["subscription"] is True and r.result["reconciled"] is True and r.result["external_id"] == EXT


def test_a_pending_subscription_counts_and_one_that_pre_dated_the_call_is_flagged_not_hidden():
    p = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "pending"): (200, sub(EXT, "pending", created=DISPATCHED - 10 * 86400), {})}))
    r = p.create_subscription(ctx(SUBREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and "created before this call" in r.detail


def test_no_subscription_is_inconclusive_never_a_negative():
    p = LagoProbes(Fake({}))
    r = p.create_subscription(ctx(SUBREF))
    assert r.outcome is O.INCONCLUSIVE and "not proof" in r.detail


def test_a_record_for_a_different_external_id_is_not_evidence():
    p = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "active"): (200, sub("sub-someone-else"), {})}))
    assert p.create_subscription(ctx(SUBREF)).outcome is O.INCONCLUSIVE


def test_the_cancel_is_confirmed_only_by_a_terminated_record_and_active_is_inconclusive():
    ok = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "terminated"): (200, sub(EXT, "terminated", terminated_at=iso(NOW - 50)), {})}))
    r = ok.cancel_subscription(ctx(SUBREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.result["cancelled"] is True
    still = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "active"): (200, sub(EXT), {})}))
    assert still.cancel_subscription(ctx(SUBREF)).outcome is O.INCONCLUSIVE                # it asks for `terminated`; an active one proves nothing
    assert still.http.requests[0][1]["status"] == "terminated"


# ───────────────────────── refunds ─────────────────────────
def notes(*rows):
    return {"credit_notes": list(rows)}


def cn(lago_id, cents, created=NOW - 300, status="succeeded"):
    return {"lago_id": lago_id, "number": f"CN-{lago_id}", "refund_amount_cents": cents, "created_at": iso(created), "refund_status": status}


REFREF = {"invoice_number": "INV-1001", "amount": "10.00"}


def test_exactly_one_matching_refund_since_the_call_is_positive_but_described_as_consistent_not_identified():
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(cn("a", 1000)), {})}))
    r = p.refund(ctx(REFREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.result["refunded_cents"] == 1000 and r.provider_ref == "a"
    assert "consistent with the request, not identified by it" in r.detail


def test_two_matching_refunds_are_inconclusive_because_one_may_be_a_duplicate():
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(cn("a", 1000), cn("b", 1000)), {})}))
    r = p.refund(ctx(REFREF))
    assert r.outcome is O.INCONCLUSIVE and "duplicate" in r.detail


@pytest.mark.parametrize("rows,why", [([], "no credit notes"), ([cn("old", 1000, created=DISPATCHED - 86400)], "a refund from before the call"),
                                      ([cn("other", 500)], "a different amount")])
def test_no_matching_refund_is_inconclusive_never_a_negative(rows, why):
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(*rows), {})}))
    assert p.refund(ctx(REFREF)).outcome is O.INCONCLUSIVE, why


def test_a_refund_from_just_before_dispatch_inside_the_clock_skew_still_counts():
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(cn("a", 1000, created=DISPATCHED - SKEW_S + 5)), {})}))
    assert p.refund(ctx(REFREF)).outcome is O.CONFIRMED_EXECUTED


def test_a_full_refund_matches_any_single_new_refund_credit_note():
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(cn("a", 4321)), {})}))
    assert p.refund(ctx({"invoice_number": "INV-1001", "full_refund": True})).outcome is O.CONFIRMED_EXECUTED


@pytest.mark.parametrize("amount,cents", [("10.00", 1000), ("$10", 1000), ("1,000.50", 100050), (1000, 1000), (5, 5), (10.5, None), (True, None), (None, None),
                                          ("abc", None), ("0", None), (-5, None), ("0.005", None)])
def test_the_amount_is_read_the_way_billing_reads_it(amount, cents):
    assert _cents(amount) == cents


def test_the_probe_queries_the_invoice_and_refund_type_only():
    p = LagoProbes(Fake({("/api/v1/credit_notes", "INV-1001"): (200, notes(), {})}))
    p.refund(ctx(REFREF))
    path, params = p.http.requests[0]
    assert path == "/api/v1/credit_notes" and params["invoice_number"] == "INV-1001" and params["types[]"] == "refund"


# ───────────────────────── the rest of the contract ─────────────────────────
@pytest.mark.parametrize("status,headers,outcome", [(429, {"Retry-After": "7"}, O.TRANSIENT_ERROR), (503, {}, O.TRANSIENT_ERROR), (500, {}, O.TRANSIENT_ERROR),
                                                    (401, {}, O.UNSUPPORTED), (403, {}, O.UNSUPPORTED)])
def test_provider_errors_map_to_the_right_outcome_and_honour_retry_after(status, headers, outcome):
    p = LagoProbes(Fake({f"/api/v1/subscriptions/{EXT}": (status, None, headers), "/api/v1/credit_notes": (status, None, headers)}))
    for r in (p.create_subscription(ctx(SUBREF)), p.cancel_subscription(ctx(SUBREF)), p.refund(ctx(REFREF))):
        assert r.outcome is outcome
    assert p.refund(ctx(REFREF)).retry_after_s == (7.0 if status == 429 else None)


def test_a_lookup_that_raises_is_a_transient_error_not_a_negative():
    p = LagoProbes(Fake({f"/api/v1/subscriptions/{EXT}": TimeoutError("read timed out")}))
    r = p.create_subscription(ctx(SUBREF))
    assert r.outcome is O.TRANSIENT_ERROR and "TimeoutError" in r.detail


@pytest.mark.parametrize("probe,ref", [("create_subscription", {}), ("cancel_subscription", {"customer": "x"}), ("refund", {}), ("refund", {"amount": "1"})])
def test_a_reservation_without_the_identifying_fields_cannot_be_probed(probe, ref):
    p = LagoProbes(Fake({}))
    assert getattr(p, probe)(ctx(ref)).outcome is O.UNSUPPORTED and p.http.requests == []


def test_a_payment_retry_is_unsupported_it_leaves_no_record():
    assert LagoProbes(Fake({})).retry_payment(ctx({})).outcome is O.UNSUPPORTED


def test_probes_only_ever_read():
    http = Fake({(f"/api/v1/subscriptions/{EXT}", "active"): (200, sub(EXT), {}), ("/api/v1/credit_notes", "INV-1001"): (200, notes(), {})})
    p = LagoProbes(http)
    p.create_subscription(ctx(SUBREF))
    p.cancel_subscription(ctx(SUBREF))
    p.refund(ctx(REFREF))
    assert all(len(req) == 2 and isinstance(req[1], dict) for req in http.requests)         # the transport takes (path, params): there is no method or body to write with


def test_no_lago_probe_can_return_an_authoritative_negative_and_the_registry_downgrades_one_that_tries():
    from agentic_os.mission.probes import ProbeResult
    assert QUALIFICATION.authoritative_negative is False
    reg = ProbeRegistry()
    register_lago(reg, Fake({}))
    v = reg.judge(QUALIFICATION, ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence={"kind": "provider_record", "reference": "searched, nothing found"},
                                             authoritative_negative=True), dispatched_at=DISPATCHED, now=NOW)
    assert v.outcome is O.INCONCLUSIVE and v.downgraded


def test_registration_declares_the_identifying_fields_and_marks_dunning_as_unsupported():
    reg = ProbeRegistry()
    register_lago(reg, Fake({}))
    assert reg.ref_keys("billing.create_subscription") == ("customer", "plan_code")
    assert reg.ref_keys("billing.refund") == ("invoice_number", "amount", "full_refund", "currency")
    assert reg.supported("billing.refund") and not reg.supported("billing.dunning")
    assert reg.effect_ref("billing.refund", {"invoice_number": "INV-1", "amount": "5", "api_token": "x", "note": "unrelated", "currency": "USD"}) == {
        "invoice_number": "INV-1", "amount": "5", "currency": "USD"}                          # only declared fields, never anything that looks like a secret


# ───────────────────────── effect_ref plumbing: dispatch records it, the supervisor hands it to the probe ─────────────────────────
def test_the_gateway_records_the_effect_ref_in_the_reservation_and_never_a_secret():
    from tests.test_gateway_unknown_outcome import refund, wire
    prov, gw, guard, gp = wire()
    reg = ProbeRegistry()
    reg.register("billing.refund", lambda c: None, QUALIFICATION, ref_keys=("invoice", "amount"))
    gw.probe_refs = reg
    prov.fail_next = True
    refund(gw, gp, api_token="s3cret")
    (lease,) = gw.unresolved_calls("acme:agent")
    assert lease["meta"]["effect_ref"] == {"invoice": "INV-1", "amount": 40}
    assert "s3cret" not in repr(lease)


def test_without_a_registry_nothing_is_recorded():
    from tests.test_gateway_unknown_outcome import refund, wire
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp)
    (lease,) = gw.unresolved_calls("acme:agent")
    assert "effect_ref" not in lease["meta"]


def test_a_runtime_dispatch_records_the_effect_ref_and_the_supervisor_gives_it_to_the_probe(tmp_path, monkeypatch):
    from tests.test_reconciliation_supervisor import Q_STRICT, build, tick
    from agentic_os.mission.runtime import MissionRuntime
    seen: list = []
    reg = ProbeRegistry()
    reg.register("*", lambda c: None, Q_STRICT)
    monkeypatch.setattr(reg, "effect_ref", lambda cap, inputs: {"invoice_number": "INV-9"}, raising=True)       # what a declared adapter would extract
    monkeypatch.setattr(MissionRuntime, "probe_refs", reg, raising=False)
    from agentic_os.mission.probes import ProbeResult
    def probe(ctx):
        seen.append(ctx.effect_ref)
        return ProbeResult(O.INCONCLUSIVE, detail="x")
    x = build(tmp_path, monkeypatch, probes=[("*", probe, Q_STRICT)])
    tick(x, 10_000)
    assert seen, "the probe was never called"
    assert all(r == {"invoice_number": "INV-9"} for r in seen)                          # recorded at dispatch, read back by the probe


def test_a_terminated_record_for_another_external_id_does_not_confirm_a_cancel():
    p = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "terminated"): (200, sub("sub-other", "terminated"), {})}))
    assert p.cancel_subscription(ctx(SUBREF)).outcome is O.INCONCLUSIVE


def test_a_subscription_created_minutes_before_the_call_is_flagged_as_possibly_pre_existing():
    p = LagoProbes(Fake({(f"/api/v1/subscriptions/{EXT}", "active"): (200, sub(EXT, created=DISPATCHED - 600), {})}))
    r = p.create_subscription(ctx(SUBREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and "created before this call" in r.detail
