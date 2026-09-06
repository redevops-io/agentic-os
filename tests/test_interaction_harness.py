"""Customer-service scenario harness — scores intent resolution over the interaction runtime, offline.

Mirrors the NeMo Voice Agent eval (airline/retail/telecom) but on OUR governed path. The 328 real
scenarios are not bundled; these tests use the built-in representative fixtures + a JSONL loader.
"""
from __future__ import annotations

import json

from runtime_contracts import Channel

from agentic_os.interaction import (
    InteractionScenario,
    KeywordIntentResolver,
    builtin_scenarios,
    load_scenarios,
    run_scenario,
    run_suite,
)
from agentic_os.interaction import bench


def test_builtin_suite_covers_the_three_nemo_domains():
    scen = builtin_scenarios()
    domains = {s.domain for s in scen}
    assert {"airline", "retail", "telecom"} <= domains
    assert all(s.source == "builtin" for s in scen)


def test_baseline_resolver_scores_but_is_not_perfect():
    report = run_suite(builtin_scenarios(), resolver=KeywordIntentResolver())
    # a real, honest number: the keyword baseline resolves most but not all (a paraphrase defeats it)
    assert 0.5 < report.accuracy < 1.0
    assert report.passed < report.total
    # per-domain breakdown adds up
    assert sum(t for _, (p, t) in report.by_domain().items()) == report.total
    assert "%" in report.summary()


def test_a_paraphrase_that_defeats_keywords_is_an_abstain_not_a_wrong_answer():
    # air-5 is phrased without booking keywords — the baseline abstains rather than guessing wrong
    air5 = next(s for s in builtin_scenarios() if s.scenario_id == "air-5")
    r = run_scenario(air5, resolver=KeywordIntentResolver())
    assert not r.passed and r.resolved is None and r.reason == "abstained"


def test_resolver_reaches_the_expected_objective_for_a_clear_utterance():
    s = InteractionScenario("t1", "retail", ("I want to return the jacket",), "return_item")
    r = run_scenario(s, resolver=KeywordIntentResolver())
    assert r.passed and r.resolved == "return_item"


def test_a_custom_resolver_plugs_into_the_same_seam():
    # an oracle resolver (stand-in for a learned/LLM one) beats the baseline → the run shows the delta
    def oracle(turns, domain):
        return {s.turns: s.expected_objective for s in builtin_scenarios()}[tuple(turns)], {}
    report = run_suite(builtin_scenarios(), resolver=oracle)
    assert report.accuracy == 1.0


def test_jsonl_loader_parses_and_skips_malformed(tmp_path):
    p = tmp_path / "scen.jsonl"
    p.write_text(
        json.dumps({"scenario_id": "x1", "domain": "airline", "turns": ["book a flight"],
                    "expected_objective": "book_flight", "channel": "web"}) + "\n"
        + "not json\n"
        + json.dumps({"turns": ["hi"]}) + "\n"                # missing scenario_id AND domain (required)
    )
    loaded = load_scenarios(str(p), source="test")
    assert [s.scenario_id for s in loaded] == ["x1"]          # malformed + missing-required skipped
    assert loaded[0].channel is Channel.WEB and loaded[0].source == "test"


def test_bench_run_uses_builtins_when_no_dataset_env(monkeypatch):
    monkeypatch.delenv("INTERACTION_SCENARIOS", raising=False)
    report = bench.run()
    assert report.total == len(builtin_scenarios()) > 0


def test_bench_run_uses_the_dataset_when_present(tmp_path, monkeypatch):
    p = tmp_path / "real.jsonl"
    p.write_text(json.dumps({"scenario_id": "r1", "domain": "telecom", "turns": ["pay my bill"],
                             "expected_objective": "pay_bill"}) + "\n")
    monkeypatch.setenv("INTERACTION_SCENARIOS", str(p))
    report = bench.run()
    assert report.total == 1 and report.passed == 1
