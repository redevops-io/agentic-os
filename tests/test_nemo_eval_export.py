"""Export NeMo Voice Agent eval scenarios → harness JSONL, and score them reference-free.

Uses tiny synthetic fixtures shaped exactly like labs-Voice-Agent's data (the real MIT corpus is not
vendored). Verifies the exporter reads eva_airline JSONL + tau2 tasks.json, and that unlabelled
scenarios are scored by coverage (reach), never a fabricated accuracy.
"""
from __future__ import annotations

import json
import os

from agentic_os.interaction import (
    KeywordIntentResolver,
    coverage_report,
    export_nemo_scenarios,
    load_scenarios,
    write_jsonl,
)


def _make_data(root):
    d = os.path.join(root, "data")
    os.makedirs(os.path.join(d, "eva_airline"))
    os.makedirs(os.path.join(d, "tau2_retail"))
    with open(os.path.join(d, "eva_airline", "eva_airline_dataset.jsonl"), "w") as fh:
        fh.write(json.dumps({"id": "1.1.2", "user_goal": "You want to cancel your flight to LAX"}) + "\n")
        fh.write(json.dumps({"id": "1.1.3", "user_goal": "You want to book a flight to Denver"}) + "\n")
    with open(os.path.join(d, "tau2_retail", "tasks.json"), "w") as fh:
        json.dump([
            {"id": 0, "user_scenario": {"instructions": {"domain": "retail",
                                                         "reason_for_call": "You want to return the shoes"}}},
            {"id": 1, "user_scenario": {"instructions": {"domain": "retail",
                                                         "reason_for_call": "Tell me a story about nothing"}}},
        ], fh)
    return d


def test_exporter_reads_eva_jsonl_and_tau2_tasks(tmp_path):
    data = _make_data(str(tmp_path))
    rows = export_nemo_scenarios(data)
    ids = {r["scenario_id"] for r in rows}
    assert ids == {"eva_airline:1.1.2", "eva_airline:1.1.3", "tau2_retail:0", "tau2_retail:1"}
    # every row has the flat harness shape; airline + retail domains present
    assert all(set(r) >= {"scenario_id", "domain", "turns", "source"} for r in rows)
    assert {r["domain"] for r in rows} == {"airline", "retail"}
    assert rows[0]["turns"] and isinstance(rows[0]["turns"][0], str)


def test_exported_jsonl_round_trips_through_the_loader(tmp_path):
    data = _make_data(str(tmp_path))
    out = tmp_path / "scen.jsonl"
    n = write_jsonl(export_nemo_scenarios(data), str(out))
    assert n == 4
    loaded = load_scenarios(str(out), source="nemo")
    # real scenarios are UNLABELLED — they load with an empty expected_objective
    assert len(loaded) == 4
    assert all(s.expected_objective == "" for s in loaded)


def test_coverage_is_reference_free_reach_not_fabricated_accuracy(tmp_path):
    data = _make_data(str(tmp_path))
    loaded = load_scenarios(str(write_and_path(tmp_path, data)), source="nemo")
    cov = coverage_report(loaded, resolver=KeywordIntentResolver())
    # baseline fires on "cancel"/"book"/"return", abstains on the nonsense utterance → 3/4
    assert cov.classified == 3 and cov.total == 4
    assert 0.0 < cov.rate < 1.0
    assert "coverage, not correctness" in cov.summary()
    assert "%" in cov.summary()


def write_and_path(tmp_path, data):
    out = tmp_path / "scen.jsonl"
    write_jsonl(export_nemo_scenarios(data), str(out))
    return out
