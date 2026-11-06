"""The authorized, read-only drill-down HTTP surface: authentication, the 404-same-as-denied rule, permission revocation
mid-session, ETags, cross-scope isolation, audit, concurrency. The permission plane is the REAL one (encrypted grant
store in a temp dir), the missions are produced by the real runtime."""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("httpx")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.drilldown import DrilldownService, build_router
from agentic_os.evidence_query import JsonlAuditSink, MemoryAuditSink, SingleOwnerTokenAuthenticator
from agentic_os.mission.store import EventStore
from tests.drilldown_support import SCOPE, TOKEN, completed_mission, ceiling_for, make_service, owner, set_env

H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, sink, perm = make_service(store, tmp_path)
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    return type("W", (), dict(client=TestClient(app), mid=mid, store=store, svc=svc, sink=sink, perm=perm, app=app,
                              base=f"/api/projects/{SCOPE}/missions/{mid}", tmp=tmp_path, path=path))


def _grant_id(perm, name=f"evidence.{SCOPE}.inspect"):
    return next(g.id for g in perm.list() if g.resource_name == name)


# ───────────────────────── authentication and the denied/unknown rule ─────────────────────────
def test_no_credential_and_a_wrong_credential_are_401_with_no_content(world):
    for hdr in ({}, {"Authorization": "Bearer nope-nope-nope-nope-nope-nope"}, {"Authorization": "Basic abc"}):
        r = world.client.get(world.base + "/timeline", headers=hdr)
        assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
        assert r.json() == {"error": "authentication required"}


def test_an_authorized_owner_gets_the_timeline_evidence_and_an_item(world):
    r = world.client.get(world.base + "/timeline?limit=500", headers=H)
    assert r.status_code == 200 and r.json()["provenance"]["sample_data"] is False
    lst = world.client.get(world.base + "/evidence", headers=H)
    assert lst.status_code == 200 and lst.json()["items"]
    item = lst.json()["items"][0]
    one = world.client.get(f"{world.base}/evidence/{item['id']}", headers=H)
    assert one.status_code == 200 and one.json()["id"] == item["id"]


def test_unknown_mission_unauthorized_scope_and_wrong_project_are_byte_identical_404(world):
    cases = [
        f"/api/projects/{SCOPE}/missions/m-does-not-exist/timeline",                 # unknown mission
        f"/api/projects/other-project/missions/{world.mid}/timeline",                # real mission, wrong project (no grant there either)
        f"/api/projects/{SCOPE}/missions/..%2f..%2fetc/timeline",                    # hostile id
    ]
    bodies = []
    for url in cases:
        r = world.client.get(url, headers=H)
        assert r.status_code == 404, url
        bodies.append((r.content, sorted(k.lower() for k in r.headers if k.lower() not in {"content-length"})))
    assert len({b[0] for b in bodies}) == 1 and len({tuple(b[1]) for b in bodies}) == 1
    assert world.mid.encode() not in bodies[0][0] and b"other-project" not in bodies[0][0]


def test_a_principal_without_the_grant_gets_the_same_404_as_an_unknown_mission(world, tmp_path):
    svc, _, _ = make_service(world.store, tmp_path / "empty", grants=())
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    c = TestClient(app)
    denied = c.get(world.base + "/timeline", headers=H)
    unknown = c.get(f"/api/projects/{SCOPE}/missions/m-nope/timeline", headers=H)
    assert denied.status_code == 404 and denied.content == unknown.content


def test_the_other_actions_do_not_grant_inspect_and_a_prefix_grant_covers_it(world, tmp_path):
    for granted, expect in ((f"evidence.{SCOPE}.reconstruct", 404), (f"evidence.{SCOPE}.resume", 404), (f"evidence.{SCOPE}.reexecute", 404),
                            (f"evidence.{SCOPE}", 200), ("evidence", 200), (f"evidence.{SCOPE}.inspect", 200), ("evidence.another.inspect", 404)):
        svc, _, _ = make_service(world.store, tmp_path / granted.replace(".", "_"), grants=(("owner", granted),))
        app = FastAPI()
        app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
        assert TestClient(app).get(world.base + "/timeline", headers=H).status_code == expect, granted


