"""billing closed loop: observe -> decide -> govern -> act -> verify -> learn, over a fake Lago transport,
an in-process MissionRuntime and an injected clock.

Run: PYTHONPATH=<repo>:<repo>/apps python -m pytest apps/billing/test_closed_loop.py
"""
from __future__ import annotations

import base64
import types

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit import BridgeOutcome
from agentic_os.intervention_record import record_human_action
from agentic_os.mission.types import MissionState
from agentic_os.observation import KnownAtQuality
from agentic_os.policy_promotion import STATIC_POLICY

from billing import core, decision, loop_api, observe
from billing.operator import build_billing_operator
from billing.producers import BillingSignals

DAY = 86400.0
NOW = 1_791_000_000.0     # 2026-10-03T03:20:00Z-ish; every date below is relative to this
AUTH = {"Authorization": "Basic " + base64.b64encode(b"op:secret").decode()}


def _day(offset_days: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + offset_days * DAY, timezone.utc).strftime("%Y-%m-%d")


def _inv(lago_id, number, ext, name, cents, *, pay="failed", overdue=True, due=-20):
    return {"lago_id": lago_id, "number": number, "status": "finalized", "payment_status": pay,
            "payment_overdue": overdue, "total_amount_cents": cents, "currency": "USD",
            "payment_due_date": _day(due), "issuing_date": _day(due - 30), "updated_at": _day(-1) + "T00:00:00Z",
            "customer": {"external_id": ext, "name": name, "lago_id": f"cus-{ext}"}}


class FakeLago:
    """A stateful fake Lago. ``settles``: lago_ids whose payment succeeds when retried."""

    def __init__(self):
        self.invoices = {i["lago_id"]: i for i in [
            _inv("a1", "INV-A1", "acme", "ACME Ltd", 250_000, due=-23),
            _inv("a2", "INV-A2", "acme", "ACME Ltd", 120_000, due=-15),
            _inv("a0", "INV-A0", "acme", "ACME Ltd", 80_000, pay="succeeded", overdue=False, due=-60),
            _inv("a9", "INV-A9", "acme", "ACME Ltd", 70_000, pay="succeeded", overdue=False, due=-90),
            _inv("b1", "INV-B1", "beta", "Beta GmbH", 90_000, due=-10),
        ]}
        self.settles = {"a1", "a2", "b1"}
        self.rejects: set = set()
        self.retries: list = []
        self.payment_requests: list | None = []

    # transport surface used by core
    def get(self, url, timeout=None, headers=None, params=None):
        return _Resp(200)                                           # /health

    def client(self, timeout=None):
        return _Client(self)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, lago):
        self.l = lago

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/v1/invoices"):
            return _Resp(200, {"invoices": list(self.l.invoices.values()), "meta": {"total_pages": 1}})
        if url.endswith("/api/v1/payment_requests"):
            if self.l.payment_requests is None:
                return _Resp(404)
            return _Resp(200, {"payment_requests": self.l.payment_requests, "meta": {"total_pages": 1}})
        if url.endswith("/api/v1/customers"):
            return _Resp(200, {"customers": [], "meta": {"total_pages": 1}})
        lid = url.rsplit("/", 1)[-1]
        if lid in self.l.invoices:
            return _Resp(200, {"invoice": self.l.invoices[lid]})
        return _Resp(404)

    def post(self, url, headers=None, json=None):
        if url.endswith("/retry_payment"):
            lid = url.split("/")[-2]
            self.l.retries.append(lid)
            if lid in self.l.rejects:
                return _Resp(422)
            if lid in self.l.settles:
                self.l.invoices[lid] = {**self.l.invoices[lid], "payment_status": "succeeded",
                                        "payment_overdue": False}
            return _Resp(200)
        return _Resp(200)


@pytest.fixture()
def lago(monkeypatch):
    fake = FakeLago()
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "LAGO_API_KEY", "k")
    monkeypatch.setattr(core, "VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(core, "_sleep", lambda s: None)
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=fake.get, Client=fake.client))
    return fake


@pytest.fixture()
def loop(lago):
    clock = {"now": NOW}
    lp = decision.build_loop(clock=lambda: clock["now"])
    return lp


def _signals(lp, account="acme"):
    return observe.observe_accounts(lp.ingestor, now=lp.clock())[account]


