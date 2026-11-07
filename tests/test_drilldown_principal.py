"""The drill-down service verifies the principal it is given (forged, replayed, foreign-issuer, other-tenant), reports the log's own integrity
checks in the Timeline, and sees events another process appended. The permission plane is the real one; the missions are real runtime logs."""
from __future__ import annotations

import time

import jsonschema
import pytest

from agentic_os.drilldown import DrilldownRefused, DrilldownService, timeline_schema
from agentic_os.evidence_query import MemoryAuditSink
from agentic_os.mission.principal import ALL_TENANTS, PrincipalIssuer, default_issuer
from agentic_os.mission.store import EventStore, MemoryEventStore
from agentic_os.overlays import Principal
from tests.drilldown_support import SCOPE, completed_mission, ceiling_for, make_service, rewrite_jsonl_line, set_env


@pytest.fixture()
def world(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, sink, perm = make_service(store, tmp_path)
    return type("W", (), dict(store=store, svc=svc, sink=sink, perm=perm, mid=mid, path=path, tmp=tmp_path))


def issue(pid="owner", tenant="default", method="test-login", issuer=None):
    return (issuer or default_issuer()).issue(Principal(pid, "user", (), tenant), method=method)


def refused(world, principal, code="unauthenticated"):
    with pytest.raises(DrilldownRefused) as e:
        world.svc.timeline(principal, world.mid)
    assert e.value.code == code
    return e.value


# ───────────────────────── forged / replayed / foreign principals ─────────────────────────
def test_a_plain_principal_is_not_an_authenticated_one(world):
    refused(world, Principal("owner", "user", ()))
    refused(world, None)


def test_a_principal_with_a_bad_proof_or_edited_fields_is_refused(world):
    good = issue()
    from dataclasses import replace
    refused(world, replace(good, proof="0" * 64))
    refused(world, replace(good, id="somebody-else"))                      # the proof was for "owner"
    refused(world, replace(good, tenant=ALL_TENANTS))                      # widening the tenant breaks the proof
    refused(world, replace(good, roles=("admin",)))
    refused(world, replace(good, method="control-token"))


def test_a_principal_from_another_issuer_is_refused(world):
    refused(world, issue(issuer=PrincipalIssuer("somewhere-else", key=b"k" * 32)))
    refused(world, issue(issuer=PrincipalIssuer("control-plane", key=b"k" * 32)))        # same name, other key


def test_a_stale_principal_is_a_replay_and_is_refused(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    ceiling, _ = ceiling_for(tmp_path)
    now = {"t": time.time()}
    svc = DrilldownService(store, ceiling, MemoryAuditSink(), scope_for=lambda m: SCOPE, clock=lambda: now["t"], principal_max_age_s=60)
    p = issue()
    assert svc.timeline(p, mid)["mission_id"] == mid
    now["t"] += 61
    with pytest.raises(DrilldownRefused) as e:
        svc.timeline(p, mid)
    assert e.value.code == "unauthenticated"
    now["t"] = time.time()
    svc.timeline(issue(), mid)                                              # a freshly authenticated one is fine


def test_a_refused_principal_reads_nothing_and_is_recorded_without_trusting_its_claims(world):
    reads = []
    real = world.store.for_mission
    world.store.for_mission = lambda m: (reads.append(m), real(m))[1]
    from dataclasses import replace
    refused(world, replace(issue(), proof="0" * 64, id="claims-to-be-admin"))
    assert reads == []
    last = world.sink.receipts[-1]
    assert last["outcome"] == "refused" and last["reason"] == "unauthenticated" and last["principal"] == ""      # the unverified id is not written down
    assert "claims-to-be-admin" not in repr(world.sink.receipts)


def test_every_surface_verifies_the_principal(world):
    p = Principal("owner", "user", ())
    with pytest.raises(DrilldownRefused):
        world.svc.evidence_list(p, world.mid)
    with pytest.raises(DrilldownRefused):
        world.svc.evidence_inspect(p, world.mid, "x")


def test_an_injected_verifier_is_the_authority_on_what_authenticated_means(world):
    seen = []
    svc = DrilldownService(world.store, world.svc.ceiling, MemoryAuditSink(), scope_for=lambda m: SCOPE, verifier=lambda p: (seen.append(p), False)[1])
    with pytest.raises(DrilldownRefused):
        svc.timeline(issue(), world.mid)
    assert len(seen) == 1


# ───────────────────────── tenant ─────────────────────────
def test_a_mission_recorded_under_a_tenant_is_invisible_to_another_tenants_principal(world):
    world.store.append("MissionCreated", "m-acme", {"goal": "g", "tenant": "acme", "policy_refs": []})
    svc, _, _ = make_service(world.store, world.tmp / "t", scope_of=lambda m: SCOPE)
    assert svc.timeline(issue(tenant="acme"), "m-acme")["mission_id"] == "m-acme"
    assert svc.timeline(issue(tenant=ALL_TENANTS), "m-acme")["mission_id"] == "m-acme"
    for other in ("beta", "default"):
        with pytest.raises(DrilldownRefused) as e:
            svc.timeline(issue(tenant=other), "m-acme")
        assert e.value.code == "not_found"
        with pytest.raises(DrilldownRefused) as e2:
            svc.timeline(issue(tenant=other), "m-never-existed")
        assert str(e.value) == str(e2.value)


def test_a_mission_with_no_recorded_tenant_is_open_to_any_tenant_that_holds_the_grant(world):
    for t in ("default", "acme", ALL_TENANTS):
        assert world.svc.timeline(issue(tenant=t), world.mid)["mission_id"] == world.mid


# ───────────────────────── freshness: another process appends to the same log ─────────────────────────
def test_events_another_writer_appended_are_visible_without_restarting_the_reader(world):
    before = world.svc.timeline(issue(), world.mid)["head"]["event_count"]
    writer = EventStore(path=world.path)
    writer.append("ReflectionEmitted", world.mid, {"note": "later"})
    after = world.svc.timeline(issue(), world.mid)["head"]["event_count"]
    assert after == before + 1


# ───────────────────────── the Timeline reports the integrity checks the store itself ran ─────────────────────────
def test_timeline_carries_the_chain_and_anchor_status_from_the_store(world):
    t = world.svc.timeline(issue(), world.mid, limit=500)
    jsonschema.validate(t, timeline_schema())
    c = t["chain"]
    assert c["ok"] is True and c["checked"] == t["head"]["event_count"] and c["unchained"] == 0
    assert c["scope"] == "this mission" and c["anchored"] is True and c["anchor"]["status"] == "ok"


def test_a_log_ahead_of_its_anchor_is_not_reported_as_anchored(world):
    EventStore(path=world.path, anchor=False).append("ReflectionEmitted", world.mid, {"note": "no anchor update"})
    c = world.svc.timeline(issue(), world.mid, limit=500)["chain"]
    assert c["ok"] is True and c["anchored"] is False and c["anchor"]["status"] == "lagging"


def test_a_truncated_log_is_reported_not_ok_and_not_hidden(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    lines = open(path, "rb").read().splitlines(keepends=True)
    open(path, "wb").write(b"".join(lines[:-1]))
    store = EventStore(path=path, strict=False)
    svc, _, _ = make_service(store, tmp_path)
    t = svc.timeline(issue(), mid, limit=500)
    jsonschema.validate(t, timeline_schema())
    assert t["chain"]["ok"] is False and t["chain"]["anchor"]["status"] == "truncated" and t["chain"]["anchored"] is False


def test_an_edited_event_is_reported_by_the_chain_check(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    rewrite_jsonl_line(path, 3, lambda d: d["payload"].update({"edited": True}))
    store = EventStore(path=path, strict=False)
    svc, _, _ = make_service(store, tmp_path)
    c = svc.timeline(issue(), mid, limit=500)["chain"]
    assert c["ok"] is False


def test_a_store_that_cannot_check_itself_reports_no_chain(world):
    class NoCheck:
        def __init__(self, inner):
            self._i = inner

        def for_mission(self, m):
            return self._i.for_mission(m)

        def mission_ids(self):
            return self._i.mission_ids()
    svc = DrilldownService(NoCheck(world.store), world.svc.ceiling, MemoryAuditSink(), scope_for=lambda m: SCOPE)
    t = svc.timeline(issue(), world.mid, limit=500)
    assert "chain" not in t
    jsonschema.validate(t, timeline_schema())


def test_an_in_memory_store_reports_a_chain_but_no_anchor(tmp_path, monkeypatch):
    set_env(monkeypatch)
    mem = MemoryEventStore()
    mem.append("MissionCreated", "m1", {"goal": "g", "policy_refs": []})
    svc, _, _ = make_service(mem, tmp_path, scope_of=lambda m: SCOPE)
    c = svc.timeline(issue(), "m1")["chain"]
    assert c["ok"] is True and c["anchored"] is False and c["anchor"]["status"] == "unanchored" and c["checked"] == 1