def test_bad_parameters_never_reveal_whether_a_mission_exists_to_an_unauthorized_caller(world, tmp_path):
    svc, _, _ = make_service(world.store, tmp_path / "none", grants=())
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    c = TestClient(app)
    assert c.get(world.base + "/timeline?limit=999999", headers=H).status_code == 404
    assert world.client.get(world.base + "/timeline?limit=999999", headers=H).status_code == 400


# ───────────────────────── revocation mid-session ─────────────────────────
def test_revoking_the_grant_refuses_the_very_next_request_including_a_conditional_one(world):
    first = world.client.get(world.base + "/timeline", headers=H)
    assert first.status_code == 200
    etag = first.headers["ETag"]
    assert world.perm.remove(_grant_id(world.perm)) is True
    after = world.client.get(world.base + "/timeline", headers=H)
    assert after.status_code == 404
    cond = world.client.get(world.base + "/timeline", headers=dict(H, **{"If-None-Match": etag}))
    assert cond.status_code == 404                                  # a cached validator is never a way around a revoked grant
    assert world.client.get(world.base + "/evidence", headers=H).status_code == 404


def test_restoring_the_grant_restores_access(world):
    gid = _grant_id(world.perm)
    world.perm.remove(gid)
    assert world.client.get(world.base + "/timeline", headers=H).status_code == 404
    world.perm.add("user", "owner", "tool", f"evidence.{SCOPE}.inspect", actions=["read"], created_by="test")
    assert world.client.get(world.base + "/timeline", headers=H).status_code == 200


def test_a_sealed_grant_store_denies_everything(world):
    # tamper with the encrypted store: the plane fails closed
    p = world.perm.path
    p.write_bytes(p.read_bytes()[:-3] + b"xyz") if world.perm.encrypted else p.write_text("not json")
    assert world.client.get(world.base + "/timeline", headers=H).status_code == 404


def test_a_ceiling_that_raises_denies(world, tmp_path):
    class Boom:
        def allows(self, principal, permission):
            raise RuntimeError("ceiling down")
    svc = DrilldownService(world.store, Boom(), MemoryAuditSink(), scope_for=lambda m: SCOPE)
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    assert TestClient(app).get(world.base + "/timeline", headers=H).status_code == 404


# ───────────────────────── cross-scope isolation ─────────────────────────
def test_a_second_projects_mission_is_invisible_to_a_caller_granted_only_the_first(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    rt, a = completed_mission(path)
    from agentic_os.mission.unified_demo import seed_mission
    b = seed_mission(rt).id
    store = EventStore(path=path)
    scopes = {a: "tenant-a", b: "tenant-b"}
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: scopes.get(m), grants=(("owner", "evidence.tenant-a.inspect"),))
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    c = TestClient(app)
    assert c.get(f"/api/projects/tenant-a/missions/{a}/timeline", headers=H).status_code == 200
    r = c.get(f"/api/projects/tenant-b/missions/{b}/timeline", headers=H)
    assert r.status_code == 404 and b.encode() not in r.content
    assert c.get(f"/api/projects/tenant-a/missions/{b}/timeline", headers=H).status_code == 404     # b is not in tenant-a
    assert c.get(f"/api/projects/tenant-b/missions/{a}/evidence", headers=H).status_code == 404


# ───────────────────────── ETag ─────────────────────────
def test_etag_gives_304_until_the_log_grows_and_changes_with_the_page(world):
    r1 = world.client.get(world.base + "/timeline?limit=5", headers=H)
    etag = r1.headers["ETag"]
    r2 = world.client.get(world.base + "/timeline?limit=5", headers=dict(H, **{"If-None-Match": etag}))
    assert r2.status_code == 304 and r2.content == b""
    assert world.client.get(world.base + "/timeline?limit=6", headers=H).headers["ETag"] != etag
    world.store.append("MissionOutcome", world.mid, {"kind": "late", "success": True})
    r3 = world.client.get(world.base + "/timeline?limit=5", headers=dict(H, **{"If-None-Match": etag}))
    assert r3.status_code == 200 and r3.headers["ETag"] != etag
    assert r1.headers["Cache-Control"] == "private, no-cache"


# ───────────────────────── read-only surface ─────────────────────────
def test_every_route_is_a_get_and_nothing_else_is_accepted(world):
    methods = {m for r in world.app.routes for m in (getattr(r, "methods", None) or set()) if r.path.startswith("/api/")}
    assert methods <= {"GET"}, methods
    for verb in ("post", "put", "patch", "delete"):
        r = getattr(world.client, verb)(world.base + "/timeline", headers=H)
        assert r.status_code in (404, 405)
    assert len(world.store.all()) == len(EventStore(path=world.path).all())     # nothing was appended by those attempts


