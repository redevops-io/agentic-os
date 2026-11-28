"""A long action approved by a person runs as a job: the decision returns with the job's id and state, the result is
read by that id, nothing runs twice, and a job whose worker is gone is not run again."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi")

from agentic_os.governed_writes import service as S  # noqa: E402
from tests.test_agent_gateway_media import APPROVER, world  # noqa: E402,F401


@pytest.fixture
def slow(world, monkeypatch):  # noqa: F811
    """A model that renders only when told to: every generate call waits for ``release``."""
    w = world
    w.studio.backend = "local-model"
    release, entered = threading.Event(), threading.Event()
    real = w.studio.handle

    def handle(request):
        if request.url.path == "/invoke" and b'"visual.generate"' in request.read():
            entered.set()
            assert release.wait(30), "the test never released the render"
        return real(request)
    writes = w.client.app.state.governed_writes
    monkeypatch.setattr(writes.writers["vibexgen"], "transport", __import__("httpx").MockTransport(handle))
    monkeypatch.setattr(writes, "inline_wait_s", 0.2)
    yield type("Slow", (), {"w": w, "writes": writes, "release": release, "entered": entered})
    release.set()


def generates(w):
    return [c for c in w.studio.calls if c[0] == "visual.generate"]


def shown(w, rid):
    return w.client.get(f"/api/writes/{rid}", headers=APPROVER).json()


def approve(w, asked):
    return w.client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER,
                         json={"approved": True, "digest": shown(w, asked["request_id"])["digest"]})


def test_the_decision_returns_a_job_at_once_and_the_result_is_read_by_its_id(slow):
    w = slow.w
    asked = w.ok("media.request_generate", kind="video", prompt="a lighthouse")
    digest = shown(w, asked["request_id"])["digest"]
    out = approve(w, asked)
    assert out.status_code == 200 and slow.entered.wait(5)
    job = out.json()
    assert job["state"] == "deciding" and job["job"] == {**job["job"], "job_id": asked["request_id"], "state": "running"}
    # while it runs, others read and propose: nothing waits for the render
    assert w.ok("media.request_status", request_id=asked["request_id"])["state"] == "deciding"
    assert w.ok("media.request_generate", kind="video", prompt="another")["state"] == "pending"
    # the same approval again decides nothing and runs nothing
    again = w.client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER, json={"approved": True, "digest": digest})
    assert again.status_code == 409
    slow.release.set()
    assert slow.writes.wait_for(asked["request_id"], 30)
    done = w.ok("media.request_status", request_id=asked["request_id"])
    assert done["state"] == "executed_verified" and "job" not in done and len(generates(w)) == 1, done


def test_a_running_job_that_beats_stays_running_and_one_whose_worker_is_gone_is_not_run_again(slow, monkeypatch):
    w = slow.w
    asked = w.ok("media.request_generate", kind="video", prompt="a lighthouse")
    assert approve(w, asked).json()["state"] == "deciding" and slow.entered.wait(5)
    clock = slow.writes.clock
    # far past the limit for an inline decision, but beating: still running
    monkeypatch.setattr(slow.writes, "clock", lambda: clock() + S._STALE_BEAT_S - 30)
    assert shown(w, asked["request_id"])["state"] == "deciding"
    # no beat for longer than a live worker ever leaves: the service was stopped; the outcome is unknown
    monkeypatch.setattr(slow.writes, "clock", lambda: clock() + S._STALE_BEAT_S + 60)
    lost = shown(w, asked["request_id"])
    assert lost["state"] == "outcome_unknown" and "check the application" in lost["note"]
    monkeypatch.setattr(slow.writes, "clock", clock)
    retry = w.client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER, json={"approved": True, "digest": "0" * 64})
    assert retry.status_code == 409 and len(generates(w)) == 0            # the render had not finished, and it is not started again
    slow.release.set()
    slow.writes.wait_for(asked["request_id"], 30)
    assert len(generates(w)) == 1 and shown(w, asked["request_id"])["state"] == "outcome_unknown"   # a late end does not rewrite it


def test_the_signed_in_page_shows_a_running_job_and_reloads_itself(slow):
    w = slow.w
    asked = w.ok("media.request_generate", kind="video", prompt="a lighthouse")
    assert approve(w, asked).json()["state"] == "deciding"
    from fastapi.testclient import TestClient
    browser = TestClient(w.client.app, follow_redirects=True)
    token = APPROVER["Authorization"].split()[1]
    assert browser.post("/approvals/signin", data={"token": token, "next": f"/approvals/writes/{asked['request_id']}"}).status_code == 200
    page = browser.get(f"/approvals/writes/{asked['request_id']}").text
    assert "Running" in page and asked["request_id"] in page and 'http-equiv="refresh"' in page
    slow.release.set()
    slow.writes.wait_for(asked["request_id"], 30)
    done = browser.get(f"/approvals/writes/{asked['request_id']}").text
    assert "Done and verified" in done and 'http-equiv="refresh"' not in done


def test_rejecting_a_long_action_runs_nothing_and_starts_no_job(slow):
    w = slow.w
    asked = w.ok("media.request_generate", kind="video", prompt="a lighthouse")
    out = w.client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER,
                        json={"approved": False, "digest": shown(w, asked["request_id"])["digest"]}).json()
    assert out["state"] == "rejected" and "job" not in out and not generates(w) and not slow.writes._jobs


def test_the_request_database_is_safe_for_jobs_running_beside_requests(world):  # noqa: F811
    """Jobs run on their own threads beside request threads, all on the service's one connection: unserialized, sqlite3
    raises InterfaceError or answers a lookup with nothing, and a job ends 'outcome unknown' though nothing was wrong."""
    writes = world.client.app.state.governed_writes
    world.studio.backend = "local-model"
    asked = [world.ok("media.request_generate", kind="video", prompt=f"clip {n}")["request_id"] for n in range(4)]
    failures: list = []
    stop = threading.Event()

    def read(rid):
        while not stop.is_set():
            try:
                row = writes._row(rid)
                assert row is not None and row["id"] == rid
                writes.preauthorizations.list(tenant="acme")
                writes._db.execute("UPDATE requests SET heartbeat=heartbeat WHERE id=?", (rid,))
            except Exception as e:  # noqa: BLE001
                failures.append(repr(e)[:200])
                return
    threads = [threading.Thread(target=read, args=(rid,)) for rid in asked for _ in range(2)]
    [t.start() for t in threads]
    try:
        for _ in range(40):
            if failures:
                break
            writes.list(tenant="acme", limit=50, for_approver=True)
    finally:
        stop.set()
        [t.join() for t in threads]
    assert not failures, failures[:3]
