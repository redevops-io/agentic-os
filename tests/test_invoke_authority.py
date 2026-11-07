"""An operator's ``POST /invoke`` runs a side-effecting capability only for an authenticated identity with current, scoped authority and the
applicable approval, in EVERY execution profile (production, development, test, unset).

* identity:   an ``AuthenticatedPrincipal`` produced from the control credential; the credential-less development caller is not one.
* authority:  re-evaluated with ``AuthorityGuard.check`` when the call arrives (revocation, expiry, scope), never remembered from an earlier decision.
* approval:   a human approval satisfies an ESCALATE and the capability's own ``approval_required``; it never overrides a DENY.
* development has no ambient allowance: the only way to run without an envelope or approval is an explicit, named ``AuthorityTestPolicy``
  (recorded in the audit trail and the response). Production never honours one.
* read-only capabilities keep running without an envelope outside production (the rule the mission runtime applies to a read-only node).
* every refusal and every allow is audited; a refusal's wording is fixed and says nothing about what exists.
"""
from __future__ import annotations

import json
import secrets as _secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.action_auth import ActionAuthorizer
from agentic_os.mission.approval import mint_approval_token
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.authority_policy import AuthorityTestPolicy
from agentic_os.mission.control_auth import Caller
from agentic_os.mission.execution_auth import mint_execution_authorization
from agentic_os.mission.invoke_gate import InvokeRefused, Who
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.principal import AuthenticatedPrincipal, PrincipalIssuer, PrincipalRequired, principal_of_caller
from agentic_os.mission.profile import ProfileError
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilityManifest
from agentic_os.mission.verify import is_staged
from agentic_os.overlays import Principal

SECRET = _secrets.token_hex(24)
ADMIN, ACME, GLOBEX = "admin-token", "acme-token", "globex-token"
PROFILES = ["production", "development", "test", None]            # None = AGENTIC_OS_PROFILE unset
NON_PRODUCTION = ["development", "test", None]
FIXED_403 = "execution authorization required: this call is not authorised"
FIXED_401 = "authentication required: a side-effecting call needs an authenticated identity"
SENTINEL = "recipient-must-not-be-logged@customer.example"


