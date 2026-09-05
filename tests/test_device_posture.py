"""Device-posture policy tests — deny-by-default, the verdict floor, and posture→ceiling.

The policy in :func:`device_posture.derive` is a pure function of observed facts, so these
run offline with synthesised :class:`DeviceFacts` — no real host probing in the policy tests.
A single smoke test exercises the real probes without asserting host-specific values.
"""
from dataclasses import replace

import pytest

from runtime_contracts.models import ExecutionConstraint
from agentic_os.mission.device_posture import (
    Decision, DeviceFacts, DevicePosture, ExecutionClass, Verdict,
    derive, probe_device, inspect,
    admit, PostureDenied, PostureBlocked, require_verified, bootstrap,
    record_posture, latest_posture,
)
from agentic_os.mission.store import EventStore
from agentic_os.mission.membrane import LocalContainmentSandbox, ContainmentError

ECHO = "agentic_os.mission._membrane_selftest:echo"

# A hardened, containment-capable dev box that meets the floor.
VERIFIED_FACTS = DeviceFacts(
    platform="linux-x86_64", container_runtime="docker",
    disk_encryption=True, secure_credential_store=True,
    network_exposure="private", gpu="NVIDIA RTX 4090",
    tailscale_connected=True, containment_supported=True,
)


def test_all_unknown_device_is_blocked_and_grants_nothing():
    """Deny-by-default: an unobserved device runs nothing."""
    p = derive(DeviceFacts())
    assert p.verdict is Verdict.BLOCKED
    assert set(p.classes) == set(ExecutionClass)                 # every class present
    assert all(d is Decision.DENY for d in p.classes.values())   # ...and all DENY
    assert all(p.ceiling(c) is None for c in ExecutionClass)     # no ceiling issuable
    assert all(not p.issuable(c) for c in ExecutionClass)


def test_hardened_device_verifies_with_expected_tiers():
    p = derive(VERIFIED_FACTS)
    assert p.verdict is Verdict.VERIFIED
    assert p.classes[ExecutionClass.LOCAL_CONTAINER] is Decision.ALLOW
    assert p.classes[ExecutionClass.HOST_PROCESS] is Decision.REVIEW
    assert p.classes[ExecutionClass.RAW_CREDENTIAL_ACCESS] is Decision.REVIEW
    # private network ⇒ not an ingress host
    assert p.classes[ExecutionClass.PUBLIC_INGRESS] is Decision.DENY


def test_floor_requires_container_runtime():
    """containment works but there is no docker/podman ⇒ BLOCKED, nothing granted."""
    facts = replace(VERIFIED_FACTS, container_runtime="")
    p = derive(facts)
    assert p.verdict is Verdict.BLOCKED
    assert all(d is Decision.DENY for d in p.classes.values())   # BLOCKED overrides per-class
    assert any("no container runtime" in r for r in p.reasons)


def test_windows_native_denies_local_container_with_wsl2_hint():
    """Windows without WSL2 can't run the rootless membrane ⇒ LOCAL_CONTAINER denied + BLOCKED."""
    facts = replace(VERIFIED_FACTS, platform="windows-amd64",
                    containment_supported=False)
    p = derive(facts)
    assert p.verdict is Verdict.BLOCKED
    assert p.classes[ExecutionClass.LOCAL_CONTAINER] is Decision.DENY
    assert any("WSL2" in r for r in p.reasons)


def test_public_exposure_makes_ingress_reviewable_not_denied():
    facts = replace(VERIFIED_FACTS, network_exposure="public")
    p = derive(facts)
    assert p.classes[ExecutionClass.PUBLIC_INGRESS] is Decision.REVIEW
    assert p.issuable(ExecutionClass.PUBLIC_INGRESS)


def test_no_secure_store_denies_credential_and_host_tiers():
    facts = replace(VERIFIED_FACTS, secure_credential_store=False)
    p = derive(facts)
    assert p.classes[ExecutionClass.RAW_CREDENTIAL_ACCESS] is Decision.DENY
    assert p.classes[ExecutionClass.HOST_PROCESS] is Decision.DENY   # needs both signals


def test_ceiling_is_a_constraint_for_issuable_classes_and_none_otherwise():
    p = derive(VERIFIED_FACTS)
    c = p.ceiling(ExecutionClass.LOCAL_CONTAINER)
    assert isinstance(c, ExecutionConstraint)
    assert c.max_memory_mb == 512 and c.max_processes == 1 and c.allow_egress == ()
    # REVIEW classes are still issuable (behind approval) ⇒ they too return a ceiling.
    assert isinstance(p.ceiling(ExecutionClass.HOST_PROCESS), ExecutionConstraint)
    # DENY ⇒ no ceiling.
    assert p.ceiling(ExecutionClass.PUBLIC_INGRESS) is None


def test_posture_id_is_content_addressed():
    a = derive(VERIFIED_FACTS)
    b = derive(VERIFIED_FACTS)
    assert a.posture_id == b.posture_id                         # deterministic
    assert a.posture_id.startswith("rcv1:")
    # a changed observed fact yields a different id (tamper-evidence)
    c = derive(replace(VERIFIED_FACTS, gpu="none"))
    assert c.posture_id != a.posture_id
    # a BLOCKED posture differs from the VERIFIED one
    assert derive(DeviceFacts()).posture_id != a.posture_id


def test_probes_run_and_inspect_returns_a_posture():
    """Smoke: the real probes never raise, and inspect() derives a posture from them."""
    facts = probe_device()
    assert isinstance(facts, DeviceFacts)
    assert facts.platform and facts.platform != "unknown-unknown"
    p = inspect()
    assert isinstance(p, DevicePosture)
    assert p.verdict in (Verdict.VERIFIED, Verdict.BLOCKED)
    assert p.posture_id.startswith("rcv1:")


# ── ledger event ─────────────────────────────────────────────────────────────────────────

def test_posture_ledger_roundtrip():
    store = EventStore()
    p = derive(VERIFIED_FACTS)
    record_posture(store, p)
    got = latest_posture(store)
    assert got is not None
    assert got.posture_id == p.posture_id      # content-addressed identity survives replay
    assert got.classes == p.classes
    assert got.facts == p.facts


def test_latest_posture_returns_most_recent():
    store = EventStore()
    record_posture(store, derive(DeviceFacts()))            # BLOCKED first
    record_posture(store, derive(VERIFIED_FACTS))           # then VERIFIED
    assert latest_posture(store).verdict is Verdict.VERIFIED
    assert latest_posture(store, scope="nope") is None


# ── admission (posture → containment clamp) ───────────────────────────────────────────────

def test_admit_clamps_request_down_to_ceiling():
    p = derive(VERIFIED_FACTS)
    big = ExecutionConstraint(
        max_memory_mb=99999, max_processes=999, max_cpu_millis=999999,
        max_duration_seconds=99999, read_paths=("/etc",), allow_egress=("evil.example",))
    eff = admit(p, ExecutionClass.LOCAL_CONTAINER, big)
    assert eff.max_memory_mb == 512 and eff.max_processes == 1        # clamped to ceiling
    assert eff.max_cpu_millis == 2000 and eff.max_duration_seconds == 30
    assert eff.read_paths == () and eff.allow_egress == ()            # ceiling grants none


def test_admit_never_widens_a_tighter_request():
    p = derive(VERIFIED_FACTS)
    tight = ExecutionConstraint(max_memory_mb=64, max_duration_seconds=5)
    eff = admit(p, ExecutionClass.LOCAL_CONTAINER, tight)
    assert eff.max_memory_mb == 64 and eff.max_duration_seconds == 5  # request wins when tighter


def test_admit_denies_a_denied_class():
    p = derive(VERIFIED_FACTS)                                        # private ⇒ ingress DENY
    with pytest.raises(PostureDenied):
        admit(p, ExecutionClass.PUBLIC_INGRESS, ExecutionConstraint())


# ── bootstrap gate ───────────────────────────────────────────────────────────────────────

def test_require_verified():
    require_verified(derive(VERIFIED_FACTS))                          # no raise
    with pytest.raises(PostureBlocked):
        require_verified(derive(DeviceFacts()))


def test_bootstrap_records_then_gates():
    store = EventStore()
    # a BLOCKED device is recorded (auditable) and then refuses to proceed
    with pytest.raises(PostureBlocked):
        bootstrap(store, prober=lambda: DeviceFacts())
    assert latest_posture(store).verdict is Verdict.BLOCKED
    # a VERIFIED device returns, and the recorded posture matches what was returned
    p = bootstrap(store, prober=lambda: VERIFIED_FACTS)
    assert p.verdict is Verdict.VERIFIED
    assert latest_posture(store).posture_id == p.posture_id


# ── the enforcement link: membrane consults the posture ───────────────────────────────────

def test_membrane_runs_within_posture_and_clamps():
    sb = LocalContainmentSandbox(posture=derive(VERIFIED_FACTS),
                                 execution_class=ExecutionClass.LOCAL_CONTAINER)
    assert sb.invoke("op", ECHO, {"x": 1}, "k1") == {"echo": {"x": 1}}


def test_membrane_refuses_a_denied_execution_class():
    sb = LocalContainmentSandbox(posture=derive(VERIFIED_FACTS),
                                 execution_class=ExecutionClass.PUBLIC_INGRESS)
    with pytest.raises(ContainmentError):
        sb.invoke("op", ECHO, {}, "k2")
