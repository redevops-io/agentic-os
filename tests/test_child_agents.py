"""Child agents: identity, attenuated authority, a carved-out allowance, a signed scoped credential and a lineage that is enforced at every
decision (revoke, expire or narrow any ancestor and every descendant stops at once)."""
from __future__ import annotations

import json
import secrets

import pytest

from agentic_os.agent_gateway import (
    AgentGateway, DevTokenVerifier, GatewayPrincipal, GatewayRequest, GatewayStatus, MissionRuntimeAdapter, register_mission_capabilities)
from agentic_os.agent_gateway.registry import CapabilityRegistry
from agentic_os.mission.authority import (
    Action, AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore,
    SqliteLedger, Verdict)
from agentic_os.mission.children import (
    ACTIVE, ChildCredentialVerifier, ChildRegistry, ChildSpawner, SpawnRefused, mint_child_credential)
from agentic_os.overlays import Principal

NOW = 3_000_000.0
SECRET = secrets.token_hex(32)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def parent_env(**kw) -> AuthorityEnvelope:
    base = dict(tenant="acme", principal="orchestrator", mission_id="root", issued_at=NOW - 1, expires_at=NOW + 3600, may_delegate=True,
                grants=(Grant("crm.*"), Grant("billing.refund", max_amount=100.0, max_tier=2, max_calls=10), Grant("missions.*", max_tier=3)),
                budget=AutonomyBudget(usd=10, tokens=1000, wall_clock_s=3600, max_actions=20, max_children=3, max_depth=2, max_parallel=2))
    base.update(kw)
    return AuthorityEnvelope(**base)


class Lab:
    def __init__(self, tmp, monkeypatch, env=None, clock=None, durable=True):
        monkeypatch.setenv("AGENTIC_OS_PROFILE", "test")
        self.tmp, self.clock = tmp, clock or Clock()
        self.db = str(tmp / "auth.db")
        self.events: list = []
        self.guard = self._guard(durable)
        self.guard.envelopes.put(env or parent_env())
        self.registry = ChildRegistry(str(tmp / "children.db"))
        self.spawner = ChildSpawner(self.guard, self.registry, SECRET, clock=self.clock, on_event=self.events.append)

    def _guard(self, durable=True):
        if durable:
            return AuthorityGuard(SqliteEnvelopeStore(self.db), SqliteLedger(self.db + ".usage"), clock=self.clock)
        return AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger(), clock=self.clock)

    def spawn(self, parent="root", role="researcher", grants=(Grant("crm.read"),), usd=2, tokens=100, actions=5, **kw):
        b = dict(usd=usd, tokens=tokens, wall_clock_s=600, max_actions=actions, max_children=0, max_depth=0, max_parallel=1)
        b.update(kw.pop("budget", {}))
        return self.spawner.spawn(parent, "acme", role=role, goal="gather", grants=tuple(grants), budget=AutonomyBudget(**b), **kw)

    def used(self, key="root"):
        return {k: v for k, v in self.guard.ledger.used(key).items() if k in ("usd", "tokens", "actions", "children") and v}

    def act(self, key, cap="crm.read", usd=0.0, **args):
        return self.guard.authorize(key, "acme", Action(cap, args, 0, False, usd))

    def kinds(self):
        return [e["type"] for e in self.events]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(tmp_path, monkeypatch)


# ───────────────────────── identity and attenuation ─────────────────────────
def test_a_child_is_its_own_identity_with_a_provably_smaller_envelope(lab):
    c = lab.spawn()
    env = lab.guard.envelopes.get(c.key)
    assert c.principal.startswith("agent:orchestrator/researcher/") and c.principal != "orchestrator"
    assert env.principal == c.principal and env.mission_id == c.key != "root"
    assert env.depth == 1 and env.parent_mission_id == "root" and env.parent_digest == lab.guard.envelopes.get("root").digest()
    assert env.grants == (Grant("crm.read"),) and env.expires_at <= lab.guard.envelopes.get("root").expires_at
    assert lab.act(c.key).allowed and lab.act(c.key, "crm.update").rule == "out_of_scope"           # it is not the parent
    assert lab.act("root", "crm.update").allowed                                                    # ... and the parent is unchanged
    assert lab.registry.get(c.child_id)["status"] == ACTIVE and "CHILD_SPAWNED" in lab.kinds()