class World:
    n = 0

    def __init__(self, tmp_path, monkeypatch, profile, *, test_policy=None, audit_store=None, authority=True, tokens=True):
        mp = monkeypatch
        World.n += 1
        tmp_path = tmp_path / f"world{World.n}"                  # a test may build several worlds: each has its own databases
        tmp_path.mkdir()
        for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "AGENTIC_OS_OPERATOR_TEST_POLICY",
                  "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_OPERATOR_AUDIT_LOG", "AGENTIC_OS_AUTHORITY_DB"):
            mp.delenv(k, raising=False)
        if profile:
            mp.setenv("AGENTIC_OS_PROFILE", profile)
        mp.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
        mp.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
        if tokens:
            mp.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
            mp.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={ACME},globex={GLOBEX}")
        else:
            mp.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
            mp.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
        self.db = str(tmp_path / "auth.db")
        if authority:
            mp.setenv("AGENTIC_OS_AUTHORITY_DB", self.db)
        self.store = SqliteEnvelopeStore(self.db)
        self.ledger = SqliteLedger(self.db + ".usage")
        self.guard = AuthorityGuard(self.store, self.ledger)
        self.ran: list = []
        ran = self.ran

        def send(i):
            ran.append(("send", dict(i)))
            return {"sent": True}

        self.op = Operator("crm", [
            capability("crm.send", send, provides=["done"], side_effecting=True, undo="crm.undo"),
            capability("crm.lookup", lambda i: (ran.append(("lookup", dict(i))), {"found": True})[1]),
            capability("crm.bank_change", lambda i: (ran.append(("bank", dict(i))), {"changed": True})[1], side_effecting=True,
                       approval_required=True, undo="crm.undo"),
            capability("crm.report", lambda i: (ran.append(("report", dict(i))), {"ok": True})[1], approval_required=True),
            capability("crm.undo", lambda i: {"undone": True}, side_effecting=True),
        ], test_policy=test_policy, audit_store=audit_store)
        app = FastAPI()
        app.include_router(self.op.router())
        self.client = TestClient(app, raise_server_exceptions=False)

    # envelopes ------------------------------------------------------------------------------------------------
    def envelope(self, key, *grants, tenant="acme", ttl=3600, **kw):
        base = dict(tenant=tenant, principal="owner", mission_id=key, issued_at=time.time() - 10, expires_at=time.time() + ttl,
                    grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000))
        base.update(kw)
        env = AuthorityEnvelope(**base)
        self.store.put(env)
        return env

    def registry(self):
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("crm", list(self.op.manifest.capabilities)))
        return reg

    def direct(self, cap, inputs, tenant="acme"):
        """What ``POST /actions/authorize`` does: evaluate the tenant's direct-action envelope and mint an execution authorization."""
        out = ActionAuthorizer(self.guard, self.registry()).authorize(tenant=tenant, subject="requester", operator="crm", capability=cap, inputs=inputs)
        assert out["authorized"], out
        return out

    def approval(self, cap, inputs, mission="m1", node="n1"):
        return mint_approval_token(SECRET, operator="crm", capability=cap, mission_id=mission, node_id=node, inputs=inputs, marker={"approved": True})

    # calls ------------------------------------------------------------------------------------------------------
    def call(self, cap="crm.send", inputs=None, *, token=ADMIN, authorization=None, approval_token=None, mission_id=None, node_id=None, key="k1"):
        body = {"capability": cap, "inputs": inputs if inputs is not None else {"recipient": SENTINEL}, "idempotency_key": key}
        for k, v in (("authorization", authorization), ("approval_token", approval_token), ("mission_id", mission_id), ("node_id", node_id)):
            if v:
                body[k] = v
        headers = {"Idempotency-Key": key}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/invoke", json=body, headers=headers)

    def events(self, outcome=None):
        return [e for e in self.op.gate.audit.recent if outcome is None or e["outcome"] == outcome]

    def sent(self):
        return [r for r in self.ran if r[0] == "send"]


@pytest.fixture
def make(tmp_path, monkeypatch, no_operator_test_policy):
    def _make(profile="development", **kw):
        return World(tmp_path, monkeypatch, profile, **kw)
    return _make


# ══ 1. nothing configured: every side-effecting /invoke is refused ══════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_a_valid_credential_with_no_envelope_and_no_policy_is_refused_with_fixed_wording(make, profile):
    w = make(profile)
    r = w.call("crm.send")
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403
    assert w.ran == []                                              # no handler was called


@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_the_credential_less_development_caller_is_not_an_identity(make, monkeypatch, profile):
    w = make(profile, tokens=False)
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    r = w.call("crm.send", token=None)
    assert r.status_code == 401 and r.json()["detail"] == FIXED_401 and w.ran == []
    assert r.headers["www-authenticate"] == "Bearer"


def test_the_anonymous_flag_is_not_honoured_in_production(make, monkeypatch):
    w = make("production", tokens=False)
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    assert w.call("crm.send", token=None).status_code == 503 and w.ran == []
    assert w.call("crm.lookup", token=None).status_code == 503


@pytest.mark.parametrize("profile", PROFILES)
def test_missing_or_wrong_credentials_are_refused_in_every_profile(make, profile):
    w = make(profile)
    assert w.call("crm.send", token=None).status_code == 401
    assert w.call("crm.send", token="not-a-token").status_code == 401
    assert w.ran == []


def test_the_anonymous_caller_is_not_a_principal():
    with pytest.raises(PrincipalRequired):
        principal_of_caller(Caller(None, "admin", anonymous=True))
    assert principal_of_caller(Caller("acme", "requester")).id == "requester:acme"


# ══ 2. identity: forged principals and other tenants ═══════════════════════════════════════════════════════════
def test_a_principal_the_authentication_layer_did_not_issue_is_refused(make):
    w = make("development")
    w.envelope("acme:direct", Grant("crm.send"))
    claims = {"tenant": "acme", "mission_id": "direct:x", "lease_id": "direct:x"}
    plain = Principal("admin", "user", ("admin",), "*")
    bad_proof = AuthenticatedPrincipal(id="admin", kind="user", roles=("admin",), tenant="*", method="control-token", issuer="control-plane",
                                       issued_at=time.time(), proof="0" * 64)
    foreign = PrincipalIssuer("someone-else").issue(plain, method="control-token")
    for forged in (plain, bad_proof, foreign):
        with pytest.raises(InvokeRefused) as e:
            w.op.gate.authorize(Who(principal=forged), "crm.send", {"recipient": "x"}, claims=claims)
        assert e.value.status == 401 and e.value.rule == "forged_principal"
    assert w.events("refused")[-1]["rule"] == "forged_principal"


def test_a_principal_for_one_tenant_cannot_use_another_tenants_authorization(make):
    w = make("development")
    acme = principal_of_caller(Caller("acme", "requester"))
    with pytest.raises(InvokeRefused) as e:
        w.op.gate.authorize(Who(principal=acme), "crm.send", {"recipient": "x"},
                            claims={"tenant": "globex", "mission_id": "direct:g", "lease_id": "direct:g"})
    assert e.value.status == 403 and e.value.rule == "tenant_mismatch"


@pytest.mark.parametrize("profile", PROFILES)
def test_a_tenant_credential_cannot_use_another_tenants_execution_authorization(make, profile):
    w = make(profile)
    w.envelope("globex:direct", Grant("crm.send"), tenant="globex")
    out = w.direct("crm.send", {"recipient": SENTINEL}, tenant="globex")
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"], token=ACME)
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []


@pytest.mark.parametrize("profile", PROFILES)
def test_a_tenant_credential_cannot_use_another_tenants_envelope_through_an_approval(make, profile):
    w = make(profile)
    w.envelope("m1", Grant("crm.send"), tenant="globex")
    r = w.call("crm.send", approval_token=w.approval("crm.send", {"recipient": SENTINEL}), mission_id="m1", node_id="n1", token=ACME)
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []


# ══ 3. current scoped authority, evaluated when the call arrives ═══════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_a_covered_call_runs_in_every_profile_and_is_recorded(make, profile):
    w = make(profile)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 200 and r.json()["result"]["sent"] is True and len(w.sent()) == 1
    ev = w.events("allowed")[-1]
    assert ev["authority_basis"] == "envelope" and ev["envelope_key"] == "acme:direct" and ev["tenant"] == "acme"
    assert ev["who"]["id"] == "admin" and ev["who"]["authenticated"] is True and ev["who"]["method"] == "control-token"


@pytest.mark.parametrize("profile", PROFILES)
def test_revocation_after_the_authorization_was_issued_is_honoured_immediately(make, profile):
    w = make(profile)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.store.revoke("acme:direct")
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []
    assert w.events("refused")[-1]["rule"] == "revoked"


@pytest.mark.parametrize("profile", PROFILES)
def test_expiry_and_narrowing_after_the_authorization_was_issued_are_honoured_immediately(make, profile):
    w = make(profile)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.envelope("acme:direct", Grant("crm.lookup"))                                   # a newer version without the grant
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "out_of_scope"
    w2 = make(profile)
    w2.envelope("acme:direct", Grant("crm.send"))
    out2 = w2.direct("crm.send", {"recipient": SENTINEL})
    w2.envelope("acme:direct", Grant("crm.send"), ttl=-5)                            # now expired
    assert w2.call("crm.send", authorization=out2["authorization"], key=out2["idempotency_key"]).status_code == 403
    assert w2.ran == [] and w2.events("refused")[-1]["rule"] == "expired"


@pytest.mark.parametrize("profile", PROFILES)
def test_a_revoked_authority_is_not_answered_from_the_idempotency_cache(make, profile):
    w = make(profile)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    assert w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"]).status_code == 200
    w.store.revoke("acme:direct")
    again = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert again.status_code == 403 and len(w.sent()) == 1


