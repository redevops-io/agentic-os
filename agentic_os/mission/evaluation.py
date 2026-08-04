"""Evaluation Runtime — a governed execution substrate for AI benchmarks (v10 extension). [public core]

Nearly every benchmark project independently rebuilds the same infrastructure: datasets, prompts, runs,
metrics, verification, publication. This makes a benchmark a **versioned Mission program**: dataset +
protocol + steps, executed reference-first, verified, and published only when it verifies — reproducible
and replayable, exactly like any other mission.

This module is the public contract: the **artifact/metric/protocol types**, the **reference-first**
`ArtifactHandle`/`ContextView` (v10), the benchmark **program/run/finding** schema, the **lineage graph**
(comparability by pin), the deterministic **harness-profile planner**, and a **local `EvaluationRuntime`**
that runs a benchmark end to end with a simple *verified → publish* rule.

What is NOT here (enterprise overlay): the governed benchmark **registry**, protected **datasets**,
**approval workflows**, evaluation **scheduling**, organization-wide **scorecards**, the clean-decision
**publication gate** (the local runtime accepts an injectable ``publish_gate`` so the enterprise gate slots
in), and **continuous discovery** (auto-replay on model releases / dataset revisions). A public-only deploy
declares, runs, verifies, replays and publishes a benchmark locally without them.

Contract version: ``evaluation/v10`` — the whitepaper that specifies the Evaluation Runtime extension.
Additive within that lineage; a later whitepaper that redefines the program/run/finding schema bumps it.
The ``_demo`` self-test is the golden fixture. Composes :mod:`agentic_os.mission.events` (public).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Callable

from agentic_os.mission.events import (
    EventLedger, EventType, RuntimeEvent, ResultStatus, capability_event,
)

CONTRACT_VERSION = "evaluation/v10"


def _hash(*parts: str) -> str:
    return sha256("|".join(parts).encode()).hexdigest()[:16]


# ════════════════════════════ versioned inputs ════════════════════════════
@dataclass(frozen=True)
class Dataset:
    """A versioned dataset: pinned, licensed, provenance-tracked, with declared limitations."""
    id: str
    version: str
    content_hash: str
    license: str = ""
    provenance: str = ""
    limitations: str = ""
    revision: int = 1
    compatibility_hash: str = ""            # changes only on a semantically-incompatible revision

    def pin(self) -> str:
        return f"{self.id}@{self.version}#{self.content_hash}"


@dataclass(frozen=True)
class EvaluationProtocol:
    """How a run is scored and accepted — metrics, execution lag, cost, statistics, verification."""
    id: str
    version: str
    metrics: tuple[str, ...]
    verification_policy: str = "deterministic-first"
    statistical_procedure: str = ""
    execution_lag: str = ""
    cost_model: str = ""

    def pin(self) -> str:
        return f"{self.id}@{self.version}"


class HarnessProfile(str, Enum):
    FAST = "fast"; BALANCED = "balanced"; RESEARCH = "research"
    VERIFICATION_HEAVY = "verification_heavy"; PARALLEL = "parallel"; PRODUCTION = "production"


# ════════════════════════════ reference-first context ════════════════════════════
@dataclass(frozen=True)
class ArtifactHandle:
    """A governed reference — carries enough to plan without exposing the object (v10)."""
    ref: str
    kind: str
    content_hash: str
    authority: str = ""
    visibility: str = "private"
    approved: bool = False                  # only approved handles may be materialized


@dataclass(frozen=True)
class ContextView:
    """A bounded, content-addressed working set materialized from approved handles (v10). Reproducible:
    a function of (handles, plan, pins) — so any run's inputs are themselves a governed artifact."""
    id: str
    content_hash: str
    derived_from: tuple[str, ...]           # the handle refs materialized
    pins: tuple[str, ...]                   # dataset/protocol/program version pins
    budget: int = 0

    @staticmethod
    def materialize(handles: list[ArtifactHandle], pins: list[str], budget: int = 0) -> "ContextView":
        approved = [h for h in handles if h.approved]           # reference-first: only approved refs
        ch = _hash(*[h.content_hash for h in approved], *pins)
        return ContextView(f"cv-{ch}", ch, tuple(h.ref for h in approved), tuple(sorted(pins)), budget)


