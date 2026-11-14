"""The owner's gate: quality (redevops-qa) tools, projection and catalog entry stay off until P2-1 AC-1..AC-12 pass in
CI on the loopback Edge Sentinel fixture target. Closed: an explicit 'external-target acceptance not met', no call to the
product. The code is exercised with the gate opened by the test only (the gate is a code constant, not configuration)."""
from __future__ import annotations

import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from agentic_os.products import gates
from agentic_os.products.hub import ProductHub
from agentic_os.products.registry import load_registry
from agentic_os.products.tools import build_registry
from agentic_os.projects_api import SampleProjectionProvider, create_app
from agentic_os.sidekick.orchestrator import SidekickOrchestrator
from tests._products_fakes import RUN, TGT, FakeQa, write_registry

ROOT = pathlib.Path(__file__).resolve().parents[1]
PID = "customer-ops"


@pytest.fixture
def qa(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "PROJECTS_APPS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeQa()
    base = fake.serve()
    reg = write_registry(tmp_path, base, kind="quality", pid="redevops-qa")
    monkeypatch.setenv("AGENTIC_OS_PRODUCTS", reg)
    yield fake, reg
    fake.close()


def test_the_gate_is_a_closed_code_constant_that_no_configuration_can_open():
    assert gates.QA_EXTERNAL_TARGET_ACCEPTANCE_MET is False
    src = (ROOT / "agentic_os" / "products" / "gates.py").read_text()
    assert "import os" not in src and "os.environ" not in src and "getenv" not in src                       # not an env switch, not a registry key
    assert gates.gate_for("security") == {"open": True}
    g = gates.gate_for("quality")
    assert g["open"] is False and g["reason"] == "external-target acceptance not met"


def test_catalog_entry_follows_the_gate():
    names = {m["name"] for m in yaml.safe_load((ROOT / "modules.yaml").read_text())["modules"]}
    assert ("redevops-qa" in names) == gates.qa_gate_open()
    assert "edge-sentinel" in names


def test_closed_gate_every_surface_says_unavailable_and_the_product_is_never_called(qa):
    fake, reg = qa
    c = TestClient(create_app(SampleProjectionProvider()))
    row = next(a for a in c.get(f"/api/projects/{PID}/apps").json() if a["provider"] == "redevops-qa")
    assert row["state"] == "NOT_CONNECTED" and row["capabilities"] == [] and "external-target acceptance not met" in " ".join(row["manual_steps"])
    assert c.get(f"/api/projects/{PID}/missions").json() == []                # no QA projection (and no fake rows)
    assert c.get(f"/api/projects/{PID}/attention").json() == []
    p = c.get("/api/products").json()["products"][0]
    assert p["gate"]["open"] is False and p["unavailable"][0]["code"] == "external-target-acceptance-not-met"
    r = c.post("/api/sidekick", json={"ctx": {"section": "today"}, "text": "start a QA run"}).json()
    assert r["data"]["status"] == "unavailable" and "external-target acceptance not met" in r["text"]
    r = c.post(f"/api/products/redevops-qa/runs/{RUN}/approval", json={"approved": True, "estimate_digest": "sha256:" + "e" * 64})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "unavailable"
    assert fake.calls == []                                                   # not even a health probe: the gate decides first


def test_open_gate_exercises_the_quality_code_path(qa, monkeypatch):
    fake, reg = qa
    monkeypatch.setattr(gates, "QA_EXTERNAL_TARGET_ACCEPTANCE_MET", True)
    hub = ProductHub(load_registry(reg))
    o = SidekickOrchestrator(registry=build_registry(hub))
    assert o.invoke("quality.qa_list_targets").evidence == (TGT,)
    r = o.invoke("quality.qa_start_run", target_id=TGT, profile="edge-sentinel-fixture-readonly", estimate_digest="sha256:" + "e" * 64)
    assert r.data["state"] == "awaiting_approval" and r.data["approval"]["decide"]["actor_required"] == "product-linked user"
    assert r.data["executed"] is False and set(fake.started[0]) == {"target_id", "profile", "estimate_digest"}   # never a path/command/plan
    bad = o.invoke("quality.qa_start_run", target_id="http://evil/x", profile="x; rm")
    assert bad.data["status"] == "invalid"
    assert "available" in o.invoke("quality.qa_info").answer and "robot (ARCHITECTURE_UNSUPPORTED)" in o.invoke("quality.qa_info").answer
    c = TestClient(create_app(SampleProjectionProvider()))
    ms = c.get(f"/api/projects/{PID}/missions").json()
    assert ms[0]["id"] == f"redevops-qa:{RUN}" and ms[0]["state"] == "needs"
    att = c.get(f"/api/projects/{PID}/attention").json()
    assert att[0]["title"] == f"Approve QA run {RUN}"
    d = c.get(f"/api/projects/{PID}/missions/redevops-qa:{RUN}").json()
    assert d["steps"][0]["capability"] == "run.approval_requested"
    card = c.get(f"/api/products/redevops-qa/records/{RUN}").json()
    ok = c.post(f"/api/products/redevops-qa/runs/{RUN}/approval", json={"approved": True, "estimate_digest": card["digest"]})
    assert ok.status_code == 200 and fake.runs[RUN]["state"] == "queued" and ok.json()["result"]["approved_by"] == "projects:local-owner"
    stale = c.post(f"/api/products/redevops-qa/runs/{RUN}/approval", json={"approved": True, "estimate_digest": "sha256:" + "0" * 64})
    assert stale.status_code == 409
