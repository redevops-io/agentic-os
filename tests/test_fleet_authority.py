"""Fleet agent work (including detached background jobs) is inside the owner's envelope in production."""
from __future__ import annotations

import threading

import pytest

from agentic_os.fleet import Fleet
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, Lease, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore,
    SqliteLedger)
from agentic_os.mission.profile import ProfileError
from agentic_os.registry import Module

NOW = 7_000_000.0


class _Reg:
    def __init__(self):
        self.m = Module(name="crm", pain="leads", source="apps/crm", agents=("scout",), approval_required=("send",))

    def get(self, name):
        return self.m


class _Router:
    def __init__(self, fail=False):
        self.calls, self.fail, self.gate = 0, fail, None

    def run(self, task):
        self.calls += 1
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("model down")
        return type("R", (), {"text": "ok"})()


class _Ctx:
    def __init__(self):
        self.requested = []

    def profile(self):
        return {"name": "Acme"}

    def request_approval(self, **kw):
        self.requested.append(kw)
        return "approval-object"


def fleet(guard=None, router=None):
    return Fleet(_Reg(), router or _Router(), _Ctx(), authority=guard, authority_key="fleet", tenant="acme")


def guard_with(*grants, store=None, ledger=None, **kw):
    st = store or MemoryEnvelopeStore()
    st.put(AuthorityEnvelope(tenant="acme", principal="fleet", mission_id="fleet", issued_at=NOW - 1, expires_at=NOW + 3600,
                             grants=grants, budget=AutonomyBudget(usd=10, max_actions=100)))
    return AuthorityGuard(st, ledger or MemoryLedger(), clock=lambda: NOW)


def test_development_default_is_unchanged():
    f = fleet(None)
    assert f.dispatch("crm", "scout", "ask", "hi") == "ok"


def test_production_fleet_cannot_be_built_without_a_durable_guard(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    with pytest.raises(ProfileError):
        fleet(None)
    with pytest.raises(ProfileError):
        fleet(guard_with(Grant("fleet.*")))                       # in-memory is not durable


def test_agent_work_outside_the_envelope_is_refused_and_never_runs():
    g = guard_with(Grant("fleet.crm.ask"))
    f = fleet(g)
    assert f.dispatch("crm", "scout", "ask", "hi") == "ok"
    with pytest.raises(PermissionError, match="out_of_scope"):
        f.dispatch("crm", "scout", "summarize", "hi")
    assert f.router.calls == 1


def test_background_jobs_are_inside_the_envelope_too():
    g = guard_with(Grant("fleet.crm.ask"))
    f = fleet(g)
    with pytest.raises(PermissionError):
        f.dispatch("crm", "scout", "other", "hi", background=True)
    assert f.router.calls == 0 and f.jobs == {}


def test_limit_reached_goes_to_a_human_not_a_silent_run():
    g = guard_with(Grant("fleet.crm.ask", max_calls=1))
    f = fleet(g)
    assert f.dispatch("crm", "scout", "ask", "hi") == "ok"
    assert f.dispatch("crm", "scout", "ask", "hi") == "approval-object"
    assert "authority" in f.context.requested[-1]["summary"] and f.router.calls == 1


def test_lease_is_settled_on_success_and_returned_when_the_model_call_fails():
    g = guard_with(Grant("fleet.crm.ask", max_calls=2))
    f = fleet(g)
    f.dispatch("crm", "scout", "ask", "hi")
    assert g.outstanding("fleet") == [] and g.ledger.used("fleet")["g0:calls"] == 1
    f2 = fleet(g, _Router(fail=True))
    with pytest.raises(RuntimeError):
        f2.dispatch("crm", "scout", "ask", "hi")
    assert g.ledger.used("fleet")["g0:calls"] == 1                # the failed call gave its reservation back


def test_background_job_lifecycle_and_shared_allowance():
    g = guard_with(Grant("fleet.crm.ask", max_calls=2))
    r = _Router()
    r.gate = threading.Event()
    f = fleet(g, r)
    first = f.dispatch("crm", "scout", "ask", "a", background=True)
    second = f.dispatch("crm", "scout", "ask", "b", background=True)
    assert first["status"] == "running" and second["status"] == "running"
    assert f.dispatch("crm", "scout", "ask", "c", background=True) == "approval-object"      # allowance held by running jobs
    assert {s for _, s in g.outstanding("fleet")} <= {"reserved", "dispatched"} and len(g.outstanding("fleet")) == 2
    r.gate.set()
    for t in threading.enumerate():
        if t.name.startswith("bg-"):
            t.join(5)
    assert g.outstanding("fleet") == [] and all(j["status"] == "done" for j in f.jobs.values())
    assert g.ledger.used("fleet")["g0:calls"] == 2


def test_mandatory_approval_actions_still_stage_for_a_human():
    g = guard_with(Grant("fleet.crm.send"))
    f = fleet(g)
    assert f.dispatch("crm", "scout", "send", "x") == "approval-object" and f.router.calls == 0