def test_two_children_get_distinct_identities_and_keys(lab):
    a, b = lab.spawn(role="a"), lab.spawn(role="a")
    assert len({a.principal, b.principal, a.key, b.key}) == 4


@pytest.mark.parametrize("kw,code", [
    (dict(grants=(Grant("billing.delete"),)), "grant_not_covered"),
    (dict(grants=(Grant("billing.refund", max_amount=500.0, max_tier=2),)), "grant_not_covered"),
    (dict(usd=11), "budget_exceeds_parent"),
    (dict(budget=dict(max_depth=2)), "budget_exceeds_parent"),
    (dict(budget=dict(max_children=4)), "budget_exceeds_parent"),
    (dict(grants=()), "invalid"),
    (dict(role=" "), "invalid"),
])
def test_a_child_can_never_exceed_its_parent_and_a_refusal_leaves_no_residue(lab, kw, code):
    before = lab.used()
    with pytest.raises(SpawnRefused) as ei:
        lab.spawn(**kw)
    assert ei.value.code == code
    assert lab.used() == before and lab.registry.children_of("root") == []          # no quota, no allowance, no child left behind
    assert "CHILD_REFUSED" in lab.kinds()
    assert lab.registry.audit_log("root")[-1]["type"] == "refused"


def test_a_parent_that_may_not_delegate_cannot_spawn(tmp_path, monkeypatch):
    L = Lab(tmp_path, monkeypatch, env=parent_env(may_delegate=False))
    with pytest.raises(SpawnRefused) as ei:
        L.spawn()
    assert ei.value.code == "delegation_not_allowed"


def test_spawning_needs_a_signing_secret(tmp_path, monkeypatch):
    L = Lab(tmp_path, monkeypatch)
    sp = ChildSpawner(L.guard, L.registry, None, clock=L.clock)
    with pytest.raises(SpawnRefused) as ei:
        sp.spawn("root", "acme", role="r", goal="g", grants=(Grant("crm.read"),), budget=AutonomyBudget(usd=1, max_actions=1, wall_clock_s=60))
    assert ei.value.code == "no_signing_secret" and L.registry.children_of("root") == []


def test_the_production_profile_requires_a_durable_guard(tmp_path, monkeypatch):
    L = Lab(tmp_path, monkeypatch, durable=False)
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    with pytest.raises(SpawnRefused) as ei:
        ChildSpawner(L.guard, L.registry, SECRET)
    assert ei.value.code == "not_durable"


# ───────────────────────── the allowance is carved out of the parent ─────────────────────────
def test_the_allowance_is_carved_out_of_the_parent_and_unused_budget_returns(lab):
    a = lab.spawn(usd=6, tokens=600, actions=10)
    assert lab.used()["usd"] == 6 and lab.used()["actions"] == 10                       # the parent cannot spend what it handed out
    with pytest.raises(SpawnRefused) as ei:
        lab.spawn(usd=6, tokens=100, actions=5)                                         # only 4 usd left to hand out
    assert ei.value.code == "allowance_unavailable" and lab.used()["usd"] == 6 and lab.used()["children"] == 1
    assert lab.act("root", "crm.read", usd=5).rule in ("usd_budget", "budget_exceeded") or not lab.act("root", "crm.read", usd=5).allowed
    assert lab.act(a.key, "crm.read", usd=2).allowed
    out = lab.spawner.close(a.child_id)
    assert out["used"]["usd"] == 2 and out["used"]["actions"] == 1
    assert lab.used()["usd"] == 2 and lab.used()["actions"] == 1                        # what it did not use went back
    assert lab.spawn(usd=6, tokens=100, actions=5).child_id                             # ... so the parent can hand it out again


def test_a_child_cannot_spend_more_than_its_allowance(lab):
    c = lab.spawn(usd=2, tokens=100, actions=2)
    assert lab.act(c.key, usd=1.5).allowed
    d = lab.act(c.key, usd=1.0)
    assert not d.allowed and d.verdict is Verdict.ESCALATE


