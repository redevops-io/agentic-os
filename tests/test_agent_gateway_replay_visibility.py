"""A stored idempotent result is served only to the caller it was produced for, only while that caller is still authorised, and only for the same
request. Synthetic data; the gateway is driven directly (no network).

What each group pins:

* revalidation: a replay is answered by the same checks as a first call (grants, OAuth scope, authority envelope, child credential and lineage),
  so revoking any of them ends replays, and a replay executes nothing and consumes none of the call allowance;
* binding: a key reused with different arguments, caller scope or capability is a conflict that reveals nothing of the earlier call;
* concurrency and bounds: concurrent retries execute once; the store has a TTL and a size bound and never evicts in-flight work;
* fail closed: a fault while revalidating is ``gateway_error``; an unknown outcome stays non-retryable; replays are audited.
"""
from __future__ import annotations

import threading
import time

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityKind, CapabilityManifest, CapabilityRegistry, DataClass, EgressAction, GatewayPrincipal,
    GatewayRequest, GatewayStatus, HandlerResult, InboxApprovalStore, RiskTier)
from agentic_os.agent_gateway.registry import PUBLIC_ERROR_CODES
from agentic_os.mission.authority import Grant
from agentic_os.overlays import Principal

SECRET = "customer-record-for-the-first-caller"


class Counter:
    """A handler that counts executions and returns a distinct, recognisable output each time."""

    def __init__(self, delay: float = 0.0):
        self.calls, self.delay, self.fail_next = 0, delay, False
        self._lock = threading.Lock()

    def __call__(self, req, env):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.delay:
            time.sleep(self.delay)
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("timed out after the request was written")
        return HandlerResult(ok=True, output={"record": SECRET, "n": n}, data_classes=(DataClass.INTERNAL,))


def read_manifest(name="idem.read", *, scopes=(), perms=("data.read",)):
    return CapabilityManifest(name, "reads synthetic data", CapabilityKind.DIRECT, permissions=perms, scopes=scopes, idempotent=True,
                              data_classes=(DataClass.INTERNAL,))


def write_manifest(name="idem.write", *, approval=None, perms=("data.write",)):
    return CapabilityManifest(name, "writes synthetic data", CapabilityKind.DIRECT, permissions=perms, risk_tier=RiskTier.BOUNDED_WRITE,
                              side_effecting=True, idempotent=True, provider="p", approval_policy=approval, data_classes=(DataClass.INTERNAL,))


class Grants:
    """A mutable authorizer: (tenant, subject, permission) triples, so a test can revoke."""

    def __init__(self, *triples):
        self.g = set(triples)

    def __call__(self, principal, perm):
        return (principal.tenant, principal.id, perm) in self.g


def gp(subject="bob", tenant="t1", workspace="", scopes=(), client="app"):
    return GatewayPrincipal(Principal(subject, "service", (), tenant), client_id=client, workspace=workspace, scopes=tuple(scopes))


def call(gw, who, cap="idem.read", args=None, key="k1", **kw):
    return gw.invoke(GatewayRequest(who, cap, dict(args or {}), idempotency_key=key, **kw))


def gateway(handler=None, *manifests, grants=None, **kw):
    handler = handler or Counter()
    reg = CapabilityRegistry()
    for m in manifests or (read_manifest(),):
        reg.register(m, handler)
    grants = grants or Grants(("t1", "bob", "data.read"))
    return AgentGateway(registry=reg, authorize=grants, **kw), handler, grants


def leaks(result):
    """Nothing of the earlier call is in what the caller is told."""
    return SECRET in repr(result.client_view()) or result.output is not None


# ───────────────────────────── revalidation: grants, scope, tenant ─────────────────────────────
def test_a_replay_is_served_while_the_permission_holds_and_executes_nothing():
    gw, h, _ = gateway()
    first, again = call(gw, gp()), call(gw, gp())
    assert first.ok and again.ok and again.output == first.output and h.calls == 1
    assert again.request_id == first.request_id                      # the caller receives the stored result as it was