@pytest.mark.parametrize("profile", PROFILES)
def test_an_authorization_with_no_matching_reservation_is_refused(make, profile):
    w = make(profile)
    inputs = {"recipient": SENTINEL}
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-x", node_id="n", lease_id="m-x:n:a1",
                                       attempt=1, envelope_digest="d", inputs=inputs, idempotency_key="k1", dispatch_by="runtime")
    r = w.call("crm.send", inputs, authorization=tok)
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "lease_unknown"


def test_a_tampered_authority_store_refuses_and_is_not_overridden_by_a_test_policy(make):
    import sqlite3
    pol = AuthorityTestPolicy.allow_all("would-allow", "must not outrank an integrity failure")
    w = make("development", test_policy=pol)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    c = sqlite3.connect(w.db, isolation_level=None)
    c.execute("UPDATE envelopes SET body = body || ' ' WHERE mission_id='acme:direct'")
    c.close()
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "authority_integrity"


def test_a_tampered_store_also_refuses_an_approved_call_and_a_test_policy_does_not_override_it(make):
    import sqlite3
    w = make("development", test_policy=AuthorityTestPolicy.allow_all("would-allow", "must not outrank an integrity failure"))
    w.envelope("m1", Grant("crm.send"))
    c = sqlite3.connect(w.db, isolation_level=None)
    c.execute("UPDATE envelopes SET body = body || ' ' WHERE mission_id='m1'")
    c.close()
    inputs = {"recipient": SENTINEL}
    r = w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1")
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "authority_integrity"


def test_a_guard_that_raises_an_integrity_error_is_a_refusal_never_a_pass(make):
    from agentic_os.mission.integrity import IntegrityError

    class Failing:
        durable = True
        envelopes = None

        def lease(self, lease_id):
            return {"lease_id": lease_id, "key": "acme:direct", "meta": {"capability": "crm.send", "tenant": "acme", "tier": 1}}

        def check(self, *a, **k):
            raise IntegrityError("the authority store failed")

        def dispatch_once(self, lease_id):
            return True
    w = make("development", test_policy=AuthorityTestPolicy.allow_all("would-allow", "must not outrank an integrity failure"))
    w.op.authority = Failing()
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-1", node_id="n", lease_id="m-1:n:a1",
                                       attempt=1, envelope_digest="d", inputs={"recipient": SENTINEL}, idempotency_key="k1", dispatch_by="runtime")
    r = w.call("crm.send", authorization=tok)
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "authority_integrity"


def test_a_reservation_for_another_capability_does_not_back_the_authorization(make):
    w = make("development")
    w.envelope("env-x", Grant("*"))
    w.ledger.reserve_lease("m-1:n:a1", "env-x", {"actions": 1.0}, {}, None, time.time(),
                           {"capability": "crm.lookup", "tenant": "acme", "tier": 0, "side_effecting": False})
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-1", node_id="n", lease_id="m-1:n:a1",
                                       attempt=1, envelope_digest="d", inputs={"recipient": SENTINEL}, idempotency_key="k1", dispatch_by="runtime")
    r = w.call("crm.send", authorization=tok)
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "lease_unknown"


def test_an_unexpected_failure_inside_the_gate_is_a_refusal(make, monkeypatch):
    w = make("development", test_policy=AuthorityTestPolicy.allow_all("would-allow", "even a broken gate must not allow"))
    monkeypatch.setattr(w.op.gate, "_authority", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = w.call("crm.send")
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []
    assert w.events("refused")[-1]["rule"] == "gate_error"


def test_an_authorization_without_an_authority_store_is_refused_in_production_and_accepted_as_signed_elsewhere(make):
    inputs = {"recipient": SENTINEL}
    tok = lambda: mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-1", node_id="n", lease_id="m-1:n:a1",
                                               attempt=1, envelope_digest="d", inputs=inputs, idempotency_key="k1", dispatch_by="runtime")
    prod = make("production", authority=False)
    prod.op.authority = None
    r = prod.call("crm.send", inputs, authorization=tok())
    assert r.status_code == 403 and prod.ran == [] and prod.events("refused")[-1]["rule"] == "authority_unavailable"
    dev = make("development", authority=False)
    r = dev.call("crm.send", inputs, authorization=tok())
    assert r.status_code == 200 and dev.events("allowed")[-1]["authority_basis"] == "execution_authorization"