def test_the_sum_over_a_tree_is_bounded_by_its_root(lab):
    for _ in range(2):
        lab.spawn(usd=5, tokens=100, actions=5)
    with pytest.raises(SpawnRefused):
        lab.spawn(usd=0.01, tokens=1, actions=1)                                        # nothing left to give: usd 10 handed out in full


# ───────────────────────── fan-out and depth ─────────────────────────
def test_active_children_are_capped_atomically_and_a_refusal_returns_the_quota(lab):
    lab.spawn(usd=1)
    lab.spawn(usd=1)                                                                    # max_parallel = 2
    n = lab.used()["children"]
    with pytest.raises(SpawnRefused) as ei:
        lab.spawn(usd=1)
    assert ei.value.code == "max_parallel" and lab.used()["children"] == n and lab.used()["usd"] == 2


def test_the_child_quota_counts_everything_ever_spawned(lab):
    for i in range(3):
        c = lab.spawn(usd=1)
        lab.spawner.close(c.child_id)
    with pytest.raises(SpawnRefused) as ei:
        lab.spawn(usd=1)
    assert ei.value.code == "child_quota"


def test_depth_is_bounded_by_the_chain(lab):
    c = lab.spawn(may_delegate=True, grants=(Grant("crm.read"),), budget=dict(max_children=1, max_depth=1))
    g = lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2)                # a grandchild
    assert lab.guard.envelopes.get(g.key).depth == 2
    with pytest.raises(SpawnRefused) as ei:                                             # ... but not a great-grandchild
        lab.spawn(parent=g.key, role="subsub", usd=0.5, tokens=1, actions=1)
    assert ei.value.code in ("delegation_not_allowed", "max_depth")


def test_the_whole_tree_has_an_active_ceiling(tmp_path, monkeypatch):
    L = Lab(tmp_path, monkeypatch, env=parent_env(budget=AutonomyBudget(usd=100, tokens=10000, wall_clock_s=3600, max_actions=200,
                                                                         max_children=10, max_depth=3, max_parallel=10)))
    L.spawner.max_active_tree = 3
    for _ in range(3):
        L.spawn(usd=1)
    with pytest.raises(SpawnRefused) as ei:
        L.spawn(usd=1)
    assert ei.value.code == "max_tree"


# ───────────────────────── the chain is enforced at every decision ─────────────────────────
def test_revoking_an_ancestor_stops_every_descendant_at_once(lab):
    c = lab.spawn(may_delegate=True, budget=dict(max_children=1, max_depth=1))
    g = lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2)
    assert lab.act(c.key).allowed and lab.act(g.key).allowed
    lab.guard.envelopes.revoke("root")
    assert lab.act(c.key).rule == "ancestor_revoked" and lab.act(g.key).rule == "ancestor_revoked"
    v = lab.spawner.verifier()
    assert not v.verify(c.credential, tenant="acme").ok and not v.verify(g.credential, tenant="acme").ok


def test_revoking_the_tenant_epoch_stops_the_whole_tree(lab):
    c = lab.spawn()
    lab.guard.envelopes.revoke_all("acme")
    assert not lab.act(c.key).allowed and not lab.spawner.verifier().verify(c.credential, tenant="acme").ok


def test_an_expired_ancestor_stops_its_descendants(lab):
    c = lab.spawn(ttl_s=100000)
    assert lab.guard.envelopes.get(c.key).expires_at == NOW + 3600                      # a child's life is clamped to its parent's
    lab.guard.envelopes.put(parent_env(expires_at=NOW + 100))                           # the owner shortens the parent
    lab.clock.t = NOW + 200
    assert lab.guard.envelopes.get(c.key).expires_at > lab.clock.t                      # the child's own envelope has not run out ...
    assert lab.act(c.key).rule == "ancestor_expired"                                    # ... but its parent has, so it has
    assert not lab.spawner.verifier().verify(c.credential, tenant="acme").ok


def test_a_child_under_a_revoked_ancestor_cannot_spawn_either(lab):
    c = lab.spawn(may_delegate=True, budget=dict(max_children=1, max_depth=1))
    lab.guard.envelopes.revoke("root")
    with pytest.raises(SpawnRefused) as ei:
        lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2)
    assert ei.value.code == "ancestor_revoked"


