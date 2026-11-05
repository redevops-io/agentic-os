"""Execution-membrane benchmarks (audit §12), scoped to a trusted host + membrane.

Benchmark A — 0 unauthorized side effects: a capability that exceeds its resource ceiling
              is killed, not run free.
Benchmark B — 0 agent-visible reusable credentials: the contained capability cannot read
              an ambient process secret.
Benchmark D — 100% tampered/expired envelopes refused, with a named reason.
Benchmark C (exactly-once) is already covered by executor.InMemoryOperatorClient's dedupe.
"""
import json
import os
import resource
import subprocess
import sys
import types

import pytest

from runtime_contracts.models import (
    EnvelopeInvalid, ExecutionConstraint, ExecutionEnvelope,
)
from runtime_contracts.models.capability import Idempotency
from agentic_os.mission import device_posture, membrane
from agentic_os.mission.membrane import (
    ContainmentError, ContainmentUnsupported, EnvelopeGate, LocalContainmentSandbox,
    membrane_supported,
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


def _fail_closed(call, monkeypatch):
    """On a platform with no containment backend (macOS) a capability must be REFUSED with the
    named ``containment_unsupported`` error, and no child process may even be created."""
    def _no_spawn(*a, **k):
        raise AssertionError("a process was started on a platform with no containment backend")
    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    with pytest.raises(ContainmentUnsupported) as ei:
        call()
    assert str(ei.value).startswith("containment_unsupported:")
    assert "memory" in str(ei.value)


# ── baseline ────────────────────────────────────────────────────────────────
def test_well_behaved_capability_runs(monkeypatch):
    if not membrane_supported():
        return _fail_closed(lambda: _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1"), monkeypatch)
    out = _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1")
    assert out == {"echo": {"x": 1}}


# ── Benchmark B: credential blindness ────────────────────────────────────────
def test_contained_capability_cannot_see_ambient_secret(monkeypatch):
    monkeypatch.setenv("MEMBRANE_TEST_SECRET", "super-secret-value")
    # Sanity: it IS in the parent environment.
    assert os.environ["MEMBRANE_TEST_SECRET"] == "super-secret-value"
    if not membrane_supported():
        return _fail_closed(
            lambda: _sandbox().invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1"), monkeypatch)
    out = _sandbox().invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1")
    assert out == {"seen": ""}          # the child saw nothing — env was scrubbed


def test_explicit_passthrough_is_the_only_way_in(monkeypatch):
    monkeypatch.setenv("MEMBRANE_TEST_SECRET", "allowed-on-purpose")
    sb = _sandbox(env_passthrough=("MEMBRANE_TEST_SECRET",))
    if not membrane_supported():
        return _fail_closed(lambda: sb.invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1"), monkeypatch)
    out = sb.invoke("op", f"{CAP}:read_ambient_secret", {}, "idem-1")
    assert out == {"seen": "allowed-on-purpose"}   # only because we opted it in


# ── Benchmark A: resource containment ────────────────────────────────────────
def test_cpu_bound_capability_is_killed(monkeypatch):
    sb = _sandbox(constraint=ExecutionConstraint(max_duration_seconds=1, max_memory_mb=512))
    if not membrane_supported():
        return _fail_closed(lambda: sb.invoke("op", f"{CAP}:busy", {}, "idem-1"), monkeypatch)
    with pytest.raises(ContainmentError):
        sb.invoke("op", f"{CAP}:busy", {}, "idem-1")


def test_memory_bound_capability_is_killed(monkeypatch):
    sb = _sandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=10))
    if not membrane_supported():
        # macOS cannot enforce this ceiling at all: the only safe outcome is that nothing runs.
        return _fail_closed(lambda: sb.invoke("op", f"{CAP}:alloc", {"mb": 4096}, "idem-1"), monkeypatch)
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


# ── platform support: the containment backend and its fail-closed signal ──────────
def test_membrane_is_supported_on_linux_and_not_on_macos():
    """Pins the probe to the platform, so a wrong 'unsupported' on Linux cannot make the
    containment tests above pass vacuously (they would assert a refusal instead of a run)."""
    if sys.platform.startswith("linux"):
        assert membrane_supported() is True
    if sys.platform == "darwin":
        assert membrane_supported() is False
        reason = device_posture.containment_unsupported_reason()
        assert "RLIMIT_AS" in reason and "darwin" in reason


@pytest.mark.parametrize("plat, supported", [
    ("linux", True), ("darwin", False), ("win32", False)])
def test_support_probe_by_platform(monkeypatch, plat, supported):
    monkeypatch.setattr(device_posture, "_host_platform", lambda: plat)
    assert membrane_supported() is supported
    assert (device_posture.containment_unsupported_reason() == "") is supported


def test_darwin_refuses_before_any_process_and_names_the_reason(monkeypatch):
    monkeypatch.setattr(device_posture, "_host_platform", lambda: "darwin")
    _fail_closed(lambda: _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1"), monkeypatch)
    # also through run_contained with an explicit constraint with NO memory ceiling: still refused
    # (there is no partially contained mode on a platform without a containment backend).
    _fail_closed(lambda: _sandbox().run_contained(
        "op", f"{CAP}:echo", {}, "k", constraint=ExecutionConstraint(max_duration_seconds=2)), monkeypatch)


def _fake_resource(log, *, refuse_as=None):
    fake = types.SimpleNamespace(**{n: getattr(resource, n) for n in dir(resource) if n.startswith("RLIMIT_")})
    def setrlimit(which, limits):
        log.append((which, limits))
        if which == resource.RLIMIT_AS and refuse_as is not None:
            raise refuse_as
    fake.setrlimit = setrlimit
    return fake


def test_non_darwin_still_applies_rlimit_as_with_the_configured_limit(monkeypatch):
    """Linux behaviour is unchanged: RLIMIT_AS (and CPU, NPROC) are set from the constraint, in the
    child's preexec, and the run proceeds. Linux itself is not exercised here (simulated)."""
    monkeypatch.setattr(device_posture, "_host_platform", lambda: "linux")
    log, seen = [], {}
    monkeypatch.setattr(membrane, "resource", _fake_resource(log))

    def fake_run(argv, **kw):
        seen.update(kw)
        kw["preexec_fn"]()                       # what the forked child would run before exec
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"ok": 1}}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    c = ExecutionConstraint(max_memory_mb=256, max_duration_seconds=7, max_processes=3)
    out = LocalContainmentSandbox(constraint=c).invoke("op", f"{CAP}:echo", {}, "k")
    assert out == {"ok": 1}
    b = 256 * 1024 * 1024
    assert (resource.RLIMIT_AS, (b, b)) in log
    assert (resource.RLIMIT_CPU, (7, 7)) in log
    assert (resource.RLIMIT_NPROC, (3, 3)) in log
    assert seen["env"].get("MEMBRANE_TEST_SECRET") is None and seen["timeout"] == 7


@pytest.mark.parametrize("err", [ValueError("current limit exceeds maximum limit"),
                                 OSError(22, "Invalid argument")])
def test_einval_on_a_platform_not_known_unsupported_still_fails_closed(monkeypatch, err):
    """If RLIMIT_AS is refused where we did not expect it, the run is refused (not swallowed, not
    run uncontained). Real subprocess + real fork; only the rlimit call is made to fail."""
    monkeypatch.setattr(device_posture, "_host_platform", lambda: "linux")
    monkeypatch.setattr(membrane, "resource", _fake_resource([], refuse_as=err))
    with pytest.raises(ContainmentError) as ei:
        _sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "idem-1")
    assert not isinstance(ei.value, ContainmentUnsupported)
    assert str(ei.value).startswith("containment_setup_failed:")