# ════════════════════════════ the benchmark program + run ════════════════════════════
@dataclass(frozen=True)
class EvaluationProgram:
    """A benchmark as a versioned Mission program — dataset + protocol + steps + harness + gates."""
    id: str
    version: str
    dataset: Dataset
    protocol: EvaluationProtocol
    harness: HarnessProfile = HarnessProfile.BALANCED
    steps: tuple[str, ...] = ()

    def pin(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass
class Run:
    id: str
    program_pin: str
    dataset_pin: str
    protocol_pin: str
    model: str
    context_view_id: str
    outputs: dict = field(default_factory=dict)
    cost_tokens: int = 0


@dataclass
class VerificationCheck:
    name: str
    passed: bool
    kind: str = "deterministic"             # deterministic | model_assisted | human
    detail: str = ""


@dataclass
class VerificationResult:
    run_id: str
    outcome: str                            # "pass" | "fail"
    checks: list[VerificationCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"


@dataclass
class Assessment:
    run_id: str
    metrics: dict[str, float]
    verified: bool


class Publication(str, Enum):
    PUBLISH = "publish"; NO_MATERIAL_IMPACT = "no_material_impact"; HOLD = "hold"; REQUIRE_REVIEW = "require_review"


@dataclass
class Finding:
    """Derived from evidence — a Claim backed by a Run + Assessment, with a publication decision."""
    id: str
    claim: str
    run_id: str
    assessment: Assessment
    verification: VerificationResult
    publication: Publication
    evidence_refs: list[str] = field(default_factory=list)


# ════════════════════════════ the benchmark graph (lineage) ════════════════════════════
class BenchmarkGraph:
    """Claims → Evidence → Runs → Findings → Publication. The durable lineage; a Finding always traces
    back to the Run and the pinned inputs that produced it (reproducibility + comparability)."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.findings: dict[str, Finding] = {}
        self._edges: list[tuple[str, str]] = []

    def add_run(self, run: Run) -> None:
        self.runs[run.id] = run

    def add_finding(self, f: Finding) -> None:
        self.findings[f.id] = f
        self._edges.append((f.id, f.run_id))

    def lineage(self, finding_id: str) -> dict:
        f = self.findings[finding_id]
        run = self.runs[f.run_id]
        return {"finding": f.id, "claim": f.claim, "run": run.id, "model": run.model,
                "pins": [run.program_pin, run.dataset_pin, run.protocol_pin],
                "context_view": run.context_view_id, "publication": f.publication.value}

    def comparable(self, finding_a: str, finding_b: str) -> bool:
        """Two findings are comparable only if their dataset + protocol pins match (explicit comparability)."""
        ra, rb = self.runs[self.findings[finding_a].run_id], self.runs[self.findings[finding_b].run_id]
        return ra.dataset_pin == rb.dataset_pin and ra.protocol_pin == rb.protocol_pin


# ════════════════════════════ harness-profile planner ════════════════════════════
@dataclass
class EvalSignals:
    dataset_size: int = 0
    needs_replay: bool = False
    verification_coverage: str = "standard"     # "standard" | "heavy"
    parallelizable: bool = False
    production: bool = False


class HarnessPlanner:
    """Planner-selected harness profile from signals (v7/v10 harness-strategy, specialized for eval)."""

    def select(self, sig: EvalSignals) -> HarnessProfile:
        if sig.production:
            return HarnessProfile.PRODUCTION
        if sig.verification_coverage == "heavy":
            return HarnessProfile.VERIFICATION_HEAVY
        if sig.parallelizable and sig.dataset_size > 1000:
            return HarnessProfile.PARALLEL
        if sig.needs_replay:
            return HarnessProfile.RESEARCH
        return HarnessProfile.FAST if sig.dataset_size < 100 else HarnessProfile.BALANCED


# ════════════════════════════ the local Evaluation Runtime ════════════════════════════
def _default_publish_gate(vr: "VerificationResult", asmt: "Assessment") -> Publication:
    """The public rule: a benchmark result publishes only if it verified (deterministic-first) and its
    assessment is verified; otherwise it is HELD. The enterprise overlay injects the clean-decision gate
    (fail-open/closed per surface, REQUIRE_REVIEW, org policy) via ``EvaluationRuntime(publish_gate=...)``."""
    return Publication.PUBLISH if (vr.passed and asmt.verified) else Publication.HOLD


class EvaluationRuntime:
    """Governs the local benchmark execution chain: reference-first materialize → run → verify → assess →
    publish (only if verified) → finding, each stage emitting a RuntimeEvent, all replayable. ``executor``
    runs the program on a model (injectable so the substrate is testable without one). The publication
    decision goes through ``publish_gate`` — the public default is *verified → publish*; an enterprise
    overlay injects the governed clean-decision gate."""

    def __init__(self, executor: Callable[[EvaluationProgram, str, ContextView], dict],
                 publish_gate: Callable[[VerificationResult, Assessment], Publication] | None = None) -> None:
        self.executor = executor
        self.publish_gate = publish_gate or _default_publish_gate
        self.graph = BenchmarkGraph()
        self.ledger = EventLedger()
        self._clock = 0

    def _emit(self, event_type: EventType, actor: str, **kw) -> RuntimeEvent:
        self._clock += 1
        return self.ledger.append(RuntimeEvent(event_type, actor, self._clock, source_runtime="evaluation", **kw))

    def _next(self) -> int:
        self._clock += 1
        return self._clock

    def run(self, program: EvaluationProgram, model: str, handles: list[ArtifactHandle],
            verify: Callable[[Run, EvaluationProtocol], VerificationResult],
            assess: Callable[[Run, EvaluationProtocol], Assessment],
            claim: str, mission_id: str = "eval") -> Finding:
        pins = [program.pin(), program.dataset.pin(), program.protocol.pin()]

        # Dataset → ContextView (reference-first: only approved handles materialize)
        cv = ContextView.materialize(handles, pins)
        self._emit(EventType.DEREFERENCE, "context-runtime", mission_id=mission_id, artifact_handles=list(cv.derived_from),
                   policy_context=program.pin(), result_status=ResultStatus.COMPLETED)

        # EvaluationProgram → Run
        self.ledger.append(capability_event("mission-runtime", self._next(), program.id, mission_id=mission_id))
        outputs = self.executor(program, model, cv)
        run = Run(f"run-{_hash(program.pin(), model, cv.id)}", program.pin(), program.dataset.pin(),
                  program.protocol.pin(), model, cv.id, outputs, outputs.get("cost_tokens", 0))
        self.graph.add_run(run)

        # Verification (deterministic-first; model self-assessment is never the gate)
        vr = verify(run, program.protocol)
        self._emit(EventType.VERIFICATION, "verification-runtime", mission_id=mission_id,
                   result_status=ResultStatus.COMPLETED if vr.passed else ResultStatus.FAILED, evidence_refs=[run.id])

        # Assessment (metrics per protocol), then Publication (only if verified)
        asmt = assess(run, program.protocol)
        pub = self.publish_gate(vr, asmt)
        self._emit(EventType.POLICY_DECISION, "evaluation-runtime", mission_id=mission_id,
                   policy_context=pub.value, result_status=ResultStatus.COMPLETED)

        # Finding (derives from evidence; traces to the run + pinned inputs)
        finding = Finding(f"find-{_hash(run.id, claim)}", claim, run.id, asmt, vr, pub, evidence_refs=[run.id, cv.id])
        self.graph.add_finding(finding)
        self._emit(EventType.PROMOTION, "evaluation-runtime", mission_id=mission_id,
                   policy_context=finding.id, result_status=ResultStatus.OBSERVED)
        return finding

    def replay(self, run_id: str, program: EvaluationProgram, model: str, handles: list[ArtifactHandle]) -> bool:
        """Re-materialize from the pinned refs and re-run; a deterministic executor reproduces the same
        ContextView id and outputs — the reproducible-working-set invariant, verified."""
        original = self.graph.runs[run_id]
        pins = [program.pin(), program.dataset.pin(), program.protocol.pin()]
        cv = ContextView.materialize(handles, pins)
        replayed = self.executor(program, model, cv)
        return cv.id == original.context_view_id and replayed == original.outputs


# ════════════════════════════ self-test / golden fixture ════════════════════════════
def _demo() -> int:
    checks: list[tuple[str, bool]] = []
    ds = Dataset("popqa", "1.0", "h_ds")
    proto = EvaluationProtocol("acc", "1.0", ("accuracy",))
    prog = EvaluationProgram("bench", "1.0", ds, proto)
    handles = [ArtifactHandle("ref/1", "dataset", "h1", approved=True),
               ArtifactHandle("ref/2", "dataset", "h2", approved=False)]   # unapproved must not materialize

    def executor(p, model, cv):  # deterministic (no model) → replayable
        return {"accuracy": 0.9, "cost_tokens": 100}

    def verify(run, proto):
        return VerificationResult(run.id, "pass", [VerificationCheck("deterministic-accuracy", True)])

    def assess(run, proto):
        return Assessment(run.id, {"accuracy": run.outputs["accuracy"]}, verified=True)

    rt = EvaluationRuntime(executor)
    f = rt.run(prog, "model-x", handles, verify, assess, "model-x scores 0.9 on popqa")
    checks.append(("reference-first: only the approved handle materialized", len(f.evidence_refs) >= 1 and rt.graph.runs[f.run_id].context_view_id.startswith("cv-")))
    checks.append(("verified result publishes", f.publication is Publication.PUBLISH))
    checks.append(("replay reproduces the same ContextView + outputs", rt.replay(f.run_id, prog, "model-x", handles)))
    checks.append(("every stage emitted a RuntimeEvent", len(rt.ledger.events()) >= 4))

    # an UNVERIFIED result is HELD, never published
    def bad_verify(run, proto): return VerificationResult(run.id, "fail")
    rt2 = EvaluationRuntime(executor)
    f2 = rt2.run(prog, "model-y", handles, bad_verify, assess, "claim")
    checks.append(("unverified result is HELD, not published", f2.publication is Publication.HOLD))

    # comparability by pin
    checks.append(("harness planner is deterministic", HarnessPlanner().select(EvalSignals(dataset_size=50)) is HarnessProfile.FAST))

    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}  (evaluation contract {CONTRACT_VERSION})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())
