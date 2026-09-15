"""P2 — the whole two-domain loop driven over HTTP through the unified-desktop server, and the
credential-invisibility invariant enforced across EVERY endpoint the shell can read.
"""
from __future__ import annotations

import json
import os
import tempfile

# UNIFIED_STORE must exist before importing the server (it builds its runtime at import). The credential
# values are set per-test below: an autouse conftest fixture (_no_live_cores) deletes core creds before
# every test, so they must be (re)set inside the test body, after that fixture runs.
_TW = "TWENTY-SECRET-http-abc"
_CW = "CHATWOOT-SECRET-http-xyz"
os.environ["UNIFIED_STORE"] = os.path.join(tempfile.mkdtemp(), "srv_events.jsonl")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.mission import unified_server  # noqa: E402

client = TestClient(unified_server.app)


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("TWENTY_API_KEY", _TW)
    monkeypatch.setenv("CHATWOOT_API_TOKEN", _CW)  # cores left unset -> unroutable -> in-memory projection


def test_full_loop_over_http_and_credential_invisibility(creds):
    seen = []  # every response body the shell could read → invisibility sweep

    conns = client.get("/api/connections").json(); seen.append(conns)
    doms = {c["domain"]: c for c in conns["connections"]}
    assert doms["CRM"]["connected"] and doms["Support"]["connected"]

    # Sidekick NL intent seeds the governed two-domain Mission, paused at the approval gate
    sk = client.post("/api/sidekick", json={"text": "sync the pilot account from revenue into support"}).json()
    seen.append(sk)
    mid = sk["mission_id"]
    assert mid and sk["state"] == "waiting_human"

    # Needs You surfaces exactly the Support write for approval
    nu = client.get("/api/needs-you").json(); seen.append(nu)
    assert len(nu["items"]) == 1 and nu["items"][0]["capability"] == "support.contact.upsert"
    node_id = nu["items"][0]["node_id"]

    head = client.get(f"/api/missions/{mid}").json(); seen.append(head)
    assert head["state"] == "waiting_human" and head["pending_human"]

    seen.append(client.get(f"/api/missions/{mid}/events").json())

    # Approve → resume → the Support side effect runs
    after = client.post(f"/api/missions/{mid}/approve", json={"node_id": node_id, "decision": "approve"}).json()
    seen.append(after)
    assert after["state"] == "succeeded"

    receipt = client.get(f"/api/missions/{mid}/receipt").json(); seen.append(receipt)
    assert receipt["outcome"]["success"] is True
    assert {"crm.read_account", "support.contact.upsert"} <= {s["capability"] for s in receipt["steps"]}

    seen.append(client.get(f"/api/missions/{mid}/evidence").json())

    # the shell HTML serves and is credential-free
    html = client.get("/").text
    assert "Sidekick" in html and _TW not in html and _CW not in html

    # invariant — no secret value in ANY endpoint the UI reads
    blob = json.dumps(seen, default=str)
    assert _TW not in blob and _CW not in blob


def test_shell_has_only_the_five_surfaces_no_legacy_nav():
    html = client.get("/").text
    for surface in ("Sidekick", "Needs You", "Mission", "Evidence", "Action Receipt"):
        assert surface in html
    # no legacy/product navigation chrome in the minimal shell
    for forbidden in ("Settings", "Dashboard", "navbar", "<nav"):
        assert forbidden not in html
