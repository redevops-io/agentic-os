"""SR-04 round 2, E2: minimal guards in market/experiments.py (evidence counting) and market/experiment_design.py
(freeze_experiment): a `synthetic-study:` reference (a prefix convention) is not counted as an observation and is refused as
sealed experiment evidence."""
from __future__ import annotations

import pytest

from agentic_os.market import experiments as X
from agentic_os.market.contracts import Opportunity, Provenance
from agentic_os.market.experiment_design import CommercialHypothesis, freeze_experiment
from agentic_os.market.synthetic_research import evidence_ref as ER
from agentic_os.market.synthetic_research.evidence_ref import StudyEvidenceRef, attach_study_refs

REF = StudyEvidenceRef("study-1", "sha256:" + "a" * 64, "relevance", "sentence-transformers:bge-small")


def opp(refs=("obs:1", "obs:2")):
    return Opportunity(prov=Provenance("internal.matcher", "o1"), pattern_ref="pat", site="s.example", gap="g",
                       proposed_experiment="Add a table", expected_value=0.7, evidence_refs=tuple(refs), confidence=0.5)


def hypothesis(refs=("obs:1",)):
    return CommercialHypothesis(prov=Provenance("p", "r"), observed_pattern="o", proposed_mechanism="m",
                                affected_funnel_stage="f", expected_metric="signup", evidence_refs=tuple(refs))


def test_e2_evidence_counting_ignores_synthetic_refs_even_if_put_on_an_opportunity_by_hand():
    plain = X.plan_experiments([opp()])[0]
    smuggled = X.plan_experiments([opp(refs=("obs:1", "obs:2", REF.to_ref(), "Synthetic-Study:v9:x"))])[0]
    assert "2 supporting observation(s)" in plain.explain and "2 supporting observation(s)" in smuggled.explain
    assert X._is_synthetic_ref is ER._shared_is_synthetic_ref            # ONE shared test (LG-3)


def test_e2_freeze_experiment_can_never_seal_a_synthetic_ref():
    ok = freeze_experiment(hypothesis(), control="c", treatment="t", target_surface="s", primary_metric="signup",
                           success_threshold=0.1)
    assert ok.evidence_snapshot == ("obs:1",)
    for ref in (REF.to_ref(), "synthetic-study:v2:whatever", "SYNTHETIC-STUDY:v1:{}", " synthetic-study:v1:x"):
        with pytest.raises(ValueError):
            freeze_experiment(hypothesis(("obs:1", ref)), control="c", treatment="t", target_surface="s",
                              primary_metric="signup", success_threshold=0.1)
    # the supported path cannot reach it either: attach refuses a hypothesis
    with pytest.raises(TypeError):
        attach_study_refs(hypothesis(), [REF])                        # type: ignore[arg-type]