# ══ 4. approval: satisfies an ESCALATE, never overrides a DENY ════════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_an_approval_with_a_covering_envelope_runs(make, profile):
    w = make(profile)
    w.envelope("m1", Grant("crm.send"))
    inputs = {"recipient": SENTINEL}
    r = w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1")
    assert r.status_code == 200 and len(w.sent()) == 1
    ev = w.events("allowed")[-1]
    assert ev["authority_basis"] == "envelope" and ev["approval"] == "verified" and ev["envelope_key"] == "m1"


@pytest.mark.parametrize("profile", PROFILES)
def test_an_approval_alone_is_no_longer_authority(make, profile):
    w = make(profile)
    inputs = {"recipient": SENTINEL}
    r = w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1")
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []


@pytest.mark.parametrize("profile", PROFILES)
def test_an_approval_does_not_override_a_denial(make, profile):
    inputs = {"recipient": SENTINEL}
    for setup in ("out_of_scope", "revoked", "expired", "forbidden"):
        w = make(profile)
        if setup == "out_of_scope":
            w.envelope("m1", Grant("crm.lookup"))
        elif setup == "revoked":
            w.envelope("m1", Grant("crm.send"))
            w.store.revoke("m1")
        elif setup == "expired":
            w.envelope("m1", Grant("crm.send"), ttl=-5)
        else:
            w.envelope("m1", Grant("crm.send"), forbidden=("crm.send",))
        r = w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1")
        assert r.status_code == 403 and w.ran == [], setup
        assert w.events("refused")[-1]["rule"] == setup, setup


@pytest.mark.parametrize("profile", PROFILES)
def test_an_approval_satisfies_an_escalation(make, profile):
    w = make(profile)
    w.envelope("m1", Grant("crm.send", max_tier=0))                    # the call is past the tier this grant runs unattended
    inputs = {"recipient": SENTINEL}
    r = w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1")
    assert r.status_code == 200 and len(w.sent()) == 1
    ev = w.events("allowed")[-1]
    assert ev["rule"] == "tier_over_grant" and ev["approval"] == "verified"


@pytest.mark.parametrize("profile", PROFILES)
def test_an_escalation_without_an_approval_is_refused(make, profile):
    w = make(profile)
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.envelope("acme:direct", Grant("crm.send", max_tier=0))            # narrowed after the authorization was issued
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 403 and w.ran == [] and w.events("refused")[-1]["rule"] == "tier_over_grant"


@pytest.mark.parametrize("profile", PROFILES)
def test_an_approval_bound_to_other_inputs_is_not_an_approval(make, profile):
    w = make(profile)
    w.envelope("m1", Grant("crm.send"))
    r = w.call("crm.send", {"recipient": SENTINEL}, approval_token=w.approval("crm.send", {"recipient": "other@customer.example"}),
               mission_id="m1", node_id="n1")
    assert r.status_code == 403 and w.ran == []


@pytest.mark.parametrize("profile", PROFILES)
def test_a_mandatory_approval_is_never_satisfied_by_an_execution_authorization(make, profile):
    w = make(profile)
    w.envelope("m-1", Grant("crm.bank_change", max_tier=3))
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.bank_change", tenant="acme", mission_id="m-1", node_id="n", lease_id="m-1:n:a1",
                                       attempt=1, envelope_digest="d", inputs={"iban": "X"}, idempotency_key="k1", dispatch_by="runtime")
    w.ledger.reserve_lease("m-1:n:a1", "m-1", {"actions": 1.0}, {}, None, time.time(),
                           {"capability": "crm.bank_change", "tenant": "acme", "tier": 1, "side_effecting": True})
    r = w.call("crm.bank_change", {"iban": "X"}, authorization=tok)
    assert r.status_code == 403 and "human approval" in r.text and w.ran == []


@pytest.mark.parametrize("profile", PROFILES)
def test_a_mandatory_approval_with_authority_and_a_verified_approval_runs(make, profile):
    w = make(profile)
    w.envelope("m1", Grant("crm.bank_change", max_tier=3))
    inputs = {"iban": "X"}
    r = w.call("crm.bank_change", inputs, approval_token=w.approval("crm.bank_change", inputs), mission_id="m1", node_id="n1")
    assert r.status_code == 200 and not is_staged(r.json()["result"]) and [x[0] for x in w.ran] == ["bank"]