def _decide(lp, account="acme"):
    return decision.run_decision_cycle(_signals(lp, account), store=lp.interventions,
                                       policy=lp.policies.active, loop=lp)


# ── OBSERVE ─────────────────────────────────────────────────────────────────────────────────────────────
def test_observe_builds_real_signals_and_persists_bitemporal_observations(lago, loop):
    sigs = observe.observe_accounts(loop.ingestor, now=NOW)
    a = sigs["acme"]
    assert a.overdue_invoices == 2 and a.overdue_amount_cents == 370_000
    assert set(a.overdue_invoice_ids) == {"a1", "a2"}
    assert a.days_overdue == pytest.approx(23, abs=1.1)             # oldest overdue invoice
    assert (a.paid_invoices, a.failed_invoices) == (2, 2)           # real payment history
    assert a.last_dunning_at is None and a.observed_at == NOW
    assert sigs["beta"].overdue_invoices == 1
    obs = loop.observations.get(a.observation_ids[0])
    assert obs.known_at == NOW and obs.known_at_quality is KnownAtQuality.OBSERVED
    assert obs.valid_at <= obs.known_at and obs.subject == "acme"
    assert obs.payload["overdue_invoice_ids"] == list(a.overdue_invoice_ids)


def test_observe_documents_fallbacks_when_lago_lacks_fields(lago, loop):
    lago.payment_requests = None                                    # endpoint unavailable
    del lago.invoices["a1"]["payment_due_date"]
    a = _signals(loop)
    assert any("payment_requests unavailable" in f for f in a.fallbacks)
    assert any("payment_due_date missing" in f for f in a.fallbacks)


def test_last_dunning_comes_from_lago_payment_requests(lago, loop):
    lago.payment_requests = [{"created_at": _day(-2) + "T10:00:00Z", "customer": {"external_id": "acme"}}]
    assert _signals(loop).last_dunning_at is not None


# ── the whole chain ─────────────────────────────────────────────────────────────────────────────────────
def test_full_chain_happy_path(lago, loop):
    # DECIDE: dunning for ONE account, gated, parked; nothing executed
    cyc = _decide(loop)
    assert cyc.selection.action.action_kind == "dunning"
    assert cyc.bridge.outcome is BridgeOutcome.LAUNCHED_GATED
    assert cyc.inputs == {"account_id": "acme", "invoice_ids": ["a1", "a2"]}
    assert loop.runtime._missions[cyc.mission_id].state is MissionState.WAITING_HUMAN
    assert lago.retries == []
    rec = loop.interventions.all()[0]
    assert rec.opportunity_id == "billing:acme" and rec.candidate_id == "billing:acme:dunning"
    assert rec.mission_id == cyc.mission_id and "invoice:a1" in rec.evidence_refs
    assert cyc.opportunity.source_app == "billing"
    # GOVERN + ACT + VERIFY
    out = decision.approve_mission(loop, cyc.mission_id)
    assert out["state"] == "succeeded"
    assert sorted(lago.retries) == ["a1", "a2"]                      # ONLY acme's overdue invoices (not b1)
    d = out["dunning"]
    assert d["dunning_verified"] is True and d["settled_count"] == 2 and d["failed_count"] == 0
    assert all(r["retried"] and r["settled"] and r["payment_status_after"] == "succeeded" for r in d["results"])
    done = [r for r in decision.latest_records(loop.interventions)][0]
    assert done.executed_at == NOW and done.approved_at == NOW and done.execution_ref == cyc.mission_id
    # cooldown: a second decision inside 24h does not dun again
    again = _decide(loop)
    assert not any(c.action_kind == "dunning" for c in again.opportunity.candidate_actions)
    assert "cooldown:dunning<24h" in again.opportunity.evidence
    # LEARN: too early -> nothing; 8 days later the outcome is attributed and persisted
    assert decision.observe_outcomes(interventions=loop.interventions, outcomes=loop.outcomes,
                                     ingestor=loop.ingestor, now=NOW + 2 * DAY) == []
    lago.invoices["a2"] = {**lago.invoices["a2"], "payment_status": "failed"}   # a2's payment bounced later
    rows = decision.observe_outcomes(interventions=loop.interventions, outcomes=loop.outcomes,
                                     ingestor=loop.ingestor, now=NOW + 8 * DAY)
    assert len(rows) == 1 and rows[0]["outcome"] == "payment_collected"
    ev = loop.outcomes.load()[0]
    assert ev.source_app == "billing" and ev.action_kind == "dunning"
    assert ev.reward_dimensions["payment_collected"] == pytest.approx(250_000 / 370_000)
    assert ev.selected_intervention_id == rec.intervention_id and 0 < ev.attribution_confidence <= 1
    assert loop.interventions.links()[0].intervention_id == rec.intervention_id
    # idempotent: no second outcome for the same intervention
    assert decision.observe_outcomes(interventions=loop.interventions, outcomes=loop.outcomes,
                                     ingestor=loop.ingestor, now=NOW + 9 * DAY) == []
    # promotion: refused for lack of data, even with apply=True; the active policy is untouched
    rep = decision.refit_policy(loop.outcomes, loop.policies, apply=True)
    assert rep.promoted is False and rep.decision.promote is False
    assert any("insufficient held-out data" in r for r in rep.decision.reasons)
    assert loop.policies.active is STATIC_POLICY


