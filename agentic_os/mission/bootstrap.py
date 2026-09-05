"""One-click bootstrap — the launcher's brain, tying the whole install vertical together.

This is the single call the native launcher (or `python -m agentic_os.mission.bootstrap`) makes,
and it runs the "one-click install, conversational configuration, capability-driven expansion"
flow end to end:

    device posture (P0.2)  →  default LLM for the first steps (no creds needed)
      →  resolve the outcome to a plan (P0.3)  →  governed install: park/approve/execute (P0.3.5)
        →  auto-provision app keys on-device (bootstrap provisioning)
          →  a plain-language onboarding summary a non-technical user can act on

Every governance property is inherited, not re-implemented: a BLOCKED device never proceeds, a
REVIEW-tier plan parks for approval, failures roll back, and the whole run is recorded on the
event ledger (durable on the P0.5 DuckDB/Postgres backend). The result carries `notices` — where
each key was saved, and how to finish setting up the LLM if the user has no credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from .default_llm import LLMChoice, resolve_llm
from .device_posture import DeviceFacts, DevicePosture, PostureBlocked, bootstrap as posture_bootstrap
from .governed_install import (
    S_APPROVED, S_AWAITING, execute_install, install_status, request_install,
)
from .inference_posture import InferencePosture, Mode, NetworkTrust, derive_inference_posture
from .installer import Deployer, InstallReceipt, default_has_credential
from .pair import PairStatus, detect_pair
from .pair_install import PairInstallProposal, propose_pair_install
from .provisioning import InstallPlan, Topology, resolve
from .types import CapabilityManifest
from .util import Fetch

# result states
BLOCKED = "BLOCKED"                 # device posture failed — nothing ran
AWAITING_APPROVAL = "AWAITING_APPROVAL"
INSTALLED = "INSTALLED"
ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class BootstrapResult:
    status: str
    posture: Optional[DevicePosture] = None
    llm: Optional[LLMChoice] = None
    plan: Optional[InstallPlan] = None
    receipt: Optional[InstallReceipt] = None
    install_id: str = ""
    notices: tuple = ()
    inference_posture: Optional[InferencePosture] = None
    #: a governed local-AI (PAIR) install to offer, when the device is capable but has no local AI
    #: and no cloud creds. The UX approves it separately (propose→approve→execute_pair_install).
    pair_proposal: Optional[PairInstallProposal] = None

    @property
    def ok(self) -> bool:
        return self.status == INSTALLED


def _summary_lines(status: str, posture, llm, plan, receipt, pair_proposal=None) -> List[str]:
    """Plain-language notices for the user, richest-signal first."""
    out: List[str] = []
    if pair_proposal is not None:
        # capable device, no local AI yet, no cloud creds → offer local AI before a cloud signup
        out.append("This computer can run AI locally — set up local AI to keep prompts on your "
                   "device (no cloud account).")
        out.append("  " + pair_proposal.as_notice().replace("\n", "\n  "))
        if llm is not None and llm.source == "guided":
            out.append(f"  Or use a cloud provider — {llm.message}")
    elif llm is not None:
        if llm.source == "pair":
            out.append("✓ Local AI found on your devices — using it (private, on your devices; "
                       "no cloud account needed).")
        elif llm.source == "local":
            out.append("✓ Using the local AI model on this device (no cloud account needed).")
        elif llm.source == "guided":
            out.append(f"LLM setup needed: {llm.message}")
        else:
            out.append(f"LLM: {llm.message}.")
    if status == BLOCKED and posture is not None:
        out.append("This device can't run the runtime yet:")
        out.extend(f"  - {r}" for r in posture.reasons)
        return out
    if plan is not None and plan.needs_approval:
        out.append("Some steps need your approval before they run: "
                   + ", ".join(plan.needs_approval))
    if receipt is not None:
        out.extend(receipt.notices)                       # e.g. "Twenty CRM: key ready … Location: …"
        missing = sorted({s for o in receipt.installed for s in o.credentials_missing})
        if missing:
            out.append("Still needs a credential: " + ", ".join(missing))
    return out


def bootstrap(outcome: str, catalog: Iterable[CapabilityManifest], *, store, deployer: Deployer,
              provisioners: Optional[Dict[str, object]] = None, secret_dir: Optional[str] = None,
              topology: Topology = Topology.STANDALONE,
              prober: Optional[Callable[[], DeviceFacts]] = None,
              env: Optional[Dict[str, str]] = None, fetch: Optional[Fetch] = None,
              has_credential: Callable[[str], bool] = default_has_credential,
              network_trust: NetworkTrust = NetworkTrust.UNKNOWN, total_memory_mb: int = 0,
              pair_detect: "Optional[Callable[[], PairStatus]]" = None,
              auto_approve: bool = False) -> BootstrapResult:
    """Run the full one-click bootstrap for one outcome. Never raises for a BLOCKED device —
    it returns a BLOCKED result carrying the reasons, so the launcher can show them.

    When the device has no cloud creds and no running local AI but is *capable* of it, the result
    carries a governed ``pair_proposal`` (local AI — install PAIR + a model), offered before a
    cloud signup. The UX approves it separately via pair_install.execute_pair_install.
    """
    # 1) device trust — records the posture; a BLOCKED device stops here (no install attempted).
    try:
        posture = posture_bootstrap(store, prober=prober)
    except PostureBlocked:
        from .device_posture import derive, probe_device
        posture = derive(prober() if prober else probe_device())
        llm = resolve_llm(env)
        return BootstrapResult(status=BLOCKED, posture=posture, llm=llm,
                               notices=tuple(_summary_lines(BLOCKED, posture, llm, None, None)))

    # 2) an LLM for the first steps, even with no creds configured. One PAIR probe is shared with
    #    the inference posture below so both agree on whether local AI is present.
    _pd = pair_detect or detect_pair
    pair_status = _pd()
    llm = resolve_llm(env, pair_detect=lambda: pair_status)

    # 2b) inference posture — can this device run local AI, and should we offer to set it up?
    infp = derive_inference_posture(posture.facts, pair=pair_status,
                                    network_trust=network_trust, total_memory_mb=total_memory_mb)
    proposal: Optional[PairInstallProposal] = None
    if llm.source == "guided" and infp.recommended_mode is Mode.INSTALL_LOCAL_PAIR:
        proposal = propose_pair_install(infp)   # a capable device with no creds → offer local AI

    def _result(status, **kw):
        notices = kw.pop("notices", None)
        return BootstrapResult(status=status, posture=posture, llm=llm,
                               inference_posture=infp, pair_proposal=proposal, notices=notices, **kw)

    # 3) resolve the outcome to a posture-gated plan
    plan = resolve(outcome, catalog, posture, topology=topology)
    if not plan.installable:
        return _result(BLOCKED, plan=plan,
                       notices=tuple(_summary_lines(BLOCKED, posture, llm, plan, None, proposal)
                                     + [f"blocked: {cap} — {why}" for cap, why in plan.blocked]))

    # 4) governed install — park for approval when the plan needs it
    req = request_install(store, plan)
    if req.status == S_AWAITING and not auto_approve:
        return _result(AWAITING_APPROVAL, plan=plan, install_id=req.install_id,
                       notices=tuple(_summary_lines(AWAITING_APPROVAL, posture, llm, plan, None, proposal)))
    if req.status == S_AWAITING and auto_approve:
        from .governed_install import approve_install
        approve_install(store, req.install_id, actor="bootstrap:auto")

    # 5) execute (with saga) + provision app keys on-device
    receipt = execute_install(store, req.install_id, plan, deployer=deployer, fetch=fetch,
                              has_credential=has_credential, provisioners=provisioners,
                              secret_dir=secret_dir)
    status = INSTALLED if receipt.ok else ROLLED_BACK
    return _result(status, plan=plan, receipt=receipt, install_id=req.install_id,
                   notices=tuple(_summary_lines(status, posture, llm, plan, receipt, proposal)))


def render_summary(result: BootstrapResult) -> str:
    """A human-readable block the launcher/CLI prints for the user."""
    head = {
        INSTALLED: "✅ Ready.",
        AWAITING_APPROVAL: "⏸  Waiting for your approval.",
        BLOCKED: "⛔ Can't proceed on this device.",
        ROLLED_BACK: "↩️  Install failed and was rolled back.",
    }.get(result.status, result.status)
    lines = [head] + [f"• {n}" for n in result.notices]
    return "\n".join(lines)


def device_report() -> int:
    """Standalone device-readiness check — the launcher's first screen / `rdo doctor`-style.

    Needs no catalog or deployer: it inspects the device posture and resolves the default LLM,
    then prints a plain report. Returns 0 when the device is VERIFIED, else 1. Invoked by the
    native launcher via `python -m agentic_os.mission.bootstrap`.
    """
    from .device_posture import inspect
    print("ReDevOps — device readiness")
    posture = inspect()
    print(f"  device : {posture.verdict.value}  ({posture.facts.platform})")
    for cls, dec in sorted(posture.classes.items(), key=lambda kv: kv[0].value):
        print(f"    {cls.value:<22} {dec.value}")
    for r in posture.reasons:
        print(f"    note: {r}")
    llm = resolve_llm()
    print(f"  llm    : {llm.message}")
    if llm.needs_setup:
        print(f"           → get a free key: {llm.signup_url}")
    return 0 if posture.verdict.value == "verified" else 1


if __name__ == "__main__":
    import sys
    sys.exit(device_report())
