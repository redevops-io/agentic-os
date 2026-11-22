"""SR-04 round 5 (LG-3): one shared, normalising test for synthetic-study references, used by the ledger guard,
freeze_experiment, plan_experiments' explain counting and evidence_ref split/attach.

DECISION (documented in docs/ledger_source_inventory.md): the experiment-plane predicates (`is_synthetic_ref`) match a
reference whose NORMALISED text STARTS with the scheme, so `obs:synthetic-study:x`, `synthetic-studyx`,
`synthetic study:v1:x`, `synth-study:x` and `syn-thetic-study:` remain ordinary references exactly as on base. The
LEDGER predicate (`mentions_synthetic_ref`) is stricter: it refuses a metric that mentions the scheme ANYWHERE in a
field, so `obs:synthetic-study:x` is refused by the ledger yet still counted by the experiment plane."""
from __future__ import annotations

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import experiments as X
from agentic_os.market.contracts import Opportunity
from agentic_os.market.experiment_design import CommercialHypothesis, freeze_experiment
from agentic_os.market.ingestion import repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger
from agentic_os.market.synthetic_refs import is_synthetic_ref, mentions_synthetic_ref, normalize_ref
from agentic_os.market.synthetic_research.evidence_ref import StudyEvidenceRef, attach_study_refs, split_refs

REF = StudyEvidenceRef("s1", "h" * 8, "relevance", "emb").to_ref()

# variants that START with the scheme once normalised
STARTS = [
    REF, REF.upper(), "  " + REF, "\t\n" + REF, "synthetic-study:", "synthetic_study:v1:x", "Synthetic_Study:x",
    "ｓｙｎｔｈｅｔｉｃ－ｓｔｕｄｙ：v1:{}", "ｓｙｎｔｈｅｔｉｃ_ｓｔｕｄｙ：x",
    "syn​thetic-study:v1:x", "synthetic‍-study:x", "s​y​n​t​h​e​t​i​c-study:",
    "‮synthetic-study:v1:x", "⁦synthetic-study:x⁩", "synthe­tic-study:x", "﻿synthetic-study:x",
    "\x00synthetic-study:x", "synthetic-\x07study:x", "ѕynthetic-study:x", "synthetіc-study:x", "synthеtic-study:x",
    "ѕуnthеtіс-ѕtudу:x", "SYNTHETIC-STUDY:V2:X"]
# references that mention the scheme but do not start with it
MENTIONS_ONLY = ["obs:synthetic-study:x", "see synthetic-study:v1:x", "x" * 50 + "synthetic_study:y", "a/b/synthetic-study:"]
# ordinary references (as on base): neither predicate
ORDINARY = ["obs:1", "https://x.io/p", "snap:abc", "synthetic-study", "synthetic-studyx", "synthetic study:v1:x",
            "synth-study:x", "syn-thetic-study:", "study:x", "synthetic", "", "  ", "ssr:1", "mission:ssrs-report",
            "ünïcode:ref", "x" * 500]


def opp(refs):
    return Opportunity(prov=Provenance("m", "o"), pattern_ref="p", site="s", gap="g", proposed_experiment="do x",
                       expected_value=0.7, evidence_refs=tuple(refs), confidence=0.5)


def hyp(refs):
    return CommercialHypothesis(prov=Provenance("p", "r"), observed_pattern="o", proposed_mechanism="m",
                                affected_funnel_stage="f", expected_metric="signup", evidence_refs=tuple(refs))


@pytest.mark.parametrize("ref", STARTS)
def test_every_normalising_variant_is_recognised_by_every_consumer(ref):
    assert is_synthetic_ref(ref) and mentions_synthetic_ref(ref)
    # explain counting excludes it
    plain = X.plan_experiments([opp(("obs:1", "obs:2"))])[0]
    with_ref = X.plan_experiments([opp(("obs:1", "obs:2", ref))])[0]
    assert "2 supporting observation(s)" in plain.explain and "2 supporting observation(s)" in with_ref.explain
    # freeze refuses it
    with pytest.raises(ValueError):
        freeze_experiment(hyp(("obs:1", ref)), control="c", treatment="t", target_surface="s", primary_metric="signup",
                          success_threshold=0.1)
    # split puts it in the synthetic / malformed buckets, never in the first-party bucket
    plain_refs, synth, malformed = split_refs(["obs:1", ref])
    assert plain_refs == ("obs:1",) and len(synth) + len(malformed) == 1
    # the ledger refuses it wherever it hides
    for kw in ({"evidence_refs": (ref,)}, {"provider_ref": ref}, {"metric": ref}, {"surface": ref}):
        kw = {"metric": "m", **kw}
        with pytest.raises(ValueError):
            FirstPartyLedger().record(repo_adapter("umami").make_metric(value=1.0, segment="control", **kw))


@pytest.mark.parametrize("ref", MENTIONS_ONLY)
def test_the_ledger_is_stricter_than_the_experiment_counters(ref):
    assert mentions_synthetic_ref(ref) and not is_synthetic_ref(ref)
    plain = X.plan_experiments([opp(("obs:1",))])[0]
    counted = X.plan_experiments([opp(("obs:1", ref))])[0]
    assert "1 supporting observation(s)" in plain.explain and "2 supporting observation(s)" in counted.explain
    freeze_experiment(hyp(("obs:1", ref)), control="c", treatment="t", target_surface="s", primary_metric="signup",
                      success_threshold=0.1)                                       # not sealed-out: as on base
    with pytest.raises(ValueError):
        FirstPartyLedger().record(repo_adapter("umami").make_metric(metric="m", value=1.0, evidence_refs=(ref,)))


@pytest.mark.parametrize("ref", ORDINARY)
def test_ordinary_references_are_unchanged_everywhere(ref):
    assert not is_synthetic_ref(ref) and not mentions_synthetic_ref(ref)
    counted = X.plan_experiments([opp(("obs:1", ref))])[0]
    assert "2 supporting observation(s)" in counted.explain
    freeze_experiment(hyp(("obs:1", ref)), control="c", treatment="t", target_surface="s", primary_metric="signup",
                      success_threshold=0.1)
    if ref.strip():
        assert FirstPartyLedger().record(repo_adapter("umami").make_metric(
            metric="m", value=1.0, evidence_refs=(ref,))).value == 1.0


def test_non_strings_and_huge_inputs_are_safe():
    for junk in (None, 5, b"synthetic-study:", ["synthetic-study:"], object()):
        assert not is_synthetic_ref(junk) and not mentions_synthetic_ref(junk) and normalize_ref(junk) == ""
    assert is_synthetic_ref("synthetic-study:" + "x" * 10 ** 6)
    assert not is_synthetic_ref("​" * 10 ** 6 + "x")


def test_attach_and_parse_treat_variants_as_synthetic_and_malformed_not_first_party():
    plain, synth, malformed = split_refs(["obs:1", "ｓｙｎｔｈｅｔｉｃ－ｓｔｕｄｙ：v1:{}", REF, "synthetic_study:v1:{}"])
    assert plain == ("obs:1",) and len(synth) == 1 and len(malformed) == 2
    for bad in ("ｓｙｎｔｈｅｔｉｃ－ｓｔｕｄｙ：v1:{}", "synthetic_study:v1:{}"):
        with pytest.raises(ValueError):
            StudyEvidenceRef.parse(bad)


def test_the_documented_difference_is_in_the_docs():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "ledger_source_inventory.md").read_text()
    assert "obs:synthetic-study:x" in text and "stricter than the experiment counters" in text
