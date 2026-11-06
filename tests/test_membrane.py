"""Execution-membrane benchmarks (audit §12), scoped to a trusted host + membrane.

Benchmark A — 0 unauthorized side effects: a capability that exceeds its resource ceiling
              is killed, not run free.
Benchmark B — 0 agent-visible reusable credentials: the contained capability cannot read
              an ambient process secret.
Benchmark D — 100% tampered/expired envelopes refused, with a named reason.
Benchmark C (exactly-once) is already covered by executor.InMemoryOperatorClient's dedupe.
"""
import os

import pytest

import sys as _sys                                                     # noqa: E402
from pathlib import Path as _Path                                      # noqa: E402

_sys.path.insert(0, str(_Path(__file__).parent))
from _platform_facts import needs_memory_ceiling                      # noqa: E402

from runtime_contracts.models import (
    EnvelopeInvalid, ExecutionConstraint, ExecutionEnvelope,
)
from runtime_contracts.models.capability import Idempotency
from agentic_os.mission.membrane import (
    ContainmentError, EnvelopeGate, LocalContainmentSandbox,
)

CAP = "agentic_os.mission._membrane_selftest"


def _sandbox(**over):
    c = ExecutionConstraint(max_memory_mb=512, max_duration_seconds=2, max_processes=64)
    return LocalContainmentSandbox(constraint=over.pop("constraint", c), **over)


def _envelope(**over):
    base = dict(mission_id="m-1", plan_fingerprint="rcv1:p", capability_id="cap.demo",
                authority="g7", target="local", parameters={"n": "1"},
                idempotency=Idempotency.AT_MOST_ONCE, idempotency_key="idem-1",
                not_after="2999-01-01T00:00:00Z")
    base.update(over)
    return ExecutionEnvelope(**base)


# ── baseline ────────────────────────────────────────────────────────────────
@needs_memory_ceiling
def test_well_behaved_capability_runs():
    out = _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1")
    assert out == {"echo": {"x": 1}}


# ── Benchmark B: credential blindness ────────────────────────────────────────
@needs_memory_ceiling
def test_contained_capability_cannot_see_ambient_secret(monkeypatch):
    monkeypatch.setenv("MEMBRANE_TEST_SECRET", "super-secret-value")
    # Sanity: it IS in the parent environment.
    assert os.environ["MEMBRANE_TEST_SECRET"] == "super-secret-value"
    out = _sandbox().invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1")
    assert out == {"seen": ""}          # the child saw nothing — env was scrubbed


@needs_memory_ceiling
def test_explicit_passthrough_is_the_only_way_in(monkeypatch):
    monkeypatch.setenv("MEMBRANE_TEST_SECRET", "allowed-on-purpose")
    sb = _sandbox(env_passthrough=("MEMBRANE_TEST_SECRET",))
    out = sb.invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1")
    assert out == {"seen": "allowed-on-purpose"}   # only because we opted it in


# ── Benchmark A: resource containment ────────────────────────────────────────
@needs_memory_ceiling
def test_cpu_bound_capability_is_killed():
    sb = _sandbox(constraint=ExecutionConstraint(max_duration_seconds=1, max_memory_mb=512))
    with pytest.raises(ContainmentError):
        sb.invoke("op", f"{CAP}:busy", {}, "idem-1")


@needs_memory_ceiling
def test_memory_bound_capability_is_killed():
    sb = _sandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=10))
    with pytest.raises(ContainmentError):
        sb.invoke("op", f"{CAP}:alloc", {"mb": 4096}, "idem-1")


# ── Benchmark D: envelope validation ─────────────────────────────────────────
def test_valid_envelope_passes_the_gate():
    e = _envelope()
    EnvelopeGate().validate(e, expected_binding=e.binding)   # no raise


def test_tampered_envelope_refused():
    issued = _envelope()
    forged = _envelope(authority="grant-forged")             # attacker altered a field
    gate = EnvelopeGate()
    with pytest.raises(EnvelopeInvalid) as ei:
        gate.validate(forged, expected_binding=issued.binding)
    assert str(ei.value) == "binding_mismatch"
    assert gate.refusal_receipt(forged, "binding_mismatch").outcome == "refused"


def test_expired_envelope_refused():
    e = _envelope(not_after="2000-01-01T00:00:00Z")
    with pytest.raises(EnvelopeInvalid) as ei:
        EnvelopeGate(now=lambda: 4102444800.0).validate(e, expected_binding=e.binding)
    assert str(ei.value) == "envelope_expired"


# ── receipt binding ──────────────────────────────────────────────────────────
def test_success_receipt_is_bound_to_the_envelope():
    e = _envelope()
    sb = _sandbox()
    r = sb.receipt(e, {"echo": {"x": 1}}, started=1.0, finished=2.0)
    assert r.envelope_binding == e.binding and r.outcome == "executed"
    assert r.side_effect_digest.startswith("rcv1:")


# ── platform support probe (from the RC line, kept after the convergence) ─────────────────────────
# The RC line refused EVERY run on macOS (``ContainmentUnsupported``); origin/main's per-request rule governs the membrane now (a memory
# ceiling that cannot be applied is refused by name, ``CapabilityUnavailable``; see tests/test_native_posture_and_membrane.py). What
# remains of the RC work is the platform-level answer the device report uses, tested here.
import subprocess                                                      # noqa: E402
import sys                                                             # noqa: E402

from agentic_os.mission import device_posture                          # noqa: E402
from agentic_os.mission.membrane import membrane_supported             # noqa: E402


def test_membrane_is_supported_on_linux_and_not_on_macos():
    """Pins the probe to the platform, so a wrong 'unsupported' on Linux cannot make the containment tests above pass vacuously."""
    if sys.platform.startswith("linux"):
        assert membrane_supported() is True
    if sys.platform == "darwin":
        assert membrane_supported() is False
        reason = device_posture.containment_unsupported_reason()
        assert "RLIMIT_AS" in reason and "darwin" in reason


@pytest.mark.parametrize("plat, supported", [("linux", True), ("darwin", False), ("win32", False)])
def test_support_probe_by_platform(monkeypatch, plat, supported):
    monkeypatch.setattr(device_posture, "_host_platform", lambda: plat)
    assert membrane_supported() is supported
    assert (device_posture.containment_unsupported_reason() == "") is supported


def test_a_ceiling_request_on_a_platform_that_cannot_apply_it_starts_no_process(monkeypatch):
    """macOS-shaped host: a memory ceiling is requested, none can be applied: refused by name before any child exists."""
    from agentic_os.mission import membrane
    monkeypatch.setattr(membrane, "memory_ceiling_enforceable", lambda: False)

    def _no_spawn(*a, **k):
        raise AssertionError("a process was started although the memory ceiling cannot be enforced")
    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    with pytest.raises(membrane.CapabilityUnavailable) as ei:
        _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1")
    assert ei.value.capability == "memory_ceiling" and str(ei.value).startswith("memory_ceiling_unenforceable")
