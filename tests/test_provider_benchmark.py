"""provider-benchmark/v1 — honest lane: unexercised-not-green, failures stay in the denominator, no
result without a premise witness, warm/cold + neutral/optimized separated, immutable versioned summary."""
from agentic_os.mission.provider_benchmark import (
    BenchmarkLane, BenchmarkArtifact, Group, LaneStatus, CONTRACT_VERSION, INITIAL_PROFILES,
)


def _art(profile, *, success=True, warm=True, optimized=False, witnessed=True, latency=100.0):
    return BenchmarkArtifact(
        group=Group.MODEL_EXECUTION, profile=profile, suite_version="s1", workload_digest="sha256:w",
        dataset_digest="sha256:d", mission_program_digest="sha256:m", context_plan_digest="sha256:c",
        provider_profile_digest="sha256:" + profile, model_identity="model/x", hardware_identity="hw/x",
        runtime_versions=("execution-plan/v8",), success=success, warm=warm,
        derived_metrics={"latency_ms": latency}, optimized=optimized,
        evidence=("art-e",) if witnessed else (), verification_result="verified" if witnessed else "")


def test_missing_nvidia_inputs_report_unexercised_not_green():
    lane = BenchmarkLane("s1")
    lane.record(_art("baseline-current-provider"))
    summ = lane.summarize()
    nim = summ.profile("nvidia-self-hosted-nim")
    assert nim is not None and nim.status == LaneStatus.UNEXERCISED and nim.success_rate is None


def test_failures_stay_in_the_denominator():
    lane = BenchmarkLane("s1")
    for ok in (True, True, False):
        lane.record(_art("baseline-current-provider", success=ok))
    base = lane.summarize().profile("baseline-current-provider")
    assert base.success_rate == 2 / 3 and base.attempts == 3


def test_result_without_witness_cannot_enter_aggregate():
    lane = BenchmarkLane("s1")
    lane.record(_art("baseline-current-provider", success=True))
    lane.record(_art("baseline-current-provider", success=True, witnessed=False))
    base = lane.summarize().profile("baseline-current-provider")
    assert base.admissible == 1 and base.dropped_no_witness == 1


def test_warm_and_cold_kept_separate():
    lane = BenchmarkLane("s1")
    lane.record(_art("nvidia-hosted-nim", warm=True, latency=120.0))
    lane.record(_art("nvidia-hosted-nim", warm=False, latency=900.0))
    summ = lane.summarize()
    warm = summ.profile("nvidia-hosted-nim", thermal="warm")
    cold = summ.profile("nvidia-hosted-nim", thermal="cold")
    assert warm.metrics["latency_ms"] == 120.0 and cold.metrics["latency_ms"] == 900.0


def test_optimized_lane_separated_from_neutral():
    lane = BenchmarkLane("s1")
    lane.record(_art("nvidia-hosted-nim", optimized=False))
    lane.record(_art("nvidia-hosted-nim", optimized=True))
    summ = lane.summarize()
    assert summ.profile("nvidia-hosted-nim", lane="neutral") is not None
    assert summ.profile("nvidia-hosted-nim", lane="optimized") is not None


def test_summary_is_immutable_and_versioned():
    lane = BenchmarkLane("s1")
    lane.record(_art("baseline-current-provider"))
    summ = lane.summarize()
    before = summ.digest()
    lane.record(_art("baseline-current-provider", success=False))   # new data
    assert summ.digest() == before                                  # old snapshot unchanged
    assert summ.contract_version == CONTRACT_VERSION


def test_all_initial_profiles_present_in_summary():
    summ = BenchmarkLane("s1").summarize()
    names = {r.profile for r in summ.results}
    assert set(INITIAL_PROFILES).issubset(names)


def test_suite_version_mismatch_rejected():
    lane = BenchmarkLane("s2")
    import pytest
    with pytest.raises(ValueError):
        lane.record(_art("baseline-current-provider"))  # artifact is s1
