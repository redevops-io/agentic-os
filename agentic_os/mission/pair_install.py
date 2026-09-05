"""Governed PAIR install — propose local AI, get approval, install with saga rollback (#2 + #3).

When the inference posture says a device is capable but PAIR is absent (INSTALL_LOCAL_PAIR), the
installer PROPOSES local AI as a governed action rather than doing it silently:

    Proposed capability installation
      NVIDIA Personal AI Router + Ollama + <model>
      Reason:        run your first AI workflows locally, no cloud API account
      Network access: local network only
      Disk:          ~8 GB
      [Approve] [Choose cloud instead]

The proposal parks for approval and executes as a saga on the mission ledger — the same
park→approve→execute→verify→compensate shape as `governed_install`, so it inherits durable state
and replay. The actual host steps (install the router, adopt/ install Ollama, **download the
model** — #3 — verify) run through an injectable `PairRunner`; on any failure the completed steps
are undone in reverse and the run is recorded ROLLED_BACK. Nothing here forks PAIR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Tuple

from .inference_posture import InferencePosture, Mode
from .pair import PairStatus, detect_pair
from .types import new_id

# ledger lifecycle (scoped by install id), mirroring governed_install
REQUESTED = "PairInstallRequested"
AWAITING = "PairInstallAwaitingApproval"
APPROVED = "PairInstallApproved"
REJECTED = "PairInstallRejected"
STEP_DONE = "PairInstallStep"
COMPLETED = "PairInstallCompleted"
ROLLED_BACK = "PairInstallRolledBack"

S_AWAITING, S_APPROVED, S_REJECTED, S_COMPLETED, S_ROLLED_BACK = (
    "AWAITING_APPROVAL", "APPROVED", "REJECTED", "COMPLETED", "ROLLED_BACK")

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_DISK_MB = 8192


class PairInstallNotApproved(RuntimeError):
    """execute called while the install is parked / rejected."""


class PairInstallBlocked(RuntimeError):
    """the device is not eligible for local AI (posture says CLOUD)."""


@dataclass(frozen=True)
class PairInstallProposal:
    components: Tuple[str, ...]
    model: str
    reason: str
    network_access: str
    est_disk_mb: int
    eligible: bool
    blocked_reason: str = ""

    @property
    def requires_approval(self) -> bool:
        return True                      # installing a local inference engine is always gated

    def as_notice(self) -> str:
        return (f"Proposed: local AI — {' + '.join(self.components)}\n"
                f"  Reason: {self.reason}\n  Network access: {self.network_access}\n"
                f"  Disk: ~{self.est_disk_mb // 1024} GB")


def propose_pair_install(posture: InferencePosture, *, model: str = DEFAULT_MODEL,
                         est_disk_mb: int = DEFAULT_DISK_MB) -> PairInstallProposal:
    """Build the local-AI install proposal from an inference posture."""
    eligible = posture.recommended_mode is Mode.INSTALL_LOCAL_PAIR or \
        posture.recommended_mode is Mode.LOCAL_PAIR
    blocked = "" if eligible else "device is not capable of running a useful local model (recommend cloud)"
    return PairInstallProposal(
        components=("NVIDIA Personal AI Router", "Ollama", model), model=model,
        reason="run your first AI workflows locally, without creating a cloud API account",
        network_access="local network only", est_disk_mb=est_disk_mb,
        eligible=eligible, blocked_reason=blocked)


class PairRunner(Protocol):
    """The narrow physical contract ReDevOps needs from a local-AI fabric — nothing more. ReDevOps
    owns the Mission, policy, approval, rollback and provider selection; PAIR owns its own inference
    fabric. Injected: the real runner shells out to the PAIR installer + Ollama; tests pass a stub.
    """

    def detect(self) -> PairStatus: ...            # already installed/serving here?
    def install_pair(self) -> None: ...            # install the router
    def install_ollama(self) -> None: ...          # install/adopt the engine
    def ensure_model(self, model: str) -> None: ...  # download the model if absent
    def start_services(self) -> None: ...          # start router + engine
    def health(self) -> PairStatus: ...            # endpoint serving + model inventory
    def uninstall(self) -> None: ...               # rollback everything this runner installed


@dataclass
class StubPairRunner:
    """In-process runner for tests: records steps; `fail_at` names a step that should raise."""

    fail_at: str = ""
    models: Tuple[str, ...] = ("qwen2.5:7b",)
    steps: List[str] = field(default_factory=list)
    undone: List[str] = field(default_factory=list)
    installed: bool = False       # detect() returns not-installed until we install

    def _do(self, step: str):
        self.steps.append(step)
        if step == self.fail_at:
            raise RuntimeError(f"{step} failed")

    def _status(self, available: bool) -> PairStatus:
        return PairStatus(available=available, base_url="http://127.0.0.1:1234/v1",
                          models=self.models if available else ())

    def detect(self): return self._status(self.installed)
    def install_pair(self): self._do("install_pair"); self.installed = True
    def install_ollama(self): self._do("install_ollama")
    def ensure_model(self, model): self._do(f"ensure_model:{model}")
    def start_services(self): self._do("start_services")
    def health(self): self._do("health"); return self._status(True)
    def uninstall(self): self.undone.append("uninstall"); self.installed = False


def request_pair_install(store, proposal: PairInstallProposal, *, install_id: Optional[str] = None):
    if not proposal.eligible:
        raise PairInstallBlocked(proposal.blocked_reason)
    install_id = install_id or new_id("pair-install")
    store.append(REQUESTED, install_id, {"components": list(proposal.components),
                                         "model": proposal.model, "disk_mb": proposal.est_disk_mb})
    store.append(AWAITING, install_id, {"notice": proposal.as_notice()})
    return install_id


def approve_pair_install(store, install_id: str, *, decision: str = "approve", actor: str = "") -> str:
    if decision == "approve":
        store.append(APPROVED, install_id, {"actor": actor})
        return S_APPROVED
    store.append(REJECTED, install_id, {"actor": actor, "decision": decision})
    return S_REJECTED


def pair_install_status(store, install_id: str) -> str:
    status = ""
    for e in store.for_mission(install_id):
        status = {REQUESTED: "REQUESTED", AWAITING: S_AWAITING, APPROVED: S_APPROVED,
                  REJECTED: S_REJECTED, COMPLETED: S_COMPLETED,
                  ROLLED_BACK: S_ROLLED_BACK}.get(e.type, status)
    return status


def execute_pair_install(store, install_id: str, proposal: PairInstallProposal, *,
                         runner: PairRunner) -> PairStatus:
    """Run the approved install as a saga: install pair → install ollama → ensure model →
    start services → health. On any failure, roll back (runner.uninstall) and record ROLLED_BACK.
    ReDevOps owns this ordering/approval/rollback; the runner only performs the physical steps."""
    status = pair_install_status(store, install_id)
    if status == S_COMPLETED:
        raise PairInstallNotApproved(f"{install_id} already completed")
    if status != S_APPROVED:
        raise PairInstallNotApproved(f"{install_id} is {status or 'unknown'}, not approved")

    done: List[str] = []
    try:
        for step, call in (("install_pair", runner.install_pair),
                           ("install_ollama", runner.install_ollama),
                           ("ensure_model", lambda: runner.ensure_model(proposal.model)),
                           ("start_services", runner.start_services)):
            call()
            done.append(step)
            store.append(STEP_DONE, install_id, {"step": step})
        result = runner.health()
        if not getattr(result, "available", False):
            raise RuntimeError("health: PAIR endpoint not serving after install")
    except Exception as e:
        try:
            runner.uninstall()          # single rollback — the runner undoes what it installed
        except Exception:
            pass
        store.append(ROLLED_BACK, install_id, {"failed_after": done, "error": type(e).__name__})
        return PairStatus(available=False)

    store.append(COMPLETED, install_id, {"base_url": result.base_url, "models": list(result.models)})
    return result