def test_no_approval_executes_nothing(lago, loop):
    cyc = _decide(loop)
    assert loop.runtime._missions[cyc.mission_id].state is MissionState.WAITING_HUMAN
    assert lago.retries == []
    out = decision.approve_mission(loop, cyc.mission_id, decision="reject")
    assert lago.retries == [] and out["state"] != "succeeded"
    assert decision.latest_records(loop.interventions)[0].executed_at is None
    assert decision.observe_outcomes(interventions=loop.interventions, outcomes=loop.outcomes,
                                     ingestor=loop.ingestor, now=NOW + 30 * DAY) == []
    with pytest.raises(KeyError):
        decision.approve_mission(loop, "mission_unknown")


def test_abstains_on_weak_evidence(lago, loop):
    weak = BillingSignals(account="acme", overdue_invoices=1, days_overdue=5.0, overdue_amount_cents=1000,
                          overdue_invoice_ids=("a1",), paid_invoices=0, failed_invoices=6,
                          fallbacks=("payment_due_date missing", "payment_requests unavailable",
                                     "payment_overdue flag absent"))
    cyc = decision.run_decision_cycle(weak, store=loop.interventions, policy=loop.policies.active, loop=loop,
                                      now=NOW)
    assert cyc.selection.action.action_kind != "dunning"
    assert not any(c.required_capabilities == ("billing.dunning",) and c.confidence >= 0.55
                   for c in cyc.opportunity.candidate_actions)
    assert len(loop.interventions.all()) == 1                        # the abstention itself is recorded
    assert lago.retries == []


def test_partial_settlement_fails_the_node_and_is_reported_partial(lago, loop):
    lago.settles = {"a1"}                                            # a2 retried but does not settle
    cyc = _decide(loop)
    out = decision.approve_mission(loop, cyc.mission_id)
    d = out["dunning"]
    assert d["status"] == "partial" and d["dunning_verified"] is False
    assert (d["settled_count"], d["failed_count"]) == (1, 1)
    assert [r["settled"] for r in d["results"]] == [True, False]
    assert out["state"] == "failed"                                  # the kernel verifier rejected the node


def test_rejected_retry_or_nothing_settled_fails_the_node(lago, loop):
    lago.rejects = {"a1", "a2"}
    out = decision.approve_mission(loop, _decide(loop).mission_id)
    d = out["dunning"]
    assert d["dunning_attempted"] is False and d["settled_count"] == 0 and not d["results"][0]["retried"]
    assert out["state"] == "failed"
    assert decision.latest_records(loop.interventions)[0].executed_at is None   # nothing was actually executed


def test_operator_scoping_only_chases_the_requested_invoices(lago):
    op = build_billing_operator()
    res = op.invoke("billing.dunning", {"account_id": "acme", "invoice_ids": ["a2"], "_approval": {"approved": True}})
    assert lago.retries == ["a2"] and res["settled_count"] == 1
    lago.retries.clear()
    res = op.invoke("billing.dunning", {"account_id": "beta", "invoice_ids": ["a1"], "_approval": {"approved": True}})
    assert lago.retries == [] and res["status"] == "error"           # a1 is not beta's: skipped, nothing chased
    assert op.invoke("billing.dunning", {"account_id": "acme", "invoice_ids": ["a1"]})["status"] == "pending_approval"