def test_a_replay_is_refused_after_the_permission_is_revoked():
    gw, h, grants = gateway()
    assert call(gw, gp()).ok
    grants.g.clear()
    replay = call(gw, gp())
    assert replay.status is GatewayStatus.DENIED and not leaks(replay) and h.calls == 1


def test_a_replay_is_refused_after_the_oauth_scope_is_narrowed():
    gw, h, _ = gateway(None, read_manifest(scopes=("data.read",)))
    assert call(gw, gp(scopes=("data.read",))).ok
    replay = call(gw, gp(scopes=()))                                    # the same subject, a token without the scope
    assert replay.status is GatewayStatus.DENIED and not leaks(replay) and h.calls == 1
    assert call(gw, gp(scopes=("data.read",))).ok and h.calls == 1      # and the wider token still replays


def test_another_tenant_with_the_same_subject_and_key_gets_nothing_of_the_first():
    gw, h, grants = gateway()
    grants.g.add(("t2", "bob", "data.read"))
    first = call(gw, gp(tenant="t1"))
    other = call(gw, gp(tenant="t2"))
    assert other.ok and other.output["n"] == 2 and h.calls == 2           # its own execution: the key space is per tenant
    assert call(gw, gp(tenant="t1")).output == first.output and h.calls == 2


# ───────────────────────────── binding: arguments, scope, capability ─────────────────────────────
def test_a_different_payload_under_the_same_key_is_a_conflict_that_reveals_nothing():
    gw, h, _ = gateway()
    assert call(gw, gp(), args={"id": 1}).ok
    r = call(gw, gp(), args={"id": 2})
    assert r.status is GatewayStatus.ERROR and r.error == "idempotency_conflict" and not leaks(r) and h.calls == 1
    assert r.client_view() == {"status": "error", "request_id": r.request_id, "error": "idempotency_conflict"}
    assert call(gw, gp(), args={"id": 1}).ok and h.calls == 1            # the original request still replays


def test_the_same_arguments_written_differently_are_the_same_request():
    gw, h, _ = gateway()
    assert call(gw, gp(), args={"a": 1, "b": {"x": [1, 2], "y": "café"}}).ok
    again = call(gw, gp(), args={"b": {"y": "café", "x": (1.0, 2)}, "a": 1.0, "idempotency_key": "k1"})
    assert again.ok and h.calls == 1


def test_a_different_workspace_under_the_same_key_is_a_conflict():
    gw, h, _ = gateway()
    assert call(gw, gp(workspace="wsA")).ok
    r = call(gw, gp(workspace="wsB"))
    assert r.error == "idempotency_conflict" and not leaks(r) and h.calls == 1


def test_a_different_principal_under_the_same_key_is_a_conflict():
    gw, h, grants = gateway()
    grants.g.add(("t1", "carol", "data.read"))
    assert call(gw, gp("bob")).ok
    r = call(gw, gp("carol"))
    assert r.error == "idempotency_conflict" and not leaks(r) and h.calls == 1
    assert call(gw, gp("bob")).ok and h.calls == 1


def test_a_different_capability_under_the_same_key_is_a_conflict():
    gw, h, _ = gateway(None, read_manifest("idem.read"), read_manifest("idem.other"))
    assert call(gw, gp(), "idem.read").ok
    r = call(gw, gp(), "idem.other")
    assert r.error == "idempotency_conflict" and not leaks(r) and h.calls == 1


def test_a_caller_who_cannot_see_the_capability_learns_nothing_from_the_key():
    gw, h, grants = gateway()
    assert call(gw, gp()).ok
    nobody = call(gw, gp("mallory"))                                     # no grant: denied like an unknown capability, not a conflict
    assert nobody.status is GatewayStatus.DENIED and nobody.error == "" and h.calls == 1


# ───────────────────────────── authority: envelope, child revoke, other child, garbage credential ─────────────────────────────
NOW = 3_000_000.0


