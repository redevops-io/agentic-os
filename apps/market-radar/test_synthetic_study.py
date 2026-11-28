"""radar.synthetic_study: off by default, labelled synthetic, inconclusive when its own controls fail."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _synthetic_fakes import FakeRespondent, HashEmbedder  # noqa: E402

from agentic_os.market import synthetic_research as sr  # noqa: E402

synthetic = importlib.import_module("market-radar.synthetic")
build = importlib.import_module("market-radar.operator").build_market_radar_operator

VARIANTS = [{"id": "A", "headline": "Approve every action with a receipt.",
             "body": "Your apps work together. Each change is approved by you and logged with a receipt. Start with a free trial on sample data.", "cta": "Start a pilot"},
            {"id": "B", "headline": "Invoices stall. Leads go cold.", "body": "Your apps help with follow-ups, with approval before anything is sent.", "cta": "See a demo"}]


class Exploding:
    def __getattr__(self, name):
        raise AssertionError("a collaborator was touched")


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(sr.FLAG_ENV, "1")


def run(**body):
    return synthetic.synthetic_study({"variants": VARIANTS, **body}, transport=FakeRespondent(), embedder=HashEmbedder(), model_id="fake")


def test_off_by_default_and_nothing_is_touched(monkeypatch):
    monkeypatch.delenv(sr.FLAG_ENV, raising=False)
    res = synthetic.synthetic_study({"variants": VARIANTS}, transport=Exploding(), embedder=Exploding())
    assert res["status"] == "disabled" and res["synthetic_study_run"] is False
    via_operator = build().invoke("radar.synthetic_study", {"variants": VARIANTS})
    assert via_operator["status"] == "disabled"


def test_the_capability_is_read_only_and_needs_no_approval():
    spec = {c.name: c for c in build().manifest.capabilities}["radar.synthetic_study"]
    assert not spec.side_effecting and not spec.approval_required and spec.concurrency_mode == "read_only"


def test_every_result_says_it_is_synthetic_and_for_review(on):
    res = run()
    assert res["status"] == "done" and res["synthetic"] is True and res["evidence_class"] == "synthetic-hypothesis"
    assert res["requires_human_review"] is True and res["evidence_ref"]["synthetic"] is True and res["caveats"]
    assert res["study"]["personas"] == 4 and res["controls"]["run"] > 0
    cmp_ = next(c for c in res["comparisons"] if c["construct"] == "purchase_intent")
    assert [v["variant_id"] for v in cmp_["variants"]] == ["A", "B"] and cmp_["n_paired"] == 4
    assert all(h.endswith("real buyers before changing anything.") or "no reason to prefer" in h for h in res["hypotheses"])


def _break(monkeypatch, which):
    real = synthetic.summarise

    def broken(result):
        import dataclasses
        # every control's outcome is set here, so the test does not depend on how the fake respondent happens to score
        return real(dataclasses.replace(result, controls=tuple({**dict(c), "passed": not which(c)} for c in result.controls)))
    monkeypatch.setattr(synthetic, "summarise", broken)


def test_a_construct_whose_controls_fail_has_its_ranking_withheld_and_the_other_keeps_its_own(on, monkeypatch):
    _break(monkeypatch, lambda c: c["construct"] == "trust" and c["control"] == "empty_offer")
    res = run()
    assert res["verdict"] == "hypotheses" and res["withheld_constructs"] == ["trust"]
    by = {c["construct"]: c for c in res["comparisons"]}
    assert by["trust"]["controls_passed"] is False and by["trust"]["ranking"] == [] and by["trust"]["separated_from_first"] == []
    assert by["purchase_intent"]["controls_passed"] is True and by["purchase_intent"]["ranking"]
    assert res["inconclusive_reasons"] == ["control failed: empty_offer (trust)"]


def test_a_study_with_no_passing_construct_is_inconclusive(on, monkeypatch):
    _break(monkeypatch, lambda c: c["control"] == "empty_offer")
    res = run()
    assert res["verdict"] == "inconclusive" and res["hypotheses"] == []
    assert all(c["ranking"] == [] and c["separated_from_first"] == [] for c in res["comparisons"])


@pytest.mark.parametrize("body,why", [
    ({"variants": VARIANTS[:1]}, "2 to 4 message variants"),
    ({"variants": VARIANTS * 3}, "2 to 4 message variants"),
    ({"variants": [VARIANTS[0], {"id": "B"}]}, "needs a headline or a body"),
    ({"variants": [VARIANTS[0], {**VARIANTS[1], "id": "A"}]}, "distinct"),
    ({"audience": "consumers"}, "audience must be"),
    ({"per_cell": 50}, "per_cell must be"),
    ({"per_cell": "many"}, "whole numbers"),
    ({"segments": ["not_a_segment"]}, "segments must be"),
])
def test_bad_requests_are_refused_before_any_model_call(on, body, why):
    res = synthetic.synthetic_study({"variants": VARIANTS, **body}, transport=Exploding(), embedder=Exploding())
    assert res["status"] == "refused" and why in res["error"]


def test_unconfigured_collaborators_are_a_refusal_not_a_crash(on, monkeypatch):
    for k in ("SYNTHETIC_RESEARCH_LLM_URL", "SYNTHETIC_RESEARCH_LLM_MODEL", "SYNTHETIC_RESEARCH_EMBED_URL", "SYNTHETIC_RESEARCH_EMBED_MODEL"):
        monkeypatch.delenv(k, raising=False)
    res = synthetic.synthetic_study({"variants": VARIANTS})
    assert res["status"] == "refused" and "not configured: SYNTHETIC_RESEARCH_LLM_URL" in res["error"]


def test_an_out_of_boundary_model_endpoint_is_refused(on, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_RESEARCH_LLM_URL", "https://api.example.com/v1")
    monkeypatch.setenv("SYNTHETIC_RESEARCH_LLM_MODEL", "m")
    monkeypatch.setenv("SYNTHETIC_RESEARCH_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SYNTHETIC_RESEARCH_EMBED_MODEL", "e")
    res = synthetic.synthetic_study({"variants": VARIANTS})
    assert res["status"] == "refused" and "trust boundary" in res["error"]


def test_the_module_has_no_path_to_a_store_or_the_watch_service():
    src = Path(synthetic.__file__).read_text(encoding="utf-8")
    for banned in ("outcome_store", "OutcomeStore", "ledger", "from . import core", "changedetection", "httpx", "requests"):
        assert banned not in src.split('"""', 2)[2], banned


