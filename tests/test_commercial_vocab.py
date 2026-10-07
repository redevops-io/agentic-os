"""Canonical commercial vocabulary reconciliation (P1C, plan §22/§28).

Proves: CanonicalPerson/Organization are typed facades over the SINGLE CanonicalEntity (build + from_entity +
to_entity round-trip; wrong entity_type rejected); CommercialOutcome adapts onto the base overlays.Outcome reward
envelope and records through the shared sink; and merge-policy aliases from the Projects-workflow vocabulary
resolve onto the canonical MergePolicy while model_synthesis is rejected as a non-policy.
"""
from __future__ import annotations

import pytest

from agentic_os.commercial import (
    CanonicalOrganization, CanonicalPerson, CommercialOutcome, record_commercial_outcome,
)
from agentic_os.integration.contracts import CanonicalEntity, EntityType, ResolutionStatus
from agentic_os.overlays import LocalRewardLog
from agentic_os.sidekick import MergePolicy, coerce_merge_policy


def test_person_facade_builds_and_roundtrips_over_one_model():
    p = CanonicalPerson.build(name="Sarah Lee", source_bindings=(("res_twenty", "c_1"),),
                              resolution_status=ResolutionStatus.RESOLVED, confidence=0.9)
    assert p.display_name == "Sarah Lee"
    assert p.entity.entity_type is EntityType.PERSON          # single underlying model
    assert p.resolution_status is ResolutionStatus.RESOLVED
    # round-trip through the one storage type
    assert CanonicalPerson.from_entity(p.to_entity()).entity_id == p.entity_id


def test_org_facade_rejects_wrong_entity_type():
    person_entity = CanonicalEntity(entity_type=EntityType.PERSON)
    with pytest.raises(ValueError):
        CanonicalOrganization.from_entity(person_entity)
    org = CanonicalOrganization.build(name="Acme Inc")
    assert org.display_name == "Acme Inc" and org.entity.entity_type is EntityType.ORGANIZATION


def test_commercial_outcome_adapts_onto_reward_envelope():
    o = CommercialOutcome(capability="acquisition.funnel_optimize", subject="redevops.io",
                          action="CHANGE_CTA", economic_value=1200.0, policy_version="learned/v3",
                          observed_outcome="conversion +0.8pp", mission_id="m_42")
    ro = o.to_reward_outcome()
    assert ro.mission_id == "m_42" and ro.reward == 1200.0 and ro.policy_version == "learned/v3"
    assert ro.context["capability"] == "acquisition.funnel_optimize"
    assert ro.context["observed_outcome"] == "conversion +0.8pp"
    # explicit reward overrides economic_value
    assert CommercialOutcome(capability="x.y", subject="s", economic_value=5.0, reward=9.0).effective_reward == 9.0


def test_record_commercial_outcome_hits_the_shared_sink():
    sink = LocalRewardLog()
    o = CommercialOutcome(capability="finance.receivables", subject="inv_7", economic_value=300.0)
    record_commercial_outcome(o, sink=sink)
    assert len(sink.outcomes) == 1 and sink.outcomes[0].reward == 300.0


def test_merge_policy_alias_reconciliation():
    assert coerce_merge_policy(MergePolicy.SET_UNION) is MergePolicy.SET_UNION
    assert coerce_merge_policy("set_union") is MergePolicy.SET_UNION          # canonical value
    # Projects-workflow vocabulary → canonical
    assert coerce_merge_policy("contradiction_first") is MergePolicy.KEEP_CONTRADICTIONS
    assert coerce_merge_policy("ranked_evidence") is MergePolicy.HIGHEST_EVIDENCE
    assert coerce_merge_policy("choose_best") is MergePolicy.RANK_CANDIDATES
    assert coerce_merge_policy("CONSENSUS") is MergePolicy.REQUIRE_AGREEMENT  # case-insensitive
    with pytest.raises(ValueError):
        coerce_merge_policy("model_synthesis")   # a post-merge step, not a field policy
    with pytest.raises(ValueError):
        coerce_merge_policy("not_a_policy")
