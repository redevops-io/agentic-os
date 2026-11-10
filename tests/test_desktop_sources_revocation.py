"""Revocation of desktop source registrations: eager, checked at every read, never persisted.

Owner requirement (binding): a source registration and its capability are SESSION-SCOPED and NONPERSISTENT; they are INVALIDATED when the
capability session ends (sign-out, expiry, the gate's store dropping it, process shutdown); REMOVE revokes access IMMEDIATELY, it does not merely hide
the registration.

What is tested here (``agentic_os/desktop_sources.py``, ``agentic_os/local_capability.py``, ``docs/DESKTOP_SOURCE_REGISTRATION.md``):

* eager invalidation: nothing waits for a later request (synchronous in the sign-out path, a sweeper for expiry, the shutdown hook);
* read-time authority: every access path asks, at the moment of the read, whether the registration exists and belongs to a LIVE session object
  (identity, not an id), and a read that straddles a revocation returns nothing;
* no persistence: no file, environment variable or log can restore a registration or its authority;
* the negative cases: removed / signed-out / expired / restarted, forged / other-session / replayed requests, races, audit unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic_os import desktop_sources as ds
from agentic_os.local_capability import LocalCapability, b64url
from agentic_os.projects_api import SampleProjectionProvider, create_app
from tests.test_desktop_sources import BASE, HOST, PORT, PROJECT, World

GW = "gw-token-revocation-test-0123456789"


class Recorder:
    """An Indexer that remembers what it was asked to index and to drop (stands in for an index that holds scanned content)."""

    def __init__(self):
        self.indexed, self.dropped, self.fail = [], [], False
        self.live = set()

    def index(self, project_id, source_id, paths):
        self.indexed.append((project_id, source_id, tuple(paths)))
        self.live.add((project_id, source_id))
        return len(list(paths))

    def drop(self, project_id, source_id):
        if self.fail:
            raise OSError("disk says no")
        self.dropped.append((project_id, source_id))
        self.live.discard((project_id, source_id))


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.rec = Recorder()
    world.authority.indexer = world.rec
    return world


def signout(w, client=None, csrf=None):
    return (client or w.client).delete("/api/local/session", headers={"X-Agentic-CSRF": csrf or w.csrf})


def sid_of(w, folder, client=None):
    return w.mine(folder, client=client)[0]["source_id"]


def regs(w):
    return list(w.authority._registrations)


def audit_pairs(w):
    return [(r["action"], r["reason"]) for r in w.audit()]


def wait_for(cond, seconds=8.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ==================================================================================================================================
# 2. eager invalidation: nothing depends on a later request noticing
# ==================================================================================================================================
def test_sign_out_revokes_before_it_returns_with_no_further_request(w):
    d = w.folder()
    w.pick(d)
    w.propose(w.folder("second"))
    sid = sid_of(w, d)
    assert signout(w).status_code == 200
    # no request has been made since: the authority's own state is already empty, and so is everything derived from it
    assert regs(w) == [] and w.authority._proposals == []
    assert (PROJECT, sid) in w.rec.dropped and w.rec.live == set()
    recs = [r for r in w.audit() if r["reason"] == "session_ended"]
    assert [r["action"] for r in recs] == ["session_end", "remove"] and recs[0]["count"] == 1 and recs[1]["source_id"] == sid


def test_the_gates_session_store_dropping_a_session_revokes_it(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    w.cap.drop_session(sess)                                   # not the HTTP route: the store itself
    assert regs(w) == [] and w.rec.live == set() and w.authority._proposals == []


def test_session_store_eviction_at_capacity_revokes_the_evicted_sessions_registrations(w):
    d = w.folder()
    w.pick(d)
    assert len(regs(w)) == 1
    for _ in range(w.cap.MAX_SESSIONS):
        w.cap.new_session(HOST)                                # the oldest session is pushed out of the store
    assert regs(w) == [] and w.rec.live == set()


def test_expiry_is_swept_by_the_authority_itself_with_no_request_at_all(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    cap = LocalCapability(port=PORT, session_ttl=2.5)
    world.cap = cap
    world.app = create_app(world.provider, capability=cap)
    world.rec = Recorder()
    world.authority.indexer = world.rec
    world.client, world.csrf = world.session(cap)
    d = world.folder()
    assert world.pick(d).status_code == 200
    assert len(regs(world)) == 1
    # now do nothing: the sweeper must notice the deadline by itself
    assert wait_for(lambda: regs(world) == []), "an expired session's registration is still held with no request to sweep it"
    # access is gone at the moment of detaching; dropping the content and writing the record follow it on the sweeper's thread
    assert wait_for(lambda: world.rec.live == set() and [r["reason"] for r in world.audit()][-2:] == ["session_ended", "session_ended"])
    assert wait_for(lambda: not world.authority._sweeper_running())        # and it does not spin for nothing once the table is empty


def test_expiry_revokes_without_the_sweeper_too_when_any_read_happens(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    sess.expires = (0.0, 0.0)                                  # the deadline passes under the table's feet
    w.authority._stop_sweeper()
    assert w.authority.live_projections_for(sess, PROJECT) == []
    assert regs(w) == [] and w.rec.live == set()


def test_shutdown_of_the_app_revokes_every_registration(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.rec = Recorder()
    world.authority.indexer = world.rec
    d = world.folder()
    with TestClient(world.app, base_url=BASE) as c:            # entering runs the app's startup, leaving runs its shutdown
        c.post("/api/local/session", json={"code": world.cap.issue_launch_code()})
        cap_hdr = c.post("/api/local/session", json={"code": world.cap.issue_launch_code()}).json()
        c.headers["X-Agentic-Session"] = cap_hdr["session_token"]
        h = {"X-Agentic-CSRF": cap_hdr["csrf_token"]}
        assert c.post("/api/sources/propose", json={"project_id": PROJECT, "text": f"use the files in {d} as context"}, headers=h).status_code == 200
        assert c.post("/api/sources/confirm", json={"project_id": PROJECT, "confirmed_by": "you", "sources": [{"kind": "files", "location": str(d)}]},
                      headers=h).status_code == 200
        assert len(regs(world)) == 1
    assert regs(world) == [] and world.rec.live == set() and world.authority._proposals == []
    assert ("remove", "shutdown") in audit_pairs(world)


def test_the_capabilitys_own_purge_of_an_expired_session_revokes_at_that_moment(w):
    d = w.folder()
    w.pick(d)
    w.authority._stop_sweeper()                                # only the capability's report can act now
    regs(w)[0].session.expires = (0.0, 0.0)
    assert w.cap.sweep_expired() == 1
    assert regs(w) == [] and w.rec.live == set() and w.authority._proposals == []


def test_any_request_that_makes_the_gate_purge_an_expired_session_revokes_its_registrations(w):
    d = w.folder()
    w.pick(d)
    c2, _ = w.session()
    w.authority._stop_sweeper()
    regs(w)[0].session.expires = (0.0, 0.0)
    assert c2.get(f"/api/projects/{PROJECT}/runtime").status_code == 200       # a different session's request: the gate purges while looking its own up
    assert regs(w) == [] and w.rec.live == set()


def test_a_session_that_vanished_without_telling_anyone_is_caught_by_the_sweep_and_by_the_sweeper(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    w.authority._stop_sweeper()
    w.cap._sessions = [(dg, s) for dg, s in w.cap._sessions if s is not sess]  # gone from the store, no notification
    assert len(regs(w)) == 1
    w.authority.sweep()
    assert regs(w) == [] and w.rec.live == set()
    # and the sweeper thread does the same on its own
    e = w.folder("e")
    c2, csrf2 = w.session()
    w.pick(e, client=c2, csrf=csrf2)
    sess2 = regs(w)[0].session
    w.cap._sessions = [(dg, s) for dg, s in w.cap._sessions if s is not sess2]
    w.authority._ensure_sweeper()
    w.authority._wake.set()
    assert wait_for(lambda: regs(w) == [])


def test_two_live_sessions_that_compare_equal_are_still_two_sessions(w):
    d = w.folder()
    w.pick(d)
    first = regs(w)[0].session
    other = w.cap.new_session(HOST)[1]
    other.csrf, other.expires, other.host = first.csrf, first.expires, first.host
    assert other == first and other is not first
    assert w.authority.live_projections_for(other, PROJECT) == []
    with pytest.raises(ds.SourceRevoked):
        w.authority.lease(other, PROJECT, regs(w)[0].source_id)


def test_the_marker_of_a_session_only_source_is_the_one_the_provider_uses():
    from agentic_os import projects_api
    assert ds.SESSION_ONLY == projects_api.SESSION_ONLY


def test_the_changed_modules_import_nothing_dynamically_and_nothing_outside_the_project_and_the_standard_library():
    """Packaging (`collect_submodules('agentic_os')`) needs no change: no `importlib`, `__import__` or import by computed name was added."""
    import ast
    import importlib.util
    allowed_third_party = {"fastapi", "starlette", "pydantic"}
    for name in ("agentic_os.desktop_sources", "agentic_os.local_capability", "agentic_os.sources_rag", "agentic_os.sources"):
        tree = ast.parse(Path(importlib.util.find_spec(name).origin).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
                pytest.fail(f"{name}: __import__")
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module":
                pytest.fail(f"{name}: import_module")
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
            for m in mods:
                top = m.split(".")[0]
                assert top in sys.stdlib_module_names or top in allowed_third_party or top in {"agentic_os", "redevops_rag", "runtime_contracts"}, (name, m)


def test_the_end_of_one_session_leaves_another_sessions_registrations_alone(w):
    d1, d2 = w.folder("one"), w.folder("two")
    w.pick(d1)
    c2, csrf2 = w.session()
    w.pick(d2, client=c2, csrf=csrf2)
    assert len(regs(w)) == 2
    signout(w)
    assert [r.real for r in regs(w)] == [os.path.realpath(d2)]
    assert len(w.mine(d2, client=c2)) == 1


# ==================================================================================================================================
# registrations are session-scoped: another session neither sees, removes nor confirms them
# ==================================================================================================================================
def test_another_live_session_does_not_see_the_registration_in_any_listing(w):
    d = w.folder()
    w.pick(d)
    c2, _ = w.session()
    assert w.mine(d) != [] and w.mine(d, client=c2) == []
    assert not any(s.get("session_only") for s in c2.get(f"/api/projects/{PROJECT}/overview").json()["sources"])
    assert any(s.get("session_only") for s in w.client.get(f"/api/projects/{PROJECT}/overview").json()["sources"])


def test_removing_another_sessions_source_is_the_same_404_as_an_unknown_one_and_changes_nothing(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    c2, csrf2 = w.session()
    other = w.remove(sid, client=c2, csrf=csrf2)
    unknown = w.remove("src-000000000000-1", client=c2, csrf=csrf2)
    assert other.status_code == unknown.status_code == 404
    assert other.json() == unknown.json() and other.text == unknown.text
    assert len(w.mine(d)) == 1 and w.rec.dropped == []
    assert [r for r in w.audit() if r["action"] == "remove"] == []


def test_a_second_session_registering_the_same_folder_does_not_replace_or_revoke_the_first(w):
    d = w.folder()
    w.pick(d)
    c2, csrf2 = w.session()
    assert w.pick(d, client=c2, csrf=csrf2).status_code == 200
    assert len(w.mine(d)) == 1 and len(w.mine(d, client=c2)) == 1
    sid = sid_of(w, d)
    assert w.remove(sid, client=c2, csrf=csrf2).status_code == 200            # each session removes its own registration of the folder
    assert len(w.mine(d)) == 1 and w.mine(d, client=c2) == []
    assert w.rec.live == {(PROJECT, sid)} and w.audit()[-1]["derived"] == "shared"      # the content the first session still holds is not dropped under it
    signout(w)
    assert w.rec.live == set()                                                # the last holder going takes it


# ==================================================================================================================================
# 3. read-time authority: the registration must exist and its session must be live at the moment of each read
# ==================================================================================================================================
def test_the_overview_and_the_listing_check_at_read_time_not_only_the_listing_route(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    assert [s for s in w.client.get(f"/api/projects/{PROJECT}/overview").json()["sources"] if s.get("session_only")]
    w.authority._stop_sweeper()
    w.cap._sessions = [(dg, s) for dg, s in w.cap._sessions if s is not sess]        # the store lost the session; nothing told the authority
    assert w.authority.live_projections_for(sess, PROJECT) == []
    assert w.provider.sources(PROJECT) == [s for s in w.provider.sources(PROJECT) if not s.get("session_only")]
    assert regs(w) == []


def test_a_look_alike_session_object_does_not_count_as_the_live_one(w):
    d = w.folder()
    w.pick(d)
    real = regs(w)[0].session
    twin = type(real)(csrf=real.csrf, expires=real.expires, host=real.host)
    assert twin == real and twin is not real
    assert w.authority.live_projections_for(twin, PROJECT) == []
    assert len(w.authority.live_projections_for(real, PROJECT)) == 1


def test_the_agent_gateway_sources_search_follows_the_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", GW)
    world = World(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", GW)
    world.app = create_app(world.provider, capability=world.cap)
    world.client, world.csrf = world.session()
    world.rec = Recorder()
    world.authority.indexer = world.rec
    gw = {"Authorization": f"Bearer {GW}"}

    def search(client, csrf):
        r = client.post("/api/agent-gateway/capabilities/sources.search/invoke", headers={**gw, "X-Agentic-CSRF": csrf},
                        json={"query": "x", "limit": 100})
        assert r.status_code == 200, r.text
        return r.text

    d = world.folder("gwdocs")
    world.pick(d)
    sid = sid_of(world, d)
    assert sid in search(world.client, world.csrf)                        # control: the owning, live session's agent can read it
    c2, csrf2 = world.session()
    assert sid not in search(c2, csrf2)                                   # another session's gateway call cannot
    world.remove(sid)
    assert sid not in search(world.client, world.csrf)                    # removed: gone from the gateway at once
    world.pick(d)
    assert sid in search(world.client, world.csrf)
    signout(world)
    c3, csrf3 = world.session()
    assert sid not in search(c3, csrf3)
    assert regs(world) == []


def test_a_listing_request_in_flight_when_the_session_ends_returns_no_registration(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    ended_midway = []

    def provider_view(project):
        return w.authority.live_projections_for(sess, project)

    first = provider_view(PROJECT)
    w.cap.drop_session(sess)
    ended_midway = provider_view(PROJECT)
    assert len(first) == 1 and ended_midway == []


def test_a_read_that_straddles_a_remove_returns_nothing(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    sid = reg.source_id

    def slow_read(view):
        assert view.source_id == sid and view.real == os.path.realpath(d)
        assert w.remove(sid).status_code == 200                           # Remove arrives while the read is in progress
        return "CONTENT-OF-THE-FOLDER"

    with pytest.raises(ds.SourceRevoked):
        w.authority.guarded_read(reg.session, PROJECT, sid, slow_read)


def test_a_read_that_straddles_a_sign_out_returns_nothing(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    with pytest.raises(ds.SourceRevoked):
        w.authority.guarded_read(reg.session, PROJECT, reg.source_id, lambda view: (signout(w), "CONTENT")[1])


def test_a_read_that_completes_before_the_remove_is_returned_and_one_that_starts_after_is_refused(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    assert w.authority.guarded_read(reg.session, PROJECT, reg.source_id, lambda view: "ok") == "ok"
    w.remove(reg.source_id)
    with pytest.raises(ds.SourceRevoked):
        w.authority.guarded_read(reg.session, PROJECT, reg.source_id, lambda view: pytest.fail("ran after Remove"))


def test_an_old_handle_is_dead_after_remove_even_when_the_same_folder_is_registered_again(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    lease = w.authority.lease(reg.session, PROJECT, reg.source_id)
    lease.check()
    w.remove(reg.source_id)
    with pytest.raises(ds.SourceRevoked):
        lease.check()
    assert w.pick(d).status_code == 200                                    # same folder, same source id: a NEW registration
    again = regs(w)[0]
    assert again.source_id == reg.source_id and again.generation > reg.generation and again is not reg
    with pytest.raises(ds.SourceRevoked):
        lease.check()                                                      # the id matches; the epoch does not
    w.authority.lease(again.session, PROJECT, again.source_id).check()


def test_a_lease_from_another_session_is_refused(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    other = w.cap.new_session(HOST)[1]
    with pytest.raises(ds.SourceRevoked):
        w.authority.lease(other, PROJECT, reg.source_id)
    with pytest.raises(ds.SourceRevoked):
        w.authority.lease(reg.session, "another-project", reg.source_id)
    with pytest.raises(ds.SourceRevoked):
        w.authority.lease(None, PROJECT, reg.source_id)


def test_remove_does_not_wait_for_a_slow_reader_and_the_reader_is_then_refused(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    inside, release = threading.Event(), threading.Event()
    outcome = {}

    def reader():
        def slow(view):
            inside.set()
            assert release.wait(10)
            return "CONTENT"
        try:
            outcome["value"] = w.authority.guarded_read(reg.session, PROJECT, reg.source_id, slow)
        except ds.SourceRevoked:
            outcome["refused"] = True

    t = threading.Thread(target=reader)
    t.start()
    assert inside.wait(10)
    started = time.monotonic()
    assert w.remove(reg.source_id).status_code == 200                       # returns while the reader is still inside its read
    assert time.monotonic() - started < 5 and regs(w) == []
    release.set()
    t.join(10)
    assert outcome == {"refused": True}


def test_a_reader_thread_started_before_sign_out_is_refused_afterwards(w):
    d = w.folder()
    w.pick(d)
    reg = regs(w)[0]
    inside, release, outcome = threading.Event(), threading.Event(), {}

    def reader():
        def slow(view):
            inside.set()
            release.wait(10)
            return "CONTENT"
        try:
            outcome["value"] = w.authority.guarded_read(reg.session, PROJECT, reg.source_id, slow)
        except ds.SourceRevoked:
            outcome["refused"] = True

    t = threading.Thread(target=reader)
    t.start()
    inside.wait(10)
    signout(w)
    release.set()
    t.join(10)
    assert outcome == {"refused": True}


class FakeRag:
    def __init__(self, path):
        self.path, self.closed = path, False
        Path(path).write_text("indexed content of the folder")

    def index(self, path):
        return {"chunks": 3}

    def search(self, query, k=8):
        return [{"filename": "a.pdf", "text": "secret passage"}]


def test_the_rag_index_is_dropped_and_its_retrieval_is_gated_by_the_registration(w, tmp_path):
    from agentic_os.sources_rag import RagIndexer
    db = tmp_path / "rag"
    rag = RagIndexer(db_dir=str(db), rag_factory=FakeRag)
    w.authority.indexer = rag
    d = w.folder()
    assert w.pick(d).status_code == 200
    reg = regs(w)[0]
    assert rag.authorize is not None                                               # the authority bound its own check to the index it was given
    with pytest.raises(ds.SourceRevoked):
        rag.retrieve(PROJECT, reg.source_id, "anything")                           # outside any request there is no session: refused
    with ds.acting_as(reg.session):
        ev = rag.retrieve(PROJECT, reg.source_id, "anything")
        assert ev.record_count == 1 and list(db.iterdir()) != []                   # control: it retrieves, and content is on disk
        w.remove(reg.source_id)
        assert list(db.iterdir()) == []                                            # the index file is gone
        with pytest.raises(ds.SourceRevoked):
            rag.retrieve(PROJECT, reg.source_id, "anything")
    assert rag._stores == {}
    with ds.acting_as(w.cap.new_session(HOST)[1]):
        with pytest.raises(ds.SourceRevoked):
            rag.retrieve(PROJECT, reg.source_id, "anything")                       # and not for another session either


def test_a_retrieval_that_straddles_the_remove_returns_nothing(w, tmp_path):
    from agentic_os.sources_rag import RagIndexer

    class SlowRag(FakeRag):
        def search(self, query, k=8):
            w.remove(regs(w)[0].source_id)                                        # Remove lands in the middle of the search
            return [{"filename": "a.pdf", "text": "secret passage"}]

    rag = RagIndexer(db_dir=str(tmp_path / "rag2"), rag_factory=SlowRag)
    w.authority.indexer = rag
    w.pick(w.folder())
    reg = regs(w)[0]
    with ds.acting_as(reg.session), pytest.raises(ds.SourceRevoked):
        rag.retrieve(PROJECT, reg.source_id, "q")


# ==================================================================================================================================
# remove: effect before it returns, derived data dropped, audited
# ==================================================================================================================================
def test_remove_drops_the_scanned_content_the_proposal_and_audits_it(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    assert w.rec.live == {(PROJECT, sid)}
    assert w.remove(sid).status_code == 200
    assert regs(w) == [] and w.rec.live == set() and w.rec.dropped == [(PROJECT, sid)]
    assert audit_pairs(w)[-1] == ("remove", "user_request")
    assert w.confirm(d).json()["detail"]["code"] == "not_proposed"           # the pick that allowed the registration went with it


def test_remove_still_revokes_when_dropping_the_derived_content_fails(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    w.rec.fail = True
    r = w.remove(sid)
    assert r.status_code == 200 and w.mine(d) == [] and regs(w) == []
    assert w.audit()[-1]["derived"] == "drop_failed"


def test_double_remove_is_a_404_and_one_audit_record(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    assert w.remove(sid).status_code == 200
    assert w.remove(sid).status_code == 404
    assert len([r for r in w.audit() if r["action"] == "remove"]) == 1 and w.rec.dropped == [(PROJECT, sid)]


# ==================================================================================================================================
# in-flight confirm
# ==================================================================================================================================
def test_signing_out_during_an_in_flight_confirm_registers_nothing_and_drops_what_was_scanned(w, monkeypatch):
    d = w.folder()
    w.propose(d)
    import agentic_os.projects_api as pa
    real = pa.confirm_and_connect_sources

    def scan_then_sign_out(*a, **k):
        out = real(*a, **k)
        assert signout(w).status_code == 200                               # the user signs out while the folder is being scanned
        return out

    monkeypatch.setattr(pa, "confirm_and_connect_sources", scan_then_sign_out)
    r = w.confirm(d)
    assert r.status_code == 401, r.text
    assert regs(w) == [] and w.rec.live == set()
    assert [x for x in w.audit() if x["action"] == "register"] == []
    assert w.provider.session_sources == {} or not any(w.provider.session_sources.values())


def test_signing_out_between_the_audit_record_and_the_registration_leaves_nothing_and_says_so(w, monkeypatch):
    d = w.folder()
    w.propose(d)
    real_append = w.authority.audit.append

    def append_then_sign_out(receipt):
        out = real_append(receipt)
        if receipt.get("action") == "register":
            signout(w)
        return out

    monkeypatch.setattr(w.authority.audit, "append", append_then_sign_out)
    r = w.confirm(d)
    assert r.status_code == 401
    assert regs(w) == [] and w.rec.live == set()
    assert ("register", "ok") in audit_pairs(w) and audit_pairs(w)[-1] == ("remove", "session_ended")


def test_a_remove_arriving_while_a_confirm_is_in_flight_finds_nothing_and_the_confirm_then_completes(w, monkeypatch):
    d = w.folder()
    w.propose(d)
    import agentic_os.projects_api as pa
    real = pa.confirm_and_connect_sources
    seen = {}

    def scan_then_remove(*a, **k):
        out = real(*a, **k)
        seen["remove"] = w.remove(out[0]["source_id"])                       # the id the scan produced is not registered yet
        return out

    monkeypatch.setattr(pa, "confirm_and_connect_sources", scan_then_remove)
    r = w.confirm(d)
    assert r.status_code == 200 and seen["remove"].status_code == 404
    assert len(w.mine(d)) == 1 and w.rec.live != set()


def test_a_remove_between_the_audit_record_and_the_listing_is_a_404_and_consistent(w, monkeypatch):
    d = w.folder()
    w.propose(d)
    real_append = w.authority.audit.append
    seen = {}

    def append_then_remove(receipt):
        out = real_append(receipt)
        if receipt.get("action") == "register":
            seen["remove"] = w.remove(receipt["source_id"])
        return out

    monkeypatch.setattr(w.authority.audit, "append", append_then_remove)
    assert w.confirm(d).status_code == 200
    assert seen["remove"].status_code == 404                                  # not visible until it is fully registered
    assert len(w.mine(d)) == 1


def test_a_failed_audit_unwinds_a_scanned_registration_completely(w, monkeypatch):
    d = w.folder()
    w.propose(d)
    monkeypatch.setattr(w.authority.audit, "append", lambda receipt: (_ for _ in ()).throw(ds.AuditUnavailable("OSError")))
    r = w.confirm(d)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "audit_unavailable"
    assert regs(w) == [] and w.rec.live == set() and w.mine(d) == []


# ==================================================================================================================================
# audit unavailable
# ==================================================================================================================================
def _break_audit(w):
    import shutil
    shutil.rmtree(w.audit_path.parent)
    w.audit_path.parent.write_text("not a directory")


def test_sign_out_still_revokes_and_drops_when_the_audit_cannot_be_written(w):
    d = w.folder()
    w.pick(d)
    _break_audit(w)
    assert signout(w).status_code == 200
    assert regs(w) == [] and w.rec.live == set()


def test_remove_still_revokes_when_the_audit_cannot_be_written(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    _break_audit(w)
    assert w.remove(sid).status_code == 200
    assert regs(w) == [] and w.rec.live == set()


def test_expiry_still_revokes_when_the_audit_cannot_be_written(w):
    d = w.folder()
    w.pick(d)
    sess = regs(w)[0].session
    _break_audit(w)
    sess.expires = (0.0, 0.0)
    w.authority.live_projections_for(sess, PROJECT)
    assert regs(w) == [] and w.rec.live == set()


# ==================================================================================================================================
# 4. nothing persists: restart, replays, files, environment, logs
# ==================================================================================================================================
def _files(root: Path):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _fresh_process(w):
    """What a restart is: a new capability, a new app, a new authority; the disk (home, state, tmp) is whatever the old process left."""
    cap = LocalCapability(port=PORT)
    app = create_app(SampleProjectionProvider(), capability=cap)
    c = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    j = c.post("/api/local/session", json={"code": cap.issue_launch_code()}).json()
    c.headers["X-Agentic-Session"] = j["session_token"]
    return app, cap, c, j["csrf_token"]


def test_after_a_restart_nothing_is_registered_and_nothing_on_disk_can_bring_it_back(w, monkeypatch):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    tok = b64url(w.authority._token)
    for_disk = _files(w.tmp)
    app, cap, c, csrf = _fresh_process(w)
    assert [s for s in c.get(f"/api/projects/{PROJECT}/sources").json() if s.get("session_only")] == []
    assert app.state.desktop_sources._registrations == [] and app.state.desktop_sources._proposals == []
    # the audit log from the old run is there, it records the registration, and it grants nothing
    assert any(r["action"] == "register" and r["source_id"] == sid for r in w.audit())
    assert c.post("/api/sources/remove", json={"project_id": PROJECT, "source_id": sid}, headers={"X-Agentic-CSRF": csrf}).status_code == 404
    assert c.post("/api/sources/confirm", json={"project_id": PROJECT, "confirmed_by": "you", "sources": [{"kind": "files", "location": str(d)}]},
                  headers={"X-Agentic-CSRF": csrf}).json()["detail"]["code"] == "not_proposed"
    # nothing outside the audit log and its anchor was written by any of it, and none of those files carries a credential
    new_files = {p: b for p, b in _files(w.tmp).items() if p not in for_disk or for_disk[p] != b}
    assert {p.name for p in new_files} <= {"source-registrations.jsonl", "source-registrations.jsonl.anchor"}
    for p, b in _files(w.tmp).items():
        assert tok.encode() not in b and w.csrf.encode() not in b, p
        if p.suffix not in (".jsonl", ".anchor") and p.parent != d:
            assert sid.encode() not in b, p


def test_a_forged_audit_log_cannot_grant_a_registration_to_a_new_process(w, monkeypatch):
    d = w.folder()
    w.pick(d)
    forged = w.audit_path.read_bytes()
    app, cap, c, csrf = _fresh_process(w)
    assert w.audit_path.read_bytes() == forged                               # starting did not rewrite it either
    # the new authority never parsed the log to learn anything
    assert app.state.desktop_sources._registrations == []
    reads = []
    real_open = open

    def spy(path, *a, **k):
        if str(path).endswith("source-registrations.jsonl") and (not a or "r" in a[0]):
            reads.append(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    assert c.get(f"/api/projects/{PROJECT}/sources").status_code == 200
    assert c.get(f"/api/projects/{PROJECT}/overview").status_code == 200
    assert reads == []                                                         # listing never reads the log


def test_the_audit_log_is_read_only_by_verify_and_the_append_tally(w):
    import inspect
    src = inspect.getsource(ds)
    assert src.count("open(self.path") == 2 and "_tally" in src and "verify" in src          # the only two readers; no loader of registrations
    assert "def load" not in src and "def restore" not in src and "def replay" not in src


def test_a_new_session_after_sign_out_starts_with_zero_registrations_and_needs_a_new_pick(w):
    d = w.folder()
    w.pick(d)
    signout(w)
    c2, csrf2 = w.session()
    assert [s for s in c2.get(f"/api/projects/{PROJECT}/sources").json() if s.get("session_only")] == []
    assert w.confirm(d, client=c2, csrf=csrf2).json()["detail"]["code"] == "not_proposed"


def test_a_proposal_offered_to_an_ended_session_cannot_be_confirmed_by_a_new_one(w):
    d = w.folder()
    w.propose(d)
    signout(w)
    c2, csrf2 = w.session()
    r = w.confirm(d, client=c2, csrf=csrf2)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_proposed"
    assert regs(w) == []


def test_requests_replayed_from_the_ended_session_are_refused_whatever_they_are(w):
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    old_headers = {"X-Agentic-Session": w.client.headers["X-Agentic-Session"], "X-Agentic-CSRF": w.csrf}
    signout(w)
    fresh = TestClient(w.app, base_url=BASE, raise_server_exceptions=False)
    for path, body in (("/api/sources/propose", {"project_id": PROJECT, "text": f"use the files in {d} as context"}),
                       ("/api/sources/confirm", {"project_id": PROJECT, "confirmed_by": "you", "sources": [{"kind": "files", "location": str(d)}]}),
                       ("/api/sources/remove", {"project_id": PROJECT, "source_id": sid})):
        r = fresh.post(path, json=body, headers=old_headers)
        assert r.status_code == 401, (path, r.status_code, r.text)
    assert fresh.get(f"/api/projects/{PROJECT}/sources", headers=old_headers).status_code == 401
    assert fresh.get(f"/api/projects/{PROJECT}/overview", headers=old_headers).status_code == 401
    assert regs(w) == []


def test_a_forged_scope_session_cannot_reach_the_routes_or_the_listing(w):
    from agentic_os.local_capability import Session, SESSION_SCOPE_KEY
    d = w.folder()
    w.pick(d)
    real = regs(w)[0].session
    twin = Session(csrf=real.csrf, expires=real.expires, host=real.host)
    assert w.authority.live_projections_for(twin, PROJECT) == []
    from tests.test_desktop_sources import request_with
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        w.authority.remove(request_with(None, {SESSION_SCOPE_KEY: twin}), project_id=PROJECT, source_id=regs(w)[0].source_id, prov=w.provider)
    assert e.value.status_code == 401 and len(regs(w)) == 1


def test_no_environment_variable_keychain_or_temp_file_carries_a_registration_or_the_token(w, tmp_path, monkeypatch):
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    before_env = dict(os.environ)
    mods_before = set(sys.modules)
    d = w.folder()
    w.pick(d)
    sid = sid_of(w, d)
    tok = b64url(w.authority._token)
    w.remove(sid)
    w.pick(d)
    signout(w)
    assert dict(os.environ) == before_env
    assert not [m for m in set(sys.modules) - mods_before if m.split(".")[0] in ("keyring", "keychain", "secretstorage", "win32cred")]
    assert list(tmpdir.iterdir()) == []
    for k, v in os.environ.items():
        assert tok not in v and sid not in v, k
    for p, b in _files(w.tmp).items():
        assert tok.encode() not in b, p


def test_the_token_the_session_secrets_and_the_registration_table_are_in_no_response_error_or_log(w, caplog):
    caplog.set_level(logging.DEBUG)
    d = w.folder()
    other = w.folder("other-private-folder")
    c2, csrf2 = w.session()
    w.pick(other, client=c2, csrf=csrf2)
    other_sid = sid_of(w, other, client=c2)
    seen = []                                                                  # everything THIS session's client was told

    def rec(r):
        seen.append(r.text)
        return r

    rec(w.propose(d))
    rec(w.confirm(d))
    rec(w.confirm(w.folder("never-proposed")))
    rec(w.confirm("/"))
    rec(w.remove("nope"))
    rec(w.remove(other_sid))                                                  # another session's id
    rec(w.client.get(f"/api/projects/{PROJECT}/sources"))
    rec(w.client.get(f"/api/projects/{PROJECT}/overview"))
    rec(w.client.post("/api/sidekick", json={"text": f"what do you know about {other}", "ctx": {}}))
    rec(w.client.get("/api/ui-config"))
    secrets = [b64url(w.authority._token), w.csrf, csrf2, w.client.headers["X-Agentic-Session"], c2.headers["X-Agentic-Session"]]
    blob = "\n".join(seen) + "\n" + caplog.text
    for s in secrets:
        assert s not in blob
    assert other_sid not in blob and os.path.realpath(other) not in "\n".join(seen[3:])      # nothing of the other session's source reaches this one
    assert "_registrations" not in blob and "DesktopSourceAuthority object" not in blob
    assert repr(w.authority) == "<DesktopSourceAuthority>"


# ==================================================================================================================================
# the removed source does not reappear in any later answer (Sidekick, overview, listing, gateway)
# ==================================================================================================================================
def test_a_removed_sources_name_and_path_are_in_no_later_sidekick_answer_or_listing(w):
    d = w.folder("quarterly-secret-reports")
    w.pick(d)
    sid = sid_of(w, d)
    w.remove(sid)
    seen = [w.client.post("/api/sidekick", json={"text": t, "ctx": {"project": PROJECT}}).text
            for t in ("what sources do I have", "use the files in the quarterly-secret-reports folder", "summarise quarterly-secret-reports")]
    seen.append(w.client.get(f"/api/projects/{PROJECT}/sources").text)
    seen.append(w.client.get(f"/api/projects/{PROJECT}/overview").text)
    for text in seen:
        assert sid not in text and os.path.realpath(d) not in text


# ==================================================================================================================================
# stress: readers and writers on threads; after a sign-out has returned no listing started later may show a registration
# ==================================================================================================================================
def test_listings_racing_with_register_remove_and_sign_out_never_show_a_registration_after_it_was_revoked(w):
    folders = [w.folder(f"f{i}") for i in range(4)]
    for f in folders:
        w.pick(f)
    sess = regs(w)[0].session
    stop, violations, revoked_at = threading.Event(), [], {}

    def reader():
        while not stop.is_set():
            started = time.monotonic()
            got = w.authority.live_projections_for(sess, PROJECT)
            if "t" in revoked_at and started > revoked_at["t"] and got:
                violations.append(got)

    ts = [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    time.sleep(0.05)
    signout(w)
    revoked_at["t"] = time.monotonic()
    time.sleep(0.1)
    stop.set()
    for t in ts:
        t.join(10)
    assert violations == [] and regs(w) == []