@pytest.mark.parametrize("profile", PROFILES)
def test_authority_is_asked_before_approval(make, profile):
    w = make(profile)
    r = w.call("crm.bank_change", {"iban": "X"})                         # no envelope, no approval: the denial, never a staged preview
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403 and w.ran == []


# ══ 5. development has no ambient allowance: only an explicit, named test policy ═══════════════════════════════
@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_named_test_policy_is_the_only_way_to_run_without_an_envelope(make, profile):
    pol = AuthorityTestPolicy("unit-tests", "this suite calls crm.send without owner envelopes", allow=("crm.send",))
    w = make(profile, test_policy=pol)
    r = w.call("crm.send")
    assert r.status_code == 200 and len(w.sent()) == 1
    assert r.headers["x-authority-basis"] == "test_policy:unit-tests"                # visible in the response
    ev = w.events("allowed")[-1]                                                      # and in the audit trail
    assert ev["authority_basis"] == "test_policy:unit-tests" and ev["test_policy"]["name"] == "unit-tests"
    assert ev["test_policy"]["digest"] == pol.digest() and ev["who"]["id"] == "admin"


@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_test_policy_covers_only_the_capabilities_it_lists(make, profile):
    w = make(profile, test_policy=AuthorityTestPolicy("narrow", "only send", allow=("crm.send",)))
    assert w.call("crm.send").status_code == 200
    r = w.call("crm.undo", {"x": 1}, key="k2")
    assert r.status_code == 403 and r.json()["detail"] == FIXED_403
    assert "narrow" in w.events("refused")[-1]["reason"]


@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_test_policy_never_overrides_an_envelope_that_denies(make, profile):
    w = make(profile, test_policy=AuthorityTestPolicy.allow_all("would-allow", "must not outrank an envelope"))
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.store.revoke("acme:direct")
    assert w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"]).status_code == 403
    w.envelope("m1", Grant("crm.lookup"))
    inputs = {"recipient": SENTINEL}
    assert w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1", key="k2").status_code == 403
    assert w.ran == []


def test_a_test_policy_is_refused_when_constructing_an_operator_in_production(make, tmp_path, monkeypatch):
    make("production")                                                               # sets the production environment
    with pytest.raises(ProfileError):
        Operator("crm", [], test_policy=AuthorityTestPolicy.allow_all("p", "r"))
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"name": "from-file", "reason": "dev", "allow": ["*"]}))
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_TEST_POLICY", str(pol))
    with pytest.raises(ProfileError):
        Operator("crm", [])


def test_an_inherited_test_policy_is_ignored_in_production(make, monkeypatch):
    monkeypatch.setattr(Operator, "test_policy", AuthorityTestPolicy.allow_all("class-default", "inherited", unapproved=True))
    w = make("production")
    r = w.call("crm.send")
    assert r.status_code == 403 and w.ran == []
    assert w.events("refused")[-1]["test_policy"] is None
    assert w.call("crm.bank_change", {"iban": "X"}, key="k2").status_code == 403


def test_a_test_policy_can_be_configured_from_a_file_outside_production(make, tmp_path, monkeypatch):
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"name": "dev-file", "reason": "local development", "allow": ["crm.send"]}))
    make("development")
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_TEST_POLICY", str(pol))
    op = Operator("crm", [capability("crm.send", lambda i: {"sent": True}, side_effecting=True, undo="crm.undo")])
    assert op.test_policy is not None and op.test_policy.name == "dev-file" and op.test_policy.permits("crm.send")


@pytest.mark.parametrize("bad", [{"name": "x", "reason": "r", "allw": ["*"]}, {"name": "", "reason": "r"}, {"name": "x", "reason": ""},
                                 {"name": "x", "reason": "r", "allow": "crm.send"}])