def test_the_enterprise_committee_is_one_scenario_with_every_role(on):
    res = run(audience="enterprise", per_cell=1)
    assert res["status"] == "done", res.get("error")
    assert res["study"]["personas"] == 7                                                 # seven committee roles, one scenario
    assert run(audience="enterprise", per_cell=1, scenario="E9")["error"].startswith("scenario must be one of")


def test_an_endpoint_that_does_not_answer_is_a_refusal_not_a_crash(on):
    class Down:
        name, revision = "down", "r"
        def embed(self, texts):
            import urllib.error
            raise urllib.error.HTTPError("http://127.0.0.1:9/v1/embeddings", 404, "Not Found", None, None)
    res = synthetic.synthetic_study({"variants": VARIANTS}, transport=FakeRespondent(), embedder=Down(), model_id="fake")
    assert res["status"] == "refused" and "HTTPError" in res["error"] and res["synthetic_study_run"] is False


def test_an_in_process_embedder_needs_no_embedding_endpoint(on, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_RESEARCH_LLM_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("SYNTHETIC_RESEARCH_LLM_MODEL", "m")
    monkeypatch.setenv("SYNTHETIC_RESEARCH_EMBEDDER", "sentence-transformers:BAAI/bge-small-en-v1.5")
    for k in ("SYNTHETIC_RESEARCH_EMBED_URL", "SYNTHETIC_RESEARCH_EMBED_MODEL"):
        monkeypatch.delenv(k, raising=False)
    _, embedder, model = synthetic.collaborators_from_env()
    assert embedder.name == "sentence-transformers:BAAI/bge-small-en-v1.5" and model == "m"


def test_three_draws_by_default_and_the_request_can_change_it(on):
    t = FakeRespondent()
    res = synthetic.synthetic_study({"variants": VARIANTS}, transport=t, embedder=HashEmbedder(), model_id="fake")
    assert res["study"]["replicates"] == 3 and len(t.calls) == 4 * (2 + 6) * 2 * 3
    t1 = FakeRespondent()
    one = synthetic.synthetic_study({"variants": VARIANTS, "replicates": 1}, transport=t1, embedder=HashEmbedder(), model_id="fake")
    assert one["study"]["replicates"] == 1 and len(t1.calls) == 4 * (2 + 6) * 2
    for bad in (0, 6, "many"):
        assert synthetic.synthetic_study({"variants": VARIANTS, "replicates": bad}, transport=Exploding(), embedder=Exploding())["status"] == "refused"


def test_controls_are_compared_with_the_variant_that_says_the_most(on):
    terse_first = [{"id": "A", "headline": "Invoices that stall", "body": "Chasing them is manual.", "cta": "See the fix"}, VARIANTS[0] | {"id": "B"}]
    res = synthetic.synthetic_study({"variants": terse_first, "replicates": 1}, transport=FakeRespondent(), embedder=HashEmbedder(), model_id="fake")
    assert res["study"]["control_base"] == "B"                  # not the terse first variant
    cmp_ = next(c for c in res["comparisons"] if c["construct"] == "purchase_intent")
    assert [v["variant_id"] for v in cmp_["variants"]] == ["A", "B"] and cmp_["variants"][0]["diff_vs_first"] == 0   # variants still compare with the first


def test_every_control_reports_what_was_measured(on):
    res = run(replicates=1)
    detail = res["controls"]["detail"]
    assert len(detail) == res["controls"]["run"] and {d["control"] for d in detail} >= {"empty_offer", "weak_value", "identical_relabelled", "false_claim"}
    paired = [d for d in detail if "diff" in d]
    assert paired and all({"passed", "expectation", "diff", "diff_ci95", "n_paired", "base_mean", "control_mean"} <= set(d) for d in paired)
