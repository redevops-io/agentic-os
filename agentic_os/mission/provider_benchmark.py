"""Provider benchmark lane — compare NVIDIA-backed services with existing providers under *identical*
ReDevOps contracts. Part 6 of the accelerator-integration plan.

This is **not a vendor leaderboard**. It measures whether an implementation satisfies the same workload
and the same guarantees — same MissionProgram, same context plan, same dataset — so the only variable is
the provider. Four initial profiles:

    baseline-current-provider · nvidia-hosted-nim · nvidia-self-hosted-nim · nemo-retriever-enabled

The lane is a **structure that reports honestly**, and today most of it is *unexercised*: without the
GPU cluster we're requesting, the NVIDIA profiles have no inputs, so they report ``UNEXERCISED`` — never
green. The rules below are the point of the module:

  * a profile with no inputs is **unexercised, not passing**;
  * a result with no **premise witness** (evidence + a verification result) cannot enter an aggregate;
  * **failures stay in the denominator** — success rate is over *attempts*, not survivors;
  * **warm and cold** results are kept separate;
  * the **neutral lane** forbids provider-specific prompt tuning; optimized lanes are labeled and
    aggregated separately;
  * a published **summary is immutable and versioned** — Context Runtime consumes a snapshot, and a
    historical plan never mutates when new benchmark data arrives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .provider import _digest

CONTRACT_VERSION = "provider-benchmark/v1"

INITIAL_PROFILES = (
    "baseline-current-provider",
    "nvidia-hosted-nim",
    "nvidia-self-hosted-nim",
    "nemo-retriever-enabled",
)


class Group(str, Enum):
    RETRIEVAL = "retrieval"
    MODEL_EXECUTION = "model_execution"
    MISSION_EXECUTION = "mission_execution"
    OPERATIONS = "operations"


class LaneStatus(str, Enum):
    UNEXERCISED = "UNEXERCISED"   # no admissible inputs — NOT green
    EXERCISED = "EXERCISED"


@dataclass(frozen=True)
class BenchmarkArtifact:
    """One measured run. It can enter an aggregate only if it carries a premise witness — evidence
    handles AND a verification result — so an unverified or evidence-free number never counts."""
    group: Group
    profile: str
    suite_version: str
    workload_digest: str
    dataset_digest: str
    mission_program_digest: str
    context_plan_digest: str
    provider_profile_digest: str
    model_identity: str
    hardware_identity: str
    runtime_versions: Tuple[str, ...]
    success: bool
    warm: bool
    derived_metrics: Dict[str, float] = field(default_factory=dict)  # latency_ms, cost_usd, ...
    evidence: Tuple[str, ...] = ()          # premise witnesses (artifact handles)
    verification_result: str = ""           # "" = not verified → inadmissible
    optimized: bool = False                 # True → optimized lane, aggregated separately

    def admissible(self) -> bool:
        """A result enters aggregates only with a premise witness: evidence + a verification result.
        Note: an admissible result may still be a *failure* — failures stay in the denominator."""
        return bool(self.evidence) and bool(self.verification_result)

    def digest(self) -> str:
        return _digest({"group": self.group.value, "profile": self.profile,
                        "workload": self.workload_digest, "dataset": self.dataset_digest,
                        "mission": self.mission_program_digest, "context": self.context_plan_digest,
                        "provider_profile": self.provider_profile_digest,
                        "success": self.success, "warm": self.warm, "optimized": self.optimized})


@dataclass(frozen=True)
class ProfileResult:
    profile: str
    status: LaneStatus
    lane: str                    # "neutral" | "optimized"
    thermal: str                 # "warm" | "cold" | "mixed"
    attempts: int                # includes failures (the denominator)
    admissible: int
    successes: int
    success_rate: Optional[float]
    metrics: Dict[str, float]    # means over admissible results
    dropped_no_witness: int      # results excluded for missing evidence/verification


@dataclass(frozen=True)
class BenchmarkSummary:
    """An immutable, versioned snapshot the Context Runtime can consume. Producing a new summary never
    mutates a prior one — historical plans read the snapshot they were built against."""
    contract_version: str
    suite_version: str
    results: Tuple[ProfileResult, ...]

    def digest(self) -> str:
        return _digest({"contract": self.contract_version, "suite": self.suite_version,
                        "results": [(r.profile, r.lane, r.thermal, r.status.value, r.attempts,
                                     r.admissible, r.successes) for r in self.results]})

    def profile(self, name: str, *, lane: str = "neutral", thermal: str = "warm") -> Optional[ProfileResult]:
        for r in self.results:
            if r.profile == name and r.lane == lane and r.thermal == thermal:
                return r
        return None


class BenchmarkLane:
    """Collects artifacts and produces an honest, immutable summary."""

    def __init__(self, suite_version: str, profiles: Tuple[str, ...] = INITIAL_PROFILES):
        self.suite_version = suite_version
        self.profiles = profiles
        self._artifacts: List[BenchmarkArtifact] = []

    def record(self, artifact: BenchmarkArtifact) -> None:
        if artifact.suite_version != self.suite_version:
            raise ValueError("artifact suite_version does not match the lane")
        self._artifacts.append(artifact)

    def _summarize_bucket(self, profile: str, lane: str, thermal: str,
                          items: List[BenchmarkArtifact]) -> ProfileResult:
        # attempts = ALL results in the bucket (failures stay in the denominator)
        attempts = len(items)
        admissible = [a for a in items if a.admissible()]
        dropped = attempts - len(admissible)
        # BUT the denominator for success_rate is admissible attempts (verified runs), incl. their failures
        successes = sum(1 for a in admissible if a.success)
        denom = len(admissible)
        rate = (successes / denom) if denom else None
        status = LaneStatus.EXERCISED if admissible else LaneStatus.UNEXERCISED
        metrics: Dict[str, float] = {}
        if admissible:
            keys = set().union(*[a.derived_metrics.keys() for a in admissible])
            for k in keys:
                vals = [a.derived_metrics[k] for a in admissible if k in a.derived_metrics]
                if vals:
                    metrics[k] = round(sum(vals) / len(vals), 6)
        return ProfileResult(profile=profile, status=status, lane=lane, thermal=thermal,
                             attempts=attempts, admissible=len(admissible), successes=successes,
                             success_rate=rate, metrics=metrics, dropped_no_witness=dropped)

    def summarize(self) -> BenchmarkSummary:
        results: List[ProfileResult] = []
        for profile in self.profiles:
            for lane in ("neutral", "optimized"):
                for thermal in ("warm", "cold"):
                    bucket = [a for a in self._artifacts
                              if a.profile == profile
                              and ("optimized" if a.optimized else "neutral") == lane
                              and ("warm" if a.warm else "cold") == thermal]
                    # emit an UNEXERCISED row for a profile with no inputs at all (neutral/warm only, to
                    # avoid noise), and a real row wherever there are inputs
                    if bucket:
                        results.append(self._summarize_bucket(profile, lane, thermal, bucket))
            if not any(a.profile == profile for a in self._artifacts):
                results.append(ProfileResult(profile=profile, status=LaneStatus.UNEXERCISED,
                                             lane="neutral", thermal="warm", attempts=0, admissible=0,
                                             successes=0, success_rate=None, metrics={},
                                             dropped_no_witness=0))
        return BenchmarkSummary(contract_version=CONTRACT_VERSION, suite_version=self.suite_version,
                                results=tuple(results))


def _artifact(profile, *, success=True, warm=True, optimized=False, witnessed=True,
              latency=100.0, group=Group.MODEL_EXECUTION):
    return BenchmarkArtifact(
        group=group, profile=profile, suite_version="s1", workload_digest="sha256:w",
        dataset_digest="sha256:d", mission_program_digest="sha256:m", context_plan_digest="sha256:c",
        provider_profile_digest="sha256:" + profile, model_identity="model/x",
        hardware_identity="hw/x", runtime_versions=("execution-plan/v8",), success=success, warm=warm,
        derived_metrics={"latency_ms": latency}, optimized=optimized,
        evidence=("art-e",) if witnessed else (), verification_result="verified" if witnessed else "")


if __name__ == "__main__":
    checks = []
    lane = BenchmarkLane(suite_version="s1")

    # baseline: 3 warm attempts, 1 is a (witnessed) failure → rate 2/3, failure in denominator
    for ok in (True, True, False):
        lane.record(_artifact("baseline-current-provider", success=ok))
    # a result with no premise witness must NOT enter aggregates
    lane.record(_artifact("baseline-current-provider", success=True, witnessed=False))

    summ = lane.summarize()
    base = summ.profile("baseline-current-provider")
    checks.append(("failures stay in denominator", base.success_rate == 2 / 3))
    checks.append(("attempts count all incl. failures", base.attempts == 4))
    checks.append(("witness-less result dropped from aggregate", base.admissible == 3 and base.dropped_no_witness == 1))
    checks.append(("exercised when it has admissible inputs", base.status == LaneStatus.EXERCISED))

    # NVIDIA profiles have no inputs → UNEXERCISED, never green
    nim = summ.profile("nvidia-self-hosted-nim")
    checks.append(("missing NVIDIA inputs → UNEXERCISED", nim is not None and nim.status == LaneStatus.UNEXERCISED))
    checks.append(("unexercised has no success rate", nim.success_rate is None))

    # warm and cold kept separate; optimized labeled separately
    lane.record(_artifact("nvidia-hosted-nim", warm=False, latency=900.0))
    lane.record(_artifact("nvidia-hosted-nim", warm=True, latency=120.0))
    lane.record(_artifact("nvidia-hosted-nim", warm=True, optimized=True, latency=80.0))
    s2 = lane.summarize()
    checks.append(("warm and cold separated",
                   s2.profile("nvidia-hosted-nim", thermal="warm") is not None
                   and s2.profile("nvidia-hosted-nim", thermal="cold") is not None))
    checks.append(("optimized lane separated from neutral",
                   s2.profile("nvidia-hosted-nim", lane="optimized", thermal="warm") is not None))

    # summary is immutable + versioned; a new summary does not mutate the old one
    d_before = summ.digest()
    lane.record(_artifact("baseline-current-provider", success=True))
    checks.append(("prior summary is immutable", summ.digest() == d_before))
    checks.append(("summary carries contract version", s2.contract_version == CONTRACT_VERSION))

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"RESULT {passed}/{len(checks)}  ({CONTRACT_VERSION})")
    import sys
    sys.exit(0 if passed == len(checks) else 1)
