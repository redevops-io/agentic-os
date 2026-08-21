"""§4 hardware-aware target selection — constraints (not hardware) drive the choice; the same snapshot
reproduces the plan; unavailable hardware drops out; mutating a stat changes the winner."""
from agentic_os.mission.provider import ExecutionTargetStats
from agentic_os.mission.target_selection import TargetConstraints, select_targets

V = "1"
HOSTED = ExecutionTargetStats(provider="nvidia-nim", accelerator_kind="nvidia-h100", locality="hosted",
                              p95_latency_ms=120.0, estimated_cost_usd=0.02, supported_context=131072,
                              supported_modalities=("text",), statistics_version=V)
SELFHOST = ExecutionTargetStats(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x",
                                locality="self-hosted", p95_latency_ms=90.0, estimated_cost_usd=0.05,
                                supported_context=131072, supported_modalities=("text",),
                                statistics_version=V)
CANDS = [HOSTED, SELFHOST]


def test_cost_and_latency_constraints_produce_different_choices():
    by_cost = select_targets(CANDS, TargetConstraints(max_cost_usd=0.03), statistics_version=V)
    by_latency = select_targets(CANDS, TargetConstraints(max_latency_ms=100.0), statistics_version=V)
    assert by_cost.selected.provider == "nvidia-nim"
    assert by_latency.selected.provider == "amd-rocm"


def test_same_snapshot_reproduces_the_plan():
    a = select_targets(CANDS, TargetConstraints(max_cost_usd=0.03), statistics_version=V)
    b = select_targets(CANDS, TargetConstraints(max_cost_usd=0.03), statistics_version=V)
    assert a.digest() == b.digest()


def test_unavailable_hardware_removed_without_error():
    sel = select_targets(CANDS, TargetConstraints(), statistics_version=V,
                         available=frozenset({"amd-instinct-mi300x"}))
    assert sel.selected.provider == "amd-rocm"
    assert all(c.stats.provider != "nvidia-nim" for c in sel.candidates if c.eligible)


def test_mutating_a_relevant_statistic_changes_selection():
    cheaper = ExecutionTargetStats(**{**SELFHOST.__dict__, "estimated_cost_usd": 0.01})
    sel = select_targets([HOSTED, cheaper], TargetConstraints(max_cost_usd=0.03), statistics_version=V)
    assert sel.selected.provider == "amd-rocm"


def test_no_eligible_target_is_explicit():
    sel = select_targets(CANDS, TargetConstraints(max_cost_usd=0.001), statistics_version=V)
    assert sel.selected is None and sel.eligible == []


def test_modality_and_residency_constraints_filter():
    text_only = select_targets(CANDS, TargetConstraints(required_modalities=("image",)),
                               statistics_version=V)
    assert text_only.selected is None   # neither supports image


def test_every_candidate_has_explain_reasons():
    sel = select_targets(CANDS, TargetConstraints(max_cost_usd=0.03), statistics_version=V)
    assert all(c.reasons for c in sel.candidates)