def test_a_malformed_test_policy_is_an_error_not_a_guess(bad):
    with pytest.raises((ValueError, TypeError)):
        AuthorityTestPolicy.from_dict(bad)


def test_a_test_policy_without_unapproved_keeps_its_record_and_digest():
    p = AuthorityTestPolicy("n", "r", allow=("a",))
    assert "unapproved" not in p.to_record()
    assert "unapproved" in AuthorityTestPolicy("n", "r", unapproved=("a",)).to_record()
    assert p.digest() != AuthorityTestPolicy("n", "r", allow=("a",), unapproved=("a",)).digest()


# ── the named test identity ────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_the_credential_less_caller_runs_only_as_the_named_test_identity_of_a_policy_that_accepts_it(make, monkeypatch, profile):
    pol = AuthorityTestPolicy("anon-dev", "local walkthrough", allow=("crm.send",), anonymous_operators=True)
    w = make(profile, tokens=False, test_policy=pol)
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    r = w.call("crm.send", token=None)
    assert r.status_code == 200 and r.headers["x-authority-basis"] == "test_policy:anon-dev"
    ev = w.events("allowed")[-1]
    assert ev["who"] == {"id": "test-identity:anon-dev", "kind": "test", "authenticated": False, "method": "test-policy",
                         "basis": "test_policy:anon-dev"}
    assert w.call("crm.undo", {"x": 1}, token=None, key="k2").status_code == 403           # identity accepted, capability not listed


@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_policy_that_does_not_accept_anonymous_operators_does_not_make_the_caller_an_identity(make, monkeypatch, profile):
    w = make(profile, tokens=False, test_policy=AuthorityTestPolicy.allow_all("no-anon", "token callers only", anonymous_operators=False))
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    assert w.call("crm.send", token=None).status_code == 401 and w.ran == []


# ── a waiver of a mandatory approval is also an explicit policy ──────────────────────────────────────────────────────
@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_mandatory_approval_stages_unless_a_test_policy_waives_it_by_name(make, profile):
    w = make(profile, test_policy=AuthorityTestPolicy("allow-only", "allows but does not waive", allow=("crm.*",)))
    r = w.call("crm.bank_change", {"iban": "X"})
    assert r.status_code == 200 and is_staged(r.json()["result"]) and w.ran == []
    assert w.events("staged")[-1]["approval"] == "missing"
    w2 = make(profile, test_policy=AuthorityTestPolicy("waives", "waives the bank change approval", allow=("crm.*",), unapproved=("crm.bank_change",)))
    r2 = w2.call("crm.bank_change", {"iban": "X"})
    assert r2.status_code == 200 and not is_staged(r2.json()["result"]) and [x[0] for x in w2.ran] == ["bank"]
    assert w2.events("allowed")[-1]["approval"] == "waived:waives" and r2.headers["x-authority-approval"] == "waived:waives"
    assert w2.call("crm.report", {}, key="k2").status_code == 200 and is_staged(w2.call("crm.report", {}, key="k3").json()["result"])


@pytest.mark.parametrize("profile", PROFILES)
def test_the_removed_environment_flag_waives_nothing(make, monkeypatch, profile):
    w = make(profile)
    monkeypatch.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    assert w.call("crm.bank_change", {"iban": "X"}).status_code == 403 and w.ran == []


# ══ 6. read-only capabilities ═════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_a_read_needs_authentication_and_no_envelope(make, profile):
    w = make(profile)
    r = w.call("crm.lookup", {})
    assert r.status_code == 200 and r.json()["result"]["found"] is True
    assert w.call("crm.lookup", {}, token=None).status_code == 401


@pytest.mark.parametrize("profile", NON_PRODUCTION)
def test_a_read_may_run_credential_less_outside_production_but_a_write_may_not(make, monkeypatch, profile):
    w = make(profile, tokens=False)
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    assert w.call("crm.lookup", {}, token=None).status_code == 200
    assert w.call("crm.send", token=None, key="k2").status_code == 401


@pytest.mark.parametrize("profile", PROFILES)
def test_an_unknown_capability_is_refused_exactly_like_a_real_one(make, profile):
    w = make(profile)
    real = w.call("crm.send")
    unknown = w.call("crm.does_not_exist", {"recipient": SENTINEL}, key="k2")
    assert (unknown.status_code, unknown.json()) == (real.status_code, real.json()) == (403, {"detail": FIXED_403})


