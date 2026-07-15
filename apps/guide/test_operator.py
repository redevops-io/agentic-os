"""agentic-guide as a Mission Runtime operator — proof over the redevops-rag corpus.

Exercises the REAL core logic (RBAC-scoped retrieval + walkthrough) and the SDK-mount
contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's own
HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

The guide's "retrieval" is pure, in-process token-overlap over a local corpus (no external
endpoint), so the operator runs the real core deterministically. The only network boundary
is the optional LLM narration, injected as a callback into core.answer — faked here to prove
the seam without touching the operator's deterministic path.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/guide/test_operator.py -q
"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guide import core
from guide.operator import build_guide_operator
from agentic_os.mission.operators import HTTPOperatorClient


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_guide_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"guide.retrieve", "guide.walkthrough"}
    # read-only retrieval → NO gate, NO side effects (matches modules.yaml approval_required: [])
    for name in caps:
        assert caps[name]["approval_required"] is False
        assert caps[name]["side_effecting"] is False
    assert "guide_answer" in caps["guide.retrieve"]["provides"]
    assert "guide_walkthrough" in caps["guide.walkthrough"]["provides"]


def test_invoke_retrieve_runs_real_core(client):
    r = client.post("/invoke", json={
        "capability": "guide.retrieve",
        "inputs": {"question": "how does billing dunning and refund work", "role": "finance"},
    }).json()
    res = r["result"]
    # the REAL core retrieved + answered (deterministic fallback, no LLM configured)
    assert res["role"] == "finance"
    assert res["cited"][0] == "agentic-billing"
    assert "agentic-billing" in res["answer"]


def test_invoke_walkthrough_runs_real_core(client):
    res = client.post("/invoke", json={
        "capability": "guide.walkthrough",
        "inputs": {"app": "outreach-engine"},
    }).json()["result"]
    assert res["app"] == "outreach-engine"
    assert res["core"] == "Twenty CRM"
    assert res["group"] == "Growth & Intelligence"
    assert len(res["steps"]) == 5


def test_retrieve_is_rbac_scoped(client):
    """finance cannot see (or be answered about) sales-only apps like outreach-engine."""
    res = client.post("/invoke", json={
        "capability": "guide.retrieve",
        "inputs": {"question": "walk me through outreach-engine sequencer", "role": "finance"},
    }).json()["result"]
    assert "outreach-engine" not in res["cited"]
    # admin does see it, ranked top for the same query
    res_admin = client.post("/invoke", json={
        "capability": "guide.retrieve",
        "inputs": {"question": "walk me through outreach-engine sequencer", "role": "admin"},
    }).json()["result"]
    assert res_admin["cited"][0] == "outreach-engine"


def test_llm_callback_narrates_core(client):
    """The optional LLM narration is a callback into core.answer (the only network seam)."""
    out = core.answer("billing", "finance", llm=lambda prompt: "FAKE-NARRATION")
    assert out["answer"] == "FAKE-NARRATION"
    assert out["cited"][0] == "agentic-billing"  # retrieval still runs, deterministically


def test_idempotency_dedupes_invoke(client):
    body = {"capability": "guide.retrieve",
            "inputs": {"question": "compliance evidence", "role": "security"},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"guide": "http://guide"}, transport=_transport)
    ans = oc.invoke("guide", "guide.retrieve",
                    {"question": "how do I close the books", "role": "finance"},
                    idempotency_key="m-1")
    assert ans["role"] == "finance" and ans["cited"]

    walk = oc.invoke("guide", "guide.walkthrough", {"app": "agentic-billing"}, idempotency_key="m-2")
    assert walk["app"] == "agentic-billing" and walk["core"] == "Lago"