def test_a_registry_that_says_closed_wins_even_if_the_envelope_still_stands(lab):
    c = lab.spawn()
    lab.registry.set_status(c.child_id, "closed", "closed by another replica", NOW)
    assert not lab.spawner.verifier().verify(c.credential, tenant="acme").ok


def test_narrowing_a_parent_narrows_its_children(lab):
    c = lab.spawn(grants=(Grant("billing.refund", max_amount=50.0, max_tier=2, max_calls=5),))
    assert lab.act(c.key, "billing.refund", amount=40.0).allowed
    lab.guard.envelopes.put(parent_env(grants=(Grant("crm.*"), Grant("billing.refund", max_amount=10.0, max_tier=2, max_calls=10))))
    assert lab.act(c.key, "billing.refund", amount=40.0).rule == "exceeds_parent"      # the owner lowered the parent's limit


def test_a_child_whose_parent_is_gone_is_denied_not_trusted(lab):
    c = lab.spawn()
    lab.guard.envelopes._e.pop("root", None) if hasattr(lab.guard.envelopes, "_e") else None
    store = MemoryEnvelopeStore()
    store.put(lab.guard.envelopes.get(c.key))
    g = AuthorityGuard(store, MemoryLedger(), clock=lab.clock)
    assert g.authorize(c.key, "acme", Action("crm.read", {})).rule == "orphaned"


def test_a_child_cannot_act_in_another_tenant(lab):
    c = lab.spawn()
    assert lab.guard.authorize(c.key, "globex", Action("crm.read", {})).rule == "no_authority"


# ───────────────────────── the credential ─────────────────────────
def test_a_credential_verifies_and_carries_the_lineage(lab):
    c = lab.spawn(client="orchestrator-app")
    v = lab.spawner.verifier().verify(c.credential, tenant="acme", client="orchestrator-app")
    assert v.ok and v.claims["principal"] == c.principal and v.claims["parent_key"] == "root" and v.claims["depth"] == 1


@pytest.mark.parametrize("what", ["tampered", "wrong_secret", "garbage", "none", "other_tenant", "other_client", "expired", "closed", "rotated",
                                  "envelope_replaced", "revoked_child"])
def test_a_credential_that_does_not_fully_hold_is_refused(lab, what):
    c = lab.spawn(client="app-1")
    ver = lab.spawner.verifier()
    tok, kw = c.credential, dict(tenant="acme", client="app-1")
    if what == "tampered":
        p, s = tok.split(".")
        tok = p[:-2] + ("AA" if p[-2:] != "AA" else "BB") + "." + s
    elif what == "wrong_secret":
        ver = ChildCredentialVerifier(lab.guard, lab.registry, secrets.token_hex(32), clock=lab.clock)
    elif what == "garbage":
        tok = "not-a-token"
    elif what == "none":
        tok = None
    elif what == "other_tenant":
        kw["tenant"] = "globex"
    elif what == "other_client":
        kw["client"] = "app-2"
    elif what == "expired":
        lab.clock.t = NOW + 1000
    elif what == "closed":
        lab.spawner.close(c.child_id)
    elif what == "rotated":
        lab.spawner.renew_credential(c.child_id)
    elif what == "envelope_replaced":
        e = lab.guard.envelopes.get(c.key)
        from dataclasses import replace
        lab.guard.envelopes.put(replace(e, grants=(Grant("crm.*"),)))               # a different envelope under the same key
    elif what == "revoked_child":
        lab.spawner.revoke(c.child_id, actor="ops@acme", reason="misbehaving worker")
    assert ver.verify(tok, **kw).ok is False


def test_a_credential_never_outlives_the_child(lab):
    c = lab.spawn(ttl_s=60)
    assert c.expires_at <= NOW + 60
    lab.clock.t = NOW + 61
    assert not lab.spawner.verifier().verify(c.credential, tenant="acme").ok