# ══ 7. audit ════════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_refusals_say_the_same_thing_whatever_the_reason(make, profile):
    inputs = {"recipient": SENTINEL}
    bodies = set()
    w = make(profile)
    bodies.add(w.call("crm.send").text)                                                                  # nothing exists
    w.envelope("m1", Grant("crm.lookup"))
    bodies.add(w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs), mission_id="m1", node_id="n1", key="k2").text)  # out of scope
    w.envelope("m2", Grant("crm.send"), tenant="globex")
    bodies.add(w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs, mission="m2"), mission_id="m2", node_id="n1",
                      key="k3", token=ACME).text)                                                         # another tenant's envelope
    w.envelope("m3", Grant("crm.send"))
    w.store.revoke("m3")
    bodies.add(w.call("crm.send", inputs, approval_token=w.approval("crm.send", inputs, mission="m3"), mission_id="m3", node_id="n1", key="k4").text)
    assert {json.loads(b)["detail"] for b in bodies} == {FIXED_403} and len(bodies) == 1             # byte-identical
    assert {e["rule"] for e in w.events("refused")} >= {"no_authority", "out_of_scope", "revoked"}


@pytest.mark.parametrize("profile", PROFILES)
def test_every_decision_is_recorded_without_the_arguments(make, profile):
    w = make(profile)
    w.call("crm.send")                                                                                     # refused
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])                    # allowed
    refused, allowed = w.events("refused")[0], w.events("allowed")[0]
    for ev in (refused, allowed):
        assert {"operator", "capability", "who", "tenant", "rule", "reason", "authority_basis", "approval", "profile", "inputs_sha256", "ts"} <= set(ev)
        assert ev["operator"] == "crm" and ev["capability"] == "crm.send" and len(ev["inputs_sha256"]) == 64
    assert refused["who"]["id"] == "admin" and refused["rule"] == "no_authority"
    assert SENTINEL not in json.dumps(w.events())                                                          # a hash of the inputs, never the inputs


def test_a_staged_call_is_recorded_as_staged(make):
    w = make("development", test_policy=AuthorityTestPolicy("p", "r", allow=("crm.*",)))
    w.call("crm.bank_change", {"iban": "X"})
    assert [e["outcome"] for e in w.events()] == ["staged"]


def test_decisions_reach_the_durable_event_log_when_one_is_configured(make, tmp_path):
    store = EventStore(str(tmp_path / "audit.jsonl"))
    w = make("development", audit_store=store)
    w.call("crm.send")
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    evs = store.for_mission("operator-invoke:crm")
    assert [e.type for e in evs] == ["OperatorInvokeRefused", "OperatorInvokeAllowed"]
    assert evs[0].payload["rule"] == "no_authority" and evs[1].payload["authority_basis"] == "envelope"
    assert store.check_integrity("operator-invoke:crm")                                                   # hash-chained like any mission log


def test_a_side_effecting_call_that_cannot_be_recorded_does_not_run(make):
    class Broken:
        def append(self, *a, **k):
            raise OSError("disk full")
    w = make("development", audit_store=Broken())
    w.envelope("acme:direct", Grant("crm.send"))
    out = w.direct("crm.send", {"recipient": SENTINEL})
    r = w.call("crm.send", authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 503 and w.ran == []
    assert w.call("crm.send", key="k2").status_code == 403                                                 # a refusal stays a refusal


def test_the_security_endpoint_shows_the_test_policy_in_force(make):
    w = make("development", test_policy=AuthorityTestPolicy("shown", "visible in /_security", allow=("crm.send",)))
    sec = w.client.get("/_security").json()
    assert sec["invoke"]["test_policy"]["name"] == "shown" and sec["invoke"]["identity_required"] is True
    none = make("development").client.get("/_security").json()
    assert none["invoke"]["test_policy"] is None
    prod = make("production").client.get("/_security").json()
    assert prod["invoke"]["test_policy"] is None
