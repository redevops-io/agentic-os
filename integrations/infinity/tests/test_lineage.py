"""Mission lineage <-> Infinity threads, and attenuated, time-bounded child authority."""
import json

import pytest

from conftest import UPDATE, drive, key_of, payload, settle
from redevops_infinity.demo.capabilities import default_envelope, default_grants, default_operations
from redevops_infinity.lineage import ChildScopePolicy, derive_child_grants

LOOK = {"lead_id": "lead_dana01"}


def child_call(env, rt, op, args, *, gid="t-child", anc=("t-root",), cid=None):
    code, c = rt.invoke(op, args, group_id=gid, ancestors=anc, call_id=cid)
    return code, c


def test_a_child_thread_acts_under_its_own_attenuated_identity(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c0 = rt.invoke("lookup_lead", LOOK, group_id="t-root")
    assert settle(env, rt, "t-root", c0)["status"] == "completed"
    code, c1 = child_call(env, rt, "lookup_lead", LOOK)
    assert code == 200
    assert settle(env, rt, "t-child", c1)["status"] == "completed"
    tree = env.spawner.tree(f"{env.tenant}:infinity-runtime")
    assert len(tree) == 1 and tree[0]["principal"].startswith("agent:") and tree[0]["depth"] == 1
    child_event = [e for e in env.gateway.audit.events if e.subject.startswith("agent:")]
    assert child_event and child_event[-1].capability == "crm.lead_lookup"


def test_the_child_can_only_use_operations_the_owner_marked_child_allowed(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "update_lead", UPDATE)
    p = settle(env, rt, "t-child", c)
    assert p["status"] == "denied" and p["reason"] == "policy_denied" and env.crm.calls == []
    # even if the operation were marked child_allowed, the child's envelope only holds grants for child_allowed capabilities
    env_grants = env.guard.envelopes.get(env.spawner.tree(f"{env.tenant}:infinity-runtime")[0]["child_id"]) if env.spawner.tree(f"{env.tenant}:infinity-runtime") else None
    assert env_grants is None or all(g.capability == "crm.lead_lookup" for g in env_grants.grants)


def test_child_grants_are_a_subset_of_the_parents_and_never_larger():
    ops = default_operations()
    parent = default_grants()
    policy = ChildScopePolicy(max_calls_per_grant=3)
    kids = derive_child_grants(parent, ops, policy)
    assert [g.capability for g in kids] == ["crm.lead_lookup"]
    for g in kids:
        assert any(p.covers(g) for p in parent) and (g.max_calls is not None and g.max_calls <= 3)
    # a parent grant with a smaller limit stays the ceiling
    from dataclasses import replace
    tight = (replace(parent[0], max_calls=2),)
    assert derive_child_grants(tight, ops, ChildScopePolicy(max_calls_per_grant=50))[0].max_calls == 2


def test_the_child_inherits_the_resource_restrictions_of_its_parent(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", {"lead_id": "lead_omar02"})
    settle(env, rt, "t-child", c)
    cid = env.spawner.tree(f"{env.tenant}:infinity-runtime")[0]["child_id"]
    g = env.guard.envelopes.get(cid).grants[0]
    assert dict(g.resources)["lead_id"] == ("lead_*",)


def test_the_child_call_limit_is_enforced(runtime_factory):
    env, srv, rt = runtime_factory(child_policy=ChildScopePolicy(max_calls_per_grant=2))
    outs = []
    for i in range(4):
        _, c = child_call(env, rt, "lookup_lead", LOOK)
        drive(env)
        outs.append(rt.await_result("t-child", c, 0.2) is not None)
    assert outs == [True, True, False, False]


def test_a_child_never_outlives_its_parent(runtime_factory, clock):
    env, srv, rt = runtime_factory(child_policy=ChildScopePolicy(ttl_s=10 ** 6))
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    settle(env, rt, "t-child", c)
    row = env.spawner.registry.get(env.spawner.tree(f"{env.tenant}:infinity-runtime")[0]["child_id"])
    parent_env = env.guard.envelopes.get(f"{env.tenant}:infinity-runtime")
    assert row["expires_at"] <= parent_env.expires_at
    clock.advance(parent_env.expires_at - clock() + 5)                  # the parent's authority has expired: so has everything below it
    _, c2 = child_call(env, rt, "lookup_lead", LOOK, cid="late")
    assert settle(env, rt, "t-child", c2)["status"] == "denied"


def test_revoking_the_parent_denies_the_child(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    assert settle(env, rt, "t-child", c)["status"] == "completed"
    env.envelopes.revoke(f"{env.tenant}:infinity-runtime")
    _, c2 = child_call(env, rt, "lookup_lead", LOOK, cid="after")
    assert settle(env, rt, "t-child", c2)["status"] == "denied"


def test_the_lineage_is_recorded_once_and_cannot_be_rewritten(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    settle(env, rt, "t-child", c)
    code, _ = child_call(env, rt, "lookup_lead", LOOK, anc=("t-other",), cid="x")      # same thread, different claimed parent
    assert code == 403
    code, _ = child_call(env, rt, "lookup_lead", LOOK, anc=(), cid="y")                  # same thread claimed as a root
    assert code == 403


def test_closing_a_thread_ends_the_authority_of_its_descendants(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", LOOK, gid="t-grand", anc=("t-root", "t-child"))
    p = settle(env, rt, "t-grand", c)
    assert p["status"] in ("denied",) and p["reason"] == "spawn_refused"      # children do not delegate unless the owner allows it
    env2, srv2, rt2 = runtime_factory(workdir=env.workdir + "/n", child_policy=ChildScopePolicy(may_delegate_children=True, max_children=1, max_depth=1))
    _, c1 = child_call(env2, rt2, "lookup_lead", LOOK, gid="t-grand", anc=("t-root", "t-child"))
    # a grandchild is only possible if the child may delegate AND the parent's budget allows depth; the intermediate thread is created on first sight
    drive(env2)
    assert rt2.await_result("t-grand", c1, 2) is not None
    assert rt2.close_thread("t-root") == 200
    live = [n for n in env2.spawner.tree(f"{env2.tenant}:infinity-runtime") if n["status"] == "active"]
    assert live == []
    code, _ = child_call(env2, rt2, "lookup_lead", LOOK, gid="t-child", anc=("t-root",), cid="z")
    assert code == 403                                                                     # a closed thread is closed


def test_without_a_spawner_children_are_refused_not_given_the_parents_authority(runtime_factory):
    env, srv, rt = runtime_factory(with_spawner=False)
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    p = settle(env, rt, "t-child", c)
    assert p["status"] == "denied" and p["reason"] == "spawn_refused" and env.crm.calls == []


def test_the_child_credential_never_leaves_the_bridge(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    settle(env, rt, "t-child", c)
    cred = next(iter(env.lineage._creds.values()))
    wire = str(rt.results) + str(rt.rejected)
    assert cred not in wire and cred not in json.dumps([e.payload for e in env.journal.all()])
    rows = json.dumps([dict(r) for r in env.store._conn().execute("select * from actions")]) + json.dumps([dict(r) for r in env.store._conn().execute("select * from lineage")])
    assert cred not in rows


def test_a_restarted_bridge_renews_the_child_credential(runtime_factory, clock):
    from conftest import restart
    from redevops_infinity import RapHttpServer
    env, srv, rt = runtime_factory()
    _, c = child_call(env, rt, "lookup_lead", LOOK)
    settle(env, rt, "t-child", c)
    old = next(iter(env.lineage._creds.values()))
    env2 = restart(env, rt, clock)
    srv2 = RapHttpServer(env2.bridge).start()
    try:
        rt.port = srv2.port
        _, c2 = child_call(env2, rt, "lookup_lead", LOOK, cid="again")
        assert settle(env2, rt, "t-child", c2)["status"] == "completed"
        new = next(iter(env2.lineage._creds.values()))
        assert new != old
        assert env2.spawner.verifier().verify(old, tenant=env2.tenant, client="runtime-key-1").ok is False      # the old token stopped working at once
    finally:
        srv2.stop()