# ── attribution ─────────────────────────────────────────────────────────────────────────────────────────
def _outcome_confidence(lago, competing: bool) -> float:
    clock = {"now": NOW}
    lp = decision.build_loop(clock=lambda: clock["now"])
    if competing:       # a human ALSO chased this account two days earlier
        record_human_action(lp.interventions, opportunity_id="billing:acme", action_kind="phone_call",
                            at=NOW - 2 * DAY, candidate_id="human:acme:call")
    decision.approve_mission(lp, _decide(lp).mission_id)
    rows = decision.observe_outcomes(interventions=lp.interventions, outcomes=lp.outcomes,
                                     ingestor=lp.ingestor, now=NOW + 3 * DAY, after_days=1)
    assert len(rows) == 1
    return rows[0]["attribution_confidence"], rows[0]["competing"]


def test_competing_intervention_discounts_attribution(lago):
    alone, n0 = _outcome_confidence(lago, competing=False)
    for k in ("a1", "a2"):                                           # reset the fake: unpaid + overdue again
        lago.invoices[k] = {**lago.invoices[k], "payment_status": "failed", "payment_overdue": True}
    contested, n1 = _outcome_confidence(lago, competing=True)
    assert (n0, n1) == (0, 1)
    assert contested < alone


# ── HTTP surface ────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def http(lago, loop, monkeypatch, load_app):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "op:secret")
    loop_api.set_loop(loop)
    yield TestClient(load_app("billing").app), loop
    loop_api.set_loop(None)


def test_http_decide_then_approve(http, lago):
    c, lp = http
    r = c.post("/api/decide", headers=AUTH, json={})
    j = r.json()
    assert r.status_code == 200 and j["selected"]["action_kind"] == "dunning"
    assert j["mission_state"] == "waiting_human" and j["mission_id"] and lago.retries == []
    assert j["opportunities"][0]["candidates"] and j["inputs"]["invoice_ids"] == ["a1", "a2"]
    a = c.post(f"/api/decide/{j['mission_id']}/approve", headers=AUTH, json={"decision": "approve"}).json()
    assert a["state"] == "succeeded" and a["dunning"]["settled_count"] == 2
    assert sorted(lago.retries) == ["a1", "a2"]


def test_http_fails_closed_and_requires_credentials(http, lago, monkeypatch):
    c, lp = http
    assert c.post("/api/decide", json={}).status_code == 401
    assert c.post("/api/decide", headers={"Authorization": "Basic " + base64.b64encode(b"op:bad").decode()},
                  json={}).status_code == 401
    assert c.post("/api/decide/x/approve", json={}).status_code == 401
    monkeypatch.delenv("OPERATOR_BASIC_AUTH")
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    assert c.post("/api/decide", headers=AUTH, json={}).status_code == 503
    assert c.post("/api/decide/x/approve", headers=AUTH, json={}).status_code == 503
    assert lp.interventions.all() == [] and lago.retries == []


def test_http_unknown_mission_and_account(http):
    c, _ = http
    assert c.post("/api/decide/mission_nope/approve", headers=AUTH, json={}).status_code == 404
    assert c.post("/api/decide", headers=AUTH, json={"account": "nobody"}).status_code == 404


FORGERIES = [{"_approval": True}, {"_approval": {"approved": True}}, {"_Approval": 1},
             {"nested": {"_approval": True}}, {"__approval": 1}]


@pytest.mark.parametrize("forged", FORGERIES)
def test_forged_approval_over_http_does_nothing(forged, http, lago):
    c, lp = http
    j = c.post("/api/decide", headers=AUTH, json={"account": "acme", "invoice_ids": ["b1"], **forged}).json()
    assert j["mission_state"] == "waiting_human" and j["inputs"]["invoice_ids"] == ["a1", "a2"]   # scope not caller-set
    # the open operator surface also only stages, whatever the body claims
    staged = c.post("/invoke", json={"capability": "billing.dunning",
                                     "inputs": {"account_id": "acme", "invoice_ids": ["a1"], **forged}}).json()["result"]
    assert staged["status"] == "pending_approval"
    c.post(f"/api/decide/{j['mission_id']}/approve", headers=AUTH, json={"decision": "reject", **forged})
    assert lago.retries == []
    assert lp.runtime._missions[j["mission_id"]].state is not MissionState.SUCCEEDED
