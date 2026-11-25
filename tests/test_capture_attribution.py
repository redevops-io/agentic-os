"""Who captured it: an authenticated tenant identity stays the actor through intake, its history, the dashboards hand-off and
Today's decision record; tenants never see each other's intake; a real deployment's intake survives a restart."""
import json

import pytest
from fastapi.testclient import TestClient

from agentic_os.capture import CaptureStore, Intake
from agentic_os.today import Today

T_ACME, T_GLOBEX, A_ACME = "a" * 40, "b" * 40, "c" * 40


def _client(monkeypatch, tmp_path, *, provider="registered", store=True):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_CONTROL_TOKEN", "OPERATOR_BASIC_AUTH", "AGENTIC_OS_PRODUCTS",
              "AGENTIC_OS_DASHBOARDS", "PROJECTS_OPERATOR_GATE", "AGENTIC_OS_CAPTURE_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PROJECTS_UI_DIR", "/nonexistent")
    monkeypatch.setenv("PROJECTS_PROVIDER", provider)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={T_ACME},globex={T_GLOBEX}")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({t: {"policy_refs": ["projects:write", "dashboards:*", "project:*"]} for t in ("acme", "globex")}))
    if store:
        monkeypatch.setenv("AGENTIC_OS_CAPTURE_DB", str(tmp_path / "capture.sqlite"))
    from agentic_os.projects_api import create_app
    return TestClient(create_app())


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_intake_records_the_actor_of_each_call_on_the_item_and_on_every_event():
    intake = Intake(Today(tenant="acme", project="p", sample_cases=False))
    got = intake.capture("Investigate why invoice 123 is unpaid", actor="requester:acme")
    assert got["actor_id"] == "requester:acme" and got["tenant_id"] == "acme"
    assert {e["actor"] for e in got["events"]} == {"requester:acme"}
    done = intake.archive(got["id"], actor="approver:acme")
    assert done["actor_id"] == "requester:acme"                              # who captured it never changes
    assert done["events"][-1]["kind"] == "archived" and done["events"][-1]["actor"] == "approver:acme"


def test_a_tenant_bearer_is_the_actor_of_its_capture_not_the_built_in_operator(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    pid = c.get("/api/projects").json()[0]["id"]
    r = c.post(f"/api/projects/{pid}/capture", json={"content": "Investigate why invoice 123 is unpaid", "source": "manual"}, headers=_h(T_ACME))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["actor_id"] == "requester:acme" and got["tenant_id"] == "acme"
    assert "user:operator" not in json.dumps(got)
    assert all(e["actor"] == "requester:acme" for e in got["events"])
    # the other tenant has its own intake: it does not see the item and cannot act on it
    assert c.get(f"/api/projects/{pid}/capture", headers=_h(T_GLOBEX)).json() == []
    assert c.post(f"/api/projects/{pid}/capture/{got['id']}/archive", headers=_h(T_GLOBEX)).status_code == 404
    mine = c.get(f"/api/projects/{pid}/capture", headers=_h(T_ACME)).json()
    assert [i["id"] for i in mine] == [got["id"]]


def test_a_real_deployment_offers_no_intake_to_an_anonymous_caller_or_without_a_durable_store(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    pid = c.get("/api/projects").json()[0]["id"]
    assert c.post(f"/api/projects/{pid}/capture", json={"content": "note", "source": "manual"}).status_code in (401, 403, 501)
    assert c.get(f"/api/projects/{pid}/today").json()["supports"]["capture"] is False
    assert c.get(f"/api/projects/{pid}/today", headers=_h(T_ACME)).json()["supports"]["capture"] is True
    c2 = _client(monkeypatch, tmp_path, store=False)
    assert c2.post(f"/api/projects/{pid}/capture", json={"content": "note", "source": "manual"}, headers=_h(T_ACME)).status_code == 501


def test_the_durable_intake_survives_a_restart_with_its_actor_and_history(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    pid = c.get("/api/projects").json()[0]["id"]
    got = c.post(f"/api/projects/{pid}/capture", json={"content": "Investigate why invoice 123 is unpaid", "source": "manual"}, headers=_h(T_ACME)).json()
    again = _client(monkeypatch, tmp_path)                                   # a new process over the same file
    back = again.get(f"/api/projects/{pid}/capture", headers=_h(T_ACME)).json()
    assert [(i["id"], i["actor_id"], i["tenant_id"]) for i in back] == [(got["id"], "requester:acme", "acme")]
    assert back[0]["events"] == got["events"]
    assert again.get(f"/api/projects/{pid}/capture", headers=_h(T_GLOBEX)).json() == []
    rows = CaptureStore(str(tmp_path / "capture.sqlite")).load("globex", pid)
    assert rows == []


def test_in_the_sample_sandbox_an_authenticated_caller_is_still_named(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, provider="sample", store=False)
    pid = c.get("/api/projects").json()[0]["id"]
    got = c.post(f"/api/projects/{pid}/capture", json={"content": "Remind me to call John tomorrow", "source": "manual"}, headers=_h(T_ACME)).json()
    assert got["actor_id"] == "requester:acme" and got["tenant_id"] == "acme"
    anon = c.post(f"/api/projects/{pid}/capture", json={"content": "Remind me to call John tomorrow", "source": "manual"}).json()
    assert anon["actor_id"] == "user:operator" and anon["tenant_id"] == "demo" and anon["id"] != got["id"]


def test_many_requests_that_open_the_store_and_the_ledger_for_the_first_time_all_succeed(tmp_path, monkeypatch):
    """The first page load of a fresh install sends several requests at once. Each used to open the capture store itself; two opens
    of a new file collided ("database is locked") and the Today feed answered 500 on some machines."""
    import threading
    from agentic_os.capture import CaptureStore
    errors = []
    for round_ in range(60):
        path = str(tmp_path / f"c{round_}.db"); gate = threading.Barrier(8)

        def first_open():
            gate.wait()
            try:
                CaptureStore(path).load("t", "p")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=first_open) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == []
