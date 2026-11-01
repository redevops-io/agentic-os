"""A device posture that is not VERIFIED grants nothing, to every consumer.

`NATIVE_PROCESS_ONLY` once made `provisioning.resolve` produce an installable plan (it read the per-class map, never the verdict). The rule is
now enforced where the class decisions are read (`DevicePosture.decision/issuable/ceiling`, `provisioning.resolve`) and tested here for EVERY
verdict, with an adversarial posture whose class map says ALLOW for everything, plus a registry of the code that reads a posture, so a new
consumer cannot appear without being added here."""
from __future__ import annotations

import pathlib
import re

import pytest

from agentic_os.mission.device_posture import (
    Decision, DeviceFacts, DevicePosture, ExecutionClass, PostureBlocked, PostureDenied, Verdict, admit, derive, require_verified,
)
from agentic_os.mission.installer import StubDeployer, install
from agentic_os.mission.membrane import ContainmentError, LocalContainmentSandbox
from agentic_os.mission.provisioning import InstallBlocked, resolve
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec
from runtime_contracts.models import ExecutionConstraint

ROOT = pathlib.Path(__file__).resolve().parents[1]
GITHUB = CapabilitySpec(name="github.review", operator="devtools", provides=["code_review"], isolation_class="sandbox", secrets=["github_token"])
CRM = CapabilitySpec(name="crm.followup", operator="crm", provides=["customer_followup"], isolation_class="in_process")
CATALOG = [CapabilityManifest("devtools", [GITHUB]), CapabilityManifest("crm", [CRM])]
NATIVE_MAC = DeviceFacts(platform="darwin-arm64", secure_credential_store=True, containment_supported=True, native_process_boundary=True)
VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker", disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)


def _all_allow(verdict: Verdict) -> DevicePosture:
    """A posture whose class map grants everything but whose verdict says otherwise: what a mis-derived or hand-built posture looks like."""
    return DevicePosture(facts=DeviceFacts(), verdict=verdict, classes={c: Decision.ALLOW for c in ExecutionClass})


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_consumer_of_a_posture_refuses_a_verdict_that_is_not_verified(verdict):
    p = _all_allow(verdict)
    if verdict is Verdict.VERIFIED:
        assert p.issuable(ExecutionClass.LOCAL_CONTAINER) and require_verified(p) is None
        return
    with pytest.raises(PostureBlocked):
        require_verified(p)
    for cls in ExecutionClass:
        assert p.decision(cls) is Decision.DENY and not p.issuable(cls) and p.ceiling(cls) is None
        with pytest.raises(PostureDenied):
            admit(p, cls, ExecutionConstraint(max_duration_seconds=5))
    plan = resolve("help with code_review and customer_followup", CATALOG, p)
    assert not plan.installable and plan.blocked
    assert all(rc.decision is Decision.DENY for rc in plan.resolved)
    with pytest.raises(InstallBlocked):
        install(plan, deployer=StubDeployer(endpoints={"devtools": "http://stub/devtools"}), fetch=lambda u, t: {"operator": "devtools", "capabilities": []})
    with pytest.raises(ContainmentError):
        LocalContainmentSandbox(posture=p).run_contained("op", "agentic_os.mission._membrane_selftest:echo", {}, "k",
                                                         constraint=ExecutionConstraint(max_duration_seconds=5))


@pytest.mark.parametrize("outcome", ["help with code_review", "book me a flight"])
def test_a_non_verified_posture_makes_even_an_empty_plan_not_installable(outcome):
    """An outcome that matches nothing is vacuously installable on a verified device; on any other it is not (nothing is installable)."""
    plan = resolve(outcome, CATALOG, _all_allow(Verdict.NATIVE_PROCESS_ONLY))
    assert not plan.installable and any("posture" in reason for _, reason in plan.blocked)


def test_the_native_posture_derived_from_real_facts_is_not_installable():
    p = derive(NATIVE_MAC)
    plan = resolve("help with code_review", CATALOG, p)
    assert p.verdict is Verdict.NATIVE_PROCESS_ONLY and not plan.installable
    with pytest.raises(InstallBlocked):
        install(plan, deployer=StubDeployer(endpoints={"devtools": "http://stub/devtools"}), fetch=lambda u, t: {})


def test_a_verified_posture_still_installs_what_it_always_did():
    plan = resolve("help with code_review", CATALOG, derive(VERIFIED))
    assert plan.installable and not plan.blocked and plan.resolved[0].decision is Decision.ALLOW


# ── the registry of code that reads a posture ──

# Files that read `.classes`, `.verdict`, `Verdict` or the decision helpers of a DevicePosture, and why each is safe. A new file in this
# list's pattern must be reviewed against "a posture that is not VERIFIED grants nothing" and added here.
KNOWN_READERS = {
    "agentic_os/mission/device_posture.py": "defines the rule (decision/issuable/ceiling/require_verified)",
    "agentic_os/mission/provisioning.py": "resolve() uses posture.decision and adds a device-level block for a non-verified verdict",
    "agentic_os/mission/bootstrap.py": "bootstrap() goes through require_verified; device_report only prints and exits 1 unless verified",
    "agentic_os/mission/membrane.py": "admit() (decision-based) clamps or refuses",
    "deploy/launcher/acceptance_check.py": "compares the verdict with VERIFIED",
}
_READ = re.compile(r"posture\.(classes|verdict|decision|issuable|ceiling)\b|\.classes\.(get|items)|Verdict\.(VERIFIED|BLOCKED|NATIVE_PROCESS_ONLY)\b|"
                   r"verdict\.value\b|\badmit\(|require_verified\(")


def test_the_code_that_reads_a_device_posture_is_a_known_set():
    found = set()
    for base in ("agentic_os", "deploy"):
        for f in (ROOT / base).rglob("*.py"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            if "device_posture" in text or f.name == "device_posture.py":
                if _READ.search(text):
                    found.add(f.relative_to(ROOT).as_posix())
    assert found <= set(KNOWN_READERS), f"new reader of a device posture, review it and add it to KNOWN_READERS: {sorted(found - set(KNOWN_READERS))}"