def test_a_request_does_not_change_the_log_file(world):
    import hashlib
    before = hashlib.sha256(open(world.path, "rb").read()).hexdigest()
    world.client.get(world.base + "/timeline?limit=500", headers=H)
    for it in world.client.get(world.base + "/evidence", headers=H).json()["items"]:
        world.client.get(f"{world.base}/evidence/{it['id']}", headers=H)
    assert hashlib.sha256(open(world.path, "rb").read()).hexdigest() == before


# ───────────────────────── audit ─────────────────────────
def test_every_read_is_audited_without_copying_the_content_and_the_chain_verifies(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    sink = JsonlAuditSink(str(tmp_path / "audit.jsonl"))
    svc, _, _ = make_service(EventStore(path=path), tmp_path, audit=sink)
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    c = TestClient(app)
    c.get(f"/api/projects/{SCOPE}/missions/{mid}/timeline", headers=H)
    c.get(f"/api/projects/{SCOPE}/missions/m-nope/timeline", headers=H)
    ok, n = sink.verify()
    assert ok and n == 2
    lines = [json.loads(l) for l in open(tmp_path / "audit.jsonl")]
    assert lines[0]["kind"] == "drilldown.timeline" and lines[0]["outcome"] == "ok" and lines[0]["side_effects"] == 0
    assert lines[0]["principal"] == "owner" and lines[0]["scope"] == SCOPE and lines[0]["lineage_sha256"].startswith("sha256:")
    assert lines[1]["outcome"] == "refused" and lines[1]["reason"] == "not_authorized"
    assert "crm.read_account" not in open(tmp_path / "audit.jsonl").read()          # the audit holds hashes and counts, not the mission content


def test_when_the_audit_log_cannot_be_written_the_request_fails_closed(tmp_path, monkeypatch):
    set_env(monkeypatch)
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)

    class Dead:
        def append(self, receipt):
            raise OSError("disk full")
    svc, _, _ = make_service(EventStore(path=path), tmp_path, audit=Dead())
    app = FastAPI()
    app.include_router(build_router(svc, SingleOwnerTokenAuthenticator(TOKEN, "owner")))
    r = TestClient(app).get(f"/api/projects/{SCOPE}/missions/{mid}/timeline", headers=H)
    assert r.status_code == 503 and "crm.read_account" not in r.text


# ───────────────────────── hygiene ─────────────────────────
def test_responses_are_json_no_store_style_and_do_not_echo_the_path_parameters(world):
    r = world.client.get(f"/api/projects/{SCOPE}/missions/<script>alert(1)</script>/timeline", headers=H)
    assert r.status_code == 404 and "<script>" not in r.text
    ok = world.client.get(world.base + "/timeline", headers=H)
    assert ok.headers["Content-Type"].startswith("application/json") and ok.headers["X-Content-Type-Options"] == "nosniff"


# ───────────────────────── concurrency ─────────────────────────
def test_concurrent_readers_during_appends_always_see_a_self_consistent_prefix(world):
    from agentic_os.evidence_query.replay import lineage_digest
    errors, stop = [], threading.Event()

    def writer():
        for i in range(200):
            world.store.append("NodeSucceeded", world.mid, {"node_id": f"x{i}", "capability": "c", "result": {"i": i}})
        stop.set()

    def reader():
        while not stop.is_set():
            r = world.client.get(world.base + "/timeline?limit=500", headers=H)
            if r.status_code != 200:
                errors.append(r.status_code)
                continue
            body = r.json()
            ents = body["entries"]
            if body["page"]["next_after_seq"] is None:
                # the lineage covers exactly the events up to head.seq, which is what was returned
                prefix = [e for e in world.store.for_mission(world.mid) if e.seq <= body["head"]["seq"]]
                if lineage_digest(prefix) != body["head"]["lineage_sha256"] or ents[-1]["seq"] != body["head"]["seq"]:
                    errors.append("inconsistent")

    ts = [threading.Thread(target=reader) for _ in range(4)] + [threading.Thread(target=writer)]
    [t.start() for t in ts]
    [t.join(timeout=120) for t in ts]
    assert not errors, errors[:5]