def test_renewing_gives_a_new_credential_and_kills_the_old_one(lab):
    c = lab.spawn()
    c2 = lab.spawner.renew_credential(c.child_id)
    ver = lab.spawner.verifier()
    assert c2.credential != c.credential and ver.verify(c2.credential, tenant="acme").ok and not ver.verify(c.credential, tenant="acme").ok
    lab.spawner.close(c.child_id)
    with pytest.raises(SpawnRefused):
        lab.spawner.renew_credential(c.child_id)


def test_an_unsigned_or_unbound_credential_is_never_minted():
    assert mint_child_credential(None, tenant="a", child_id="c", principal="p", parent_key="r", role="x", depth=1, envelope_digest="d", exp=1, jti="j") is None
    assert mint_child_credential(SECRET, tenant="", child_id="c", principal="p", parent_key="r", role="x", depth=1, envelope_digest="d", exp=1, jti="j") is None


# ───────────────────────── the gateway takes identity from the credential ─────────────────────────
class _Rt:
    def create_mission(self, goal, **kw): raise AssertionError("not used")
    def missions(self): return []
    def inbox(self): return []
    def explain(self, mid): return {}


def gateway(lab, client="orchestrator-app"):
    adapter = MissionRuntimeAdapter(_Rt())
    dev = DevTokenVerifier()
    reg = register_mission_capabilities(CapabilityRegistry(), adapter)
    gw = AgentGateway(registry=reg, authorize=dev.authorize, mission=adapter, authority=lab.guard, child_credentials=lab.spawner.verifier(),
                      authority_key=lambda gp: "root")                                  # a mapping the child must NOT be able to ride on
    dev.issue("orchestrator", tenant="acme", grants=("missions.delegate", "missions.read", "missions.control"))
    dev.issue("agent:orchestrator/researcher/abcd1234", tenant="acme", grants=("missions.read",))
    return gw, GatewayPrincipal(Principal("orchestrator", "service", (), "acme"), client_id=client)


def gcall(gw, gp, cred=None, cap="missions.list"):
    return gw.invoke(GatewayRequest(gp, cap, {}, credential=cred))


def test_the_gateway_acts_as_the_child_under_the_childs_envelope(lab):
    c = lab.spawn(grants=(Grant("missions.list"),), client="orchestrator-app")
    gw, gp = gateway(lab)
    ok = gcall(gw, gp, c.credential)
    assert ok.decision.reason != "authority:out_of_scope" and ok.status is not GatewayStatus.DENIED
    denied = gcall(gw, gp, c.credential, cap="missions.pause")                         # the parent may; the child may not
    assert denied.status is GatewayStatus.DENIED and denied.decision.reason == "authority:out_of_scope"
    assert gcall(gw, gp, None).status is not GatewayStatus.DENIED                       # the parent, as itself, is unchanged


def test_the_call_is_attributed_to_the_child_in_the_reservation_record(lab):
    c = lab.spawn(grants=(Grant("missions.list"),), client="orchestrator-app")
    gw, gp = gateway(lab)
    req = GatewayRequest(gp, "missions.list", {}, credential=c.credential)
    gw.invoke(req)
    meta = lab.guard.lease(req.request_id)["meta"]
    assert meta["agent_id"] == c.child_id and meta["agent_principal"] == c.principal and meta["agent_parent"] == "root" and meta["agent_depth"] == 1


def test_every_bad_credential_gets_the_same_answer(lab):
    c = lab.spawn(grants=(Grant("missions.list"),), client="orchestrator-app")
    lab.spawner.close(c.child_id)
    gw, gp = gateway(lab)
    other, gp_other = gateway(lab, client="someone-else")
    replies = {gcall(gw, gp, "garbage").decision.reason, gcall(gw, gp, c.credential).decision.reason,
               gcall(other, gp_other, c.credential).decision.reason}
    assert replies == {"authority:invalid_credential"}                                   # no oracle for why


def test_claiming_an_agent_identity_without_a_credential_is_refused(lab):
    gw, _ = gateway(lab)
    gp = GatewayPrincipal(Principal("agent:orchestrator/researcher/abcd1234", "service", (), "acme"), client_id="orchestrator-app")
    r = gw.invoke(GatewayRequest(gp, "missions.list", {}))                              # visible to it, and the mapping would let it ride on root
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:invalid_credential"


