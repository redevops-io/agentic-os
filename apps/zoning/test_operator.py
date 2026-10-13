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


# ── independent-oracle evaluation (hand-labelled fixture; labels NOT derived from the classifier) ──
import json as _json
import pathlib as _pl

_LABELS = _json.loads((_pl.Path(__file__).parent / "fixtures" / "zoning_labels.json").read_text())["cases"]


def _score_labels():
    rows = []
    for c in _LABELS:
        r = core.evaluate_use({"base_zoning": c["code"], "use": c["use"], "overlays": c.get("overlays", []),
                               "permitted_uses": c.get("permitted_uses", [])})
        rows.append((c, r["disposition"]))
    return rows


def test_label_set_is_large_and_covers_required_codes():
    assert len(_LABELS) >= 60
    codes = {c["code"] for c in _LABELS}
    assert {"RC-1", "ROS", "RI", "MU-1", "R-1", "C-2", "I-1", ""} <= codes


def test_zero_false_permits_against_independent_labels():
    bad = [(c["code"], c["use"], d) for c, d in _score_labels() if d == "PERMITTED" and c["expected"] != "PERMITTED"]
    assert bad == []


def test_zoning_metrics_against_labels():
    rows = _score_labels()
    tp = sum(1 for c, d in rows if d == "PERMITTED" and c["expected"] == "PERMITTED")
    fp = sum(1 for c, d in rows if d == "PERMITTED" and c["expected"] != "PERMITTED")
    fn = sum(1 for c, d in rows if d != "PERMITTED" and c["expected"] == "PERMITTED")
    unk = sum(1 for c, d in rows if d == "UNKNOWN") / len(rows)
    wrong_prohibit = [(c["code"], c["use"]) for c, d in rows if d == "PROHIBITED" and c["expected"] != "PROHIBITED"]
    print(f"PERMIT precision={tp/(tp+fp):.3f} recall={tp/(tp+fn):.3f} UNKNOWN-rate={unk:.3f} n={len(rows)}")
    assert fp == 0 and wrong_prohibit == []
    assert tp / (tp + fn) >= 0.9
    assert unk <= 0.7


def test_lookalike_r_codes_never_permit_a_home():
    for code in ("RC-1", "ROS", "RI", "RT-1", "RPD", "MU-1", "PD"):
        assert core.evaluate_use({"base_zoning": code, "use": "RESIDENTIAL_SINGLE_FAMILY"})["disposition"] == "UNKNOWN"


def test_false_permit_tripwire_is_independent_of_classifier(monkeypatch):
    monkeypatch.setattr(core, "classify_code", lambda code, jurisdiction="": ("residential", "gen"))
    r = core.evaluate_use({"base_zoning": "RC-1", "use": "RESIDENTIAL_SINGLE_FAMILY"})
    assert r["disposition"] == "PERMITTED" and r["false_permit"] is True   # classifier bug is caught


def test_acquire_evidence_does_not_invent_provenance():
    e = core.acquire_evidence({"zoning_code": "R-1", "ordinance_url": "http://x"})
    assert "official_gis" not in e["evidence_sources"] and e["evidence_sources"][0] == "caller_supplied"
    assert e["verified"] is False
    core.register_fetcher("gis_test", lambda b: {"zoning_code": "C-2"})
    e2 = core.acquire_evidence({"zoning_code": "R-1", "fetcher": "gis_test"})
    assert e2["evidence_sources"][0] == "gis_test" and e2["base_zoning"] == "C-2" and e2["verified"] is True


def test_jurisdiction_table_extension(tmp_path, monkeypatch):
    f = tmp_path / "t.json"; f.write_text('{"Testville": {"LDR": "residential"}}')
    monkeypatch.setenv("ZONING_CODE_TABLE", str(f))
    assert core.code_family("LDR", "Testville") == "residential" and core.code_family("LDR") == "unknown"
