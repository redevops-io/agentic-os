"""Zoning operator — proof over the real (self-contained) core logic and the SDK-mount contract.

Exercises the deterministic-first, fail-safe conclusions (the false-permitted = 0 SLO), the parcel-first
and use-first capabilities, the GET /capabilities + POST /invoke wire contract, and the runtime's own
LocalOperatorClient driving the operator.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest apps/zoning/test_operator.py -q
"""
from __future__ import annotations

import pytest

pytest.importorskip("runtime_contracts")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zoning import core
from zoning.operator import build_zoning_operator

from agentic_os.mission.operator_sdk import LocalOperatorClient

# Three real-shaped parcels: a residential base, a commercial base, and an industrial base.
PARCELS = [
    {"parcel_id": "geo:res1", "zoning_code": "R-4", "jurisdiction": "Raleigh", "ordinance_url": "http://ord/r4"},
    {"parcel_id": "geo:com1", "zoning_code": "C-2", "jurisdiction": "Phoenix", "ordinance_url": "http://ord/c2"},
    {"parcel_id": "geo:ind1", "zoning_code": "IL", "jurisdiction": "Columbus", "ordinance_url": ""},
]


# ── core: deterministic-first, fail-safe conclusions ──

def test_resolve_parcel_from_raw_geometry_gets_canonical_identity():
    out = core.resolve_parcel({"rings": [[(0, 0), (0, 1), (1, 1), (1, 0)]], "crs": "EPSG:4326",
                               "jurisdiction": "Testville"})
    assert out["parcel_id"].startswith("geo:")
    assert out["envelope_ref"].startswith("rcv1:")
    assert out["geometry_ref"]["crs"] == "EPSG:4326"


def test_evaluate_use_is_fail_safe():
    # residential-by-right → PERMITTED
    r = core.evaluate_use({"base_zoning": "R-4", "use": "RESIDENTIAL_SINGLE_FAMILY"})
    assert r["disposition"] == "PERMITTED" and r["false_permit"] is False
    # base-incompatible → PROHIBITED with certainty
    p = core.evaluate_use({"base_zoning": "R-4", "use": "WAREHOUSE"})
    assert p["disposition"] == "PROHIBITED"
    # base-compatible but needs the ordinance → UNKNOWN, never a guessed PERMITTED
    u = core.evaluate_use({"base_zoning": "C-2", "use": "RETAIL"})
    assert u["disposition"] == "UNKNOWN"


def test_no_false_permit_across_all_combinations():
    uses = list(core.USE_FAMILY.keys())
    false_permits = sum(core.evaluate_use({**p, "base_zoning": p["zoning_code"], "use": u})["false_permit"]
                        for p in PARCELS for u in uses)
    assert false_permits == 0                                    # the blocking SLO


def test_search_parcels_use_first_never_includes_a_prohibited_parcel():
    res = core.search_parcels({"use": "RESIDENTIAL_SINGLE_FAMILY",
                               "parcels": [{**p, "base_zoning": p["zoning_code"]} for p in PARCELS]})
    ids = {m["parcel_id"] for m in res["matches"]}
    assert "geo:res1" in ids                                     # residential base is compatible
    assert all(m["disposition"] != "PROHIBITED" for m in res["matches"])


# ── the SDK-mount + client contract ──

def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_zoning_operator().router())
    return TestClient(app)


def test_capabilities_manifest_lists_the_four_syscalls():
    names = {c["name"] for c in _client().get("/capabilities").json()["capabilities"]}
    assert names == {"zoning.resolve_parcel", "zoning.acquire_evidence",
                     "zoning.evaluate_use", "zoning.search_parcels"}


def test_invoke_over_the_wire_evaluates_a_use():
    r = _client().post("/invoke", json={"capability": "zoning.evaluate_use",
                                        "inputs": {"base_zoning": "IL", "use": "LIGHT_INDUSTRIAL"}})
    assert r.status_code == 200
    assert r.json()["result"]["disposition"] == "UNKNOWN"       # industrial base, but needs the ordinance


def test_local_client_drives_the_operator_with_idempotency():
    client = LocalOperatorClient({"zoning": build_zoning_operator()})
    out = client.invoke("zoning", "zoning.resolve_parcel",
                        {"parcel_id": "geo:com1", "zoning_code": "C-2", "jurisdiction": "Phoenix"}, "k1")
    assert out["parcel_id"] == "geo:com1"
    # exactly-once: same idempotency key returns the same result
    again = client.invoke("zoning", "zoning.resolve_parcel",
                          {"parcel_id": "geo:com1", "zoning_code": "C-2", "jurisdiction": "Phoenix"}, "k1")
    assert again == out