def test_a_credential_stolen_by_another_client_is_useless(lab):
    c = lab.spawn(grants=(Grant("missions.list"),), client="orchestrator-app")
    gw, gp = gateway(lab, client="thief-app")
    assert gcall(gw, gp, c.credential).decision.reason == "authority:invalid_credential"


# ───────────────────────── ending children, lineage, durability ─────────────────────────
def test_revoking_cascades_down_the_lineage_deepest_first_and_names_who_and_why(lab):
    c = lab.spawn(may_delegate=True, budget=dict(max_children=1, max_depth=1))
    g = lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2)
    with pytest.raises(SpawnRefused):
        lab.spawner.revoke(c.child_id, actor="", reason="misbehaving worker")
    with pytest.raises(SpawnRefused):
        lab.spawner.revoke(c.child_id, actor="ops", reason="x")
    out = lab.spawner.revoke(c.child_id, actor="ops@acme", reason="misbehaving worker")
    assert out["status"] == "revoked"
    assert lab.registry.get(g.child_id)["status"] == "revoked" and lab.registry.get(c.child_id)["status"] == "revoked"
    log = [r["type"] for r in lab.registry.audit_log() if r["type"] in ("revoked", "closed")]
    assert log[:2] == ["revoked", "revoked"] and lab.registry.audit_log(c.child_id)[-1]["payload"]["actor"] == "ops@acme"
    assert not lab.act(g.key).allowed and "CHILD_REVOKED" in lab.kinds()
    assert lab.spawner.close(c.child_id)["already"] is True                              # the first decision stands


def test_closing_reports_what_the_child_left_unresolved_instead_of_dropping_it(lab):
    c = lab.spawn()
    d = lab.guard.authorize(c.key, "acme", Action("crm.read", {}), lease_id="call-1")
    assert d.allowed and lab.guard.dispatch_once("call-1")                               # sent, and the outcome is not known yet
    out = lab.spawner.close(c.child_id)
    assert out["unresolved"] == ["call-1"] and lab.registry.audit_log(c.child_id)[-1]["payload"]["unresolved"] == ["call-1"]
    assert lab.guard.lease("call-1")["state"] == "dispatched"                            # still counted, still owed a reconciliation


def test_the_parent_finishing_ends_its_children(lab):
    a, b = lab.spawn(), lab.spawn()
    outs = lab.spawner.close_tree("root", "mission finished")
    assert {o["child_id"] for o in outs} == {a.child_id, b.child_id}
    assert not lab.act(a.key).allowed and lab.used().get("usd", 0) == 0


def test_the_lineage_and_usage_are_readable_as_a_tree(lab):
    c = lab.spawn(may_delegate=True, budget=dict(max_children=1, max_depth=1))
    lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2)
    lab.act(c.key)
    t = lab.spawner.tree("root")
    assert [(x["role"], x["depth"], x["parent"] == "root") for x in t] == [("researcher", 1, True), ("sub", 2, False)]
    assert t[0]["used"]["actions"] >= 1


def test_everything_survives_a_restart(tmp_path, monkeypatch):
    L = Lab(tmp_path, monkeypatch)
    c = L.spawn(client="app-1")
    L2 = Lab.__new__(Lab)                                                                # a new process over the same files
    L2.clock, L2.events = L.clock, []
    L2.guard = L._guard()
    L2.registry = ChildRegistry(str(tmp_path / "children.db"))
    L2.spawner = ChildSpawner(L2.guard, L2.registry, SECRET, clock=L.clock)
    assert L2.spawner.verifier().verify(c.credential, tenant="acme", client="app-1").ok
    assert L2.registry.key_for_principal("acme", c.principal) == c.child_id
    L2.spawner.close(c.child_id)
    assert not L.spawner.verifier().verify(c.credential, tenant="acme", client="app-1").ok and L.registry.key_for_principal("acme", c.principal) is None


# ───────────────────────── nothing changes for everyone else ─────────────────────────
def test_a_non_child_envelope_serialises_and_digests_exactly_as_before():
    e = parent_env()
    d = e.to_dict()
    assert "parent_mission_id" not in d
    import hashlib
    expect = "sha256:" + hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert e.digest() == expect and AuthorityEnvelope.from_dict(d).digest() == e.digest()