def authority_gateway(tmp_path, monkeypatch, *, max_calls=None, handler=None):
    from tests.test_child_agents import Lab
    lab = Lab(tmp_path, monkeypatch)
    h = handler or Counter()
    reg = CapabilityRegistry().register(read_manifest("crm.read", perms=("crm.read",)), h)
    grants = Grants(("acme", "orchestrator", "crm.read"))
    gw = AgentGateway(registry=reg, authorize=grants, authority=lab.guard, child_credentials=lab.spawner.verifier(), authority_key=lambda g: "root")
    return lab, gw, h, GatewayPrincipal(Principal("orchestrator", "service", (), "acme"), client_id="app")


def child_call(gw, who, cred, key="k1", args=None):
    return gw.invoke(GatewayRequest(who, "crm.read", dict(args or {}), idempotency_key=key, credential=cred))


def test_a_replay_is_refused_after_a_child_is_revoked(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    c = lab.spawn(grants=(Grant("crm.read"),), client="app")
    assert child_call(gw, who, c.credential).ok
    lab.spawner.revoke(c.child_id, actor="owner", reason="revoked for the test")
    r = child_call(gw, who, c.credential)
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:invalid_credential" and not leaks(r) and h.calls == 1


def test_a_different_child_with_the_same_key_gets_neither_the_result_nor_a_conflict_if_it_has_no_authority(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    one = lab.spawn(grants=(Grant("crm.read"),), client="app")
    two = lab.spawn(grants=(Grant("missions.list"),), client="app", role="other")
    assert child_call(gw, who, one.credential).ok
    r = child_call(gw, who, two.credential)
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:out_of_scope" and not leaks(r) and h.calls == 1


def test_a_different_child_that_is_allowed_to_call_gets_a_conflict_not_the_first_childs_result(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    one = lab.spawn(grants=(Grant("crm.read"),), client="app")
    two = lab.spawn(grants=(Grant("crm.read"),), client="app", role="other")
    assert child_call(gw, who, one.credential).ok
    r = child_call(gw, who, two.credential)
    assert r.error == "idempotency_conflict" and not leaks(r) and h.calls == 1
    assert child_call(gw, who, one.credential).ok and h.calls == 1


def test_a_garbage_credential_gets_no_cached_output(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    c = lab.spawn(grants=(Grant("crm.read"),), client="app")
    assert child_call(gw, who, c.credential).ok
    for cred in ("garbage", "", "x" * 400):
        r = child_call(gw, who, cred or None)
        assert not r.ok and not leaks(r), cred
    assert h.calls == 1


def test_a_replay_is_refused_after_the_authority_envelope_is_revoked(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    assert gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1")).ok         # the parent, as itself (authority key "root")
    lab.guard.envelopes.revoke("root")
    r = gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1"))
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:revoked" and not leaks(r) and h.calls == 1


def test_a_replay_consumes_no_call_allowance_and_leaves_no_reservation(tmp_path, monkeypatch):
    from tests.test_child_agents import parent_env
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    lab.guard.envelopes.put(parent_env(grants=(Grant("crm.read", max_calls=1),)))
    assert gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1")).ok
    used = lab.guard.ledger.used("root")
    for _ in range(3):
        assert gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1")).ok       # the allowance of one call is spent; retries still replay
    assert lab.guard.ledger.used("root") == used and lab.guard.outstanding("root") == [] and h.calls == 1
    assert not gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k2")).ok       # a NEW action is over the limit


# ───────────────────────────── concurrency ─────────────────────────────
def test_concurrent_retries_execute_exactly_once_and_the_others_replay_or_are_told_it_is_in_progress():
    gw, h, _ = gateway(Counter(delay=0.3))
    out, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        out.append(call(gw, gp()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert h.calls == 1
    assert sum(1 for r in out if r.ok) >= 1
    assert all(r.ok or r.error == "idempotency_in_progress" for r in out)
    assert {r.output["n"] for r in out if r.ok} == {1}
    assert call(gw, gp()).ok and h.calls == 1                                     # and once it finished, every retry replays


def test_concurrent_calls_with_different_arguments_under_one_key_execute_at_most_once():
    gw, h, _ = gateway(Counter(delay=0.2))
    out, barrier = [], threading.Barrier(6)

    def worker(i):
        barrier.wait()
        out.append(call(gw, gp(), args={"id": i % 2}))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert h.calls == 1 and all(r.ok or r.error in ("idempotency_conflict", "idempotency_in_progress") for r in out)


# ───────────────────────────── approvals ─────────────────────────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def approval_gateway(clock):
    inbox = InboxApprovalStore(clock=clock)
    from agentic_os.agent_gateway.contracts import ApprovalPolicy
    gw, h, grants = gateway(None, write_manifest(approval=ApprovalPolicy.REQUIRED), grants=Grants(("t1", "bob", "data.write")), approvals=inbox)
    return gw, h, inbox


def test_a_replay_of_an_executed_write_survives_the_expiry_of_its_approval_and_parks_nothing_new():
    clock = Clock()
    gw, h, inbox = approval_gateway(clock)
    assert call(gw, gp(), "idem.write", {"v": 1}).status is GatewayStatus.PENDING_APPROVAL
    inbox.approve(inbox.pending()[0]["id"], by="manager")
    first = call(gw, gp(), "idem.write", {"v": 1})
    assert first.ok and h.calls == 1
    clock.t += 10_000                                                           # the approval is long expired (and was single-use anyway)
    again = call(gw, gp(), "idem.write", {"v": 1})
    assert again.ok and again.output == first.output and h.calls == 1 and inbox.pending() == []


def test_a_rejected_write_is_not_executed_and_nothing_is_stored_for_its_key():
    gw, h, inbox = approval_gateway(Clock())
    assert call(gw, gp(), "idem.write", {"v": 1}).status is GatewayStatus.PENDING_APPROVAL
    inbox.reject(inbox.pending()[0]["id"], by="manager")
    for _ in range(2):
        assert call(gw, gp(), "idem.write", {"v": 1}).status is GatewayStatus.PENDING_APPROVAL
    assert h.calls == 0


def test_an_approval_that_expires_before_it_is_used_does_not_run_the_call():
    clock = Clock()
    gw, h, inbox = approval_gateway(clock)
    call(gw, gp(), "idem.write", {"v": 1})
    inbox.approve(inbox.pending()[0]["id"], by="manager")
    clock.t += 10_000
    assert call(gw, gp(), "idem.write", {"v": 1}).status is GatewayStatus.PENDING_APPROVAL and h.calls == 0
    inbox.approve(inbox.pending()[0]["id"], by="manager")                       # a fresh decision is needed, and then it runs once
    assert call(gw, gp(), "idem.write", {"v": 1}).ok and call(gw, gp(), "idem.write", {"v": 1}).ok and h.calls == 1


def test_a_revoked_caller_cannot_replay_an_executed_write():
    clock = Clock()
    gw, h, inbox = approval_gateway(clock)
    call(gw, gp(), "idem.write", {"v": 1})
    inbox.approve(inbox.pending()[0]["id"], by="manager")
    assert call(gw, gp(), "idem.write", {"v": 1}).ok
    gw.authorize.g.clear()
    r = call(gw, gp(), "idem.write", {"v": 1})
    assert r.status is GatewayStatus.DENIED and not leaks(r) and h.calls == 1


# ───────────────────────────── bounds: TTL and size ─────────────────────────────
def test_a_stored_result_expires_after_the_ttl():
    from agentic_os.agent_gateway import InMemoryIdempotencyStore
    clock = Clock()
    gw, h, _ = gateway(idempotency=InMemoryIdempotencyStore(ttl_s=60, max_entries=10, clock=clock))
    assert call(gw, gp()).ok
    clock.t += 59
    assert call(gw, gp()).ok and h.calls == 1
    clock.t += 2
    assert call(gw, gp()).ok and h.calls == 2                                   # the window is over: the key is unknown again


def test_the_store_is_bounded_and_evicts_the_oldest_completed_entry():
    from agentic_os.agent_gateway import InMemoryIdempotencyStore
    store = InMemoryIdempotencyStore(ttl_s=3600, max_entries=3)
    gw, h, _ = gateway(idempotency=store)
    for i in range(5):
        assert call(gw, gp(), key=f"k{i}").ok
    assert len(store) == 3 and h.calls == 5
    assert call(gw, gp(), key="k4").ok and h.calls == 5                         # recent keys still replay
    assert call(gw, gp(), key="k0").ok and h.calls == 6                         # an evicted key runs again


def test_a_full_store_never_evicts_in_flight_work_and_refuses_new_claims_instead():
    from agentic_os.agent_gateway import InMemoryIdempotencyStore
    store = InMemoryIdempotencyStore(ttl_s=3600, max_entries=2)
    gw, h, _ = gateway(Counter(delay=0.5), idempotency=store)
    started = [threading.Thread(target=lambda k=k: call(gw, gp(), key=k)) for k in ("a", "b")]
    [t.start() for t in started]
    time.sleep(0.2)                                                             # both are inside the handler
    r = call(gw, gp(), key="c")
    assert not r.ok and r.error == "gateway_error" and len(store) == 2
    [t.join() for t in started]
    assert call(gw, gp(), key="a").ok and h.calls == 2                          # the in-flight claims completed and are stored, not evicted


def test_eviction_never_turns_an_unknown_outcome_into_a_re_executable_call(tmp_path, monkeypatch):
    from agentic_os.agent_gateway import InMemoryIdempotencyStore
    from tests.test_gateway_unknown_outcome import wire, refund
    prov, gw, guard, who = wire()
    gw.idempotency = InMemoryIdempotencyStore(ttl_s=3600, max_entries=1)
    prov.fail_next = True
    assert refund(gw, who, key="k1").status is GatewayStatus.ERROR and prov.sends == 1
    for i in range(4):                                                          # churn the one-entry store
        assert refund(gw, who, key=f"other{i}", invoice=f"INV-{i}").ok
    r = refund(gw, who, key="k1")
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:outcome_unknown" and not leaks(r)
    assert prov.sends == 5 and gw.unresolved_calls("acme:agent")[0]["meta"]["idempotency_key"] == "k1"


def test_an_unknown_outcome_stays_non_retryable_and_a_changed_request_does_not_reveal_it():
    from tests.test_gateway_unknown_outcome import wire, refund
    prov, gw, guard, who = wire()
    prov.fail_next = True
    assert refund(gw, who).status is GatewayStatus.ERROR
    same = refund(gw, who)
    changed = refund(gw, who, amount=99)                                        # same key, other arguments
    assert same.decision.reason == "authority:outcome_unknown" and prov.sends == 1
    assert changed.status is GatewayStatus.DENIED and not leaks(changed) and prov.sends == 1


def test_a_claim_is_released_when_the_call_does_not_complete_so_a_retry_can_run():
    h = Counter()
    gw, h, _ = gateway(h)
    h.fail_next = True
    assert call(gw, gp()).status is GatewayStatus.ERROR
    assert call(gw, gp()).ok and h.calls == 2                                   # not stuck "in progress"
    gw2, h2, grants = gateway()
    grants.g.clear()
    assert call(gw2, gp()).status is GatewayStatus.DENIED
    grants.g.add(("t1", "bob", "data.read"))
    assert call(gw2, gp()).ok and h2.calls == 1


# ───────────────────────────── audit, faults, codes ─────────────────────────────
def test_a_replay_is_audited_under_its_own_request_id_without_the_result_or_arguments():
    gw, h, _ = gateway()
    first = call(gw, gp(), args={"note": "private-argument-text"})
    second = call(gw, gp(), args={"note": "private-argument-text"})
    events = gw.audit.events
    assert len(events) == 2 and events[0].request_id == first.request_id and events[1].request_id != first.request_id
    assert "idempotent_replay" in events[1].decision.policy_rule_ids and events[1].status is GatewayStatus.OK
    text = repr([e.as_record() for e in events])
    assert SECRET not in text and "private-argument-text" not in text
    assert second.audit_ref == "audit_1"


def test_a_conflict_is_audited():
    gw, h, _ = gateway()
    call(gw, gp(), args={"id": 1})
    call(gw, gp(), args={"id": 2})
    ev = gw.audit.events[-1]
    assert ev.status is GatewayStatus.ERROR and ev.decision.reason == "idempotency_conflict"


def test_a_fault_while_revalidating_a_replay_is_gateway_error_with_no_output():
    gw, h, grants = gateway()
    assert call(gw, gp()).ok

    def boom(principal, perm):
        raise RuntimeError("identity service down: db password=hunter2")

    gw.authorize = boom
    r = call(gw, gp())
    assert r.status is GatewayStatus.ERROR and r.error == "gateway_error" and not leaks(r) and "hunter2" not in repr(r) and h.calls == 1


def test_a_fault_in_the_authority_check_during_a_replay_is_gateway_error(tmp_path, monkeypatch):
    lab, gw, h, who = authority_gateway(tmp_path, monkeypatch)
    assert gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1")).ok
    lab.guard.check = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger offline"))
    r = gw.invoke(GatewayRequest(who, "crm.read", {}, idempotency_key="k1"))
    assert r.status is GatewayStatus.ERROR and r.error == "gateway_error" and not leaks(r) and h.calls == 1


def test_a_fault_in_the_idempotency_store_is_gateway_error_and_nothing_executes():
    class Broken:
        def begin(self, *a): raise RuntimeError("store offline")
        def abandon(self, *a): pass

    gw, h, _ = gateway(idempotency=Broken())
    r = call(gw, gp())
    assert r.status is GatewayStatus.ERROR and r.error == "gateway_error" and h.calls == 0


def test_the_egress_policy_is_asked_again_about_a_stored_output():
    class Policy:
        deny = False

        def decide(self, output, classes, principal, manifest):
            return (EgressAction.DENY, None, ("egress:tightened",)) if self.deny else (EgressAction.ALLOW, output, ())

    policy = Policy()
    gw, h, _ = gateway(egress=policy)
    assert call(gw, gp()).output is not None
    policy.deny = True
    r = call(gw, gp())
    assert r.ok and r.output is None and "egress:tightened" in r.decision.policy_rule_ids and h.calls == 1


def test_the_new_client_codes_are_in_the_allow_list_and_nothing_else_is_reported():
    assert {"idempotency_conflict", "idempotency_in_progress"} <= PUBLIC_ERROR_CODES
    gw, h, _ = gateway()
    call(gw, gp(), args={"id": 1})
    r = call(gw, gp(), args={"id": 2})
    assert r.error in PUBLIC_ERROR_CODES and set(r.client_view()) == {"status", "request_id", "error"}


def test_a_reconciled_result_is_replayed_only_to_the_request_it_was_authorised_for():
    from tests.test_gateway_unknown_outcome import wire, refund, EVIDENCE
    prov, gw, guard, who = wire()
    prov.fail_next = True
    refund(gw, who)
    (lease,) = gw.unresolved_calls("acme:agent")
    gw.reconcile_call(lease["lease_id"], executed=True, evidence=EVIDENCE, actor="ops@acme", reason="refund obj_1 is in the dashboard",
                      result={"ok": True, "provider_object_id": "obj_1"})
    changed = refund(gw, who, amount=99)
    assert changed.error == "idempotency_conflict" and prov.sends == 1
    same = refund(gw, who)
    assert same.ok and same.output["provider_object_id"] == "obj_1" and prov.sends == 1


def test_arguments_that_cannot_be_bound_fail_closed_before_anything_executes():
    gw, h, _ = gateway()
    for args in ({"x": object()}, {"blob": "x" * (300 * 1024)}):
        r = call(gw, gp(), args=args)
        assert r.status is GatewayStatus.ERROR and r.error == "gateway_error" and h.calls == 0
    assert call(gw, gp(), key="k" * 300).error == "gateway_error" and h.calls == 0
    assert call(gw, gp(), args={"x": object()}, key=None).ok and h.calls == 1      # without a key nothing needs binding
