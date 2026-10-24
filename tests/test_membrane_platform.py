"""The membrane on platforms that cannot apply a memory ceiling, and the test suite's honesty about it.

macOS refuses RLIMIT_AS, so a sandbox asked for a memory ceiling there used to die inside the child with an unexplained `SubprocessError`. It now refuses BY NAME before anything runs, never runs a
capability as if the ceiling applied, and a request for no ceiling still works. These tests run on every platform by faking the probe, so the logic is tested on Linux CI too."""
from __future__ import annotations

import subprocess
import sys

import pytest

from runtime_contracts.models import ExecutionConstraint
from agentic_os.mission import membrane
from agentic_os.mission.membrane import ContainmentError, LocalContainmentSandbox

CAP = "agentic_os.mission._membrane_selftest"
_PROBE = membrane.memory_ceiling_enforceable                 # the real function, kept so the cache can be cleared even while a test has replaced it


@pytest.fixture(autouse=True)
def _fresh_probe():
    _PROBE.cache_clear()
    yield
    _PROBE.cache_clear()


def never_run(*a, **k):
    raise AssertionError("a child process was started although the ceiling cannot be applied")


def test_a_memory_ceiling_that_cannot_be_applied_is_refused_by_name_before_anything_runs(monkeypatch):
    monkeypatch.setattr(membrane, "memory_ceiling_enforceable", lambda: False)
    monkeypatch.setattr(membrane.subprocess, "run", never_run)
    sb = LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=5, max_processes=1))
    with pytest.raises(ContainmentError) as e:
        sb.invoke("op", f"{CAP}:echo", {"x": 1}, "k")
    assert "memory_ceiling_unenforceable" in str(e.value) and "RLIMIT_AS" in str(e.value)


def test_asking_for_no_memory_ceiling_still_runs_on_every_platform():
    sb = LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=0, max_duration_seconds=10, max_processes=0))
    assert sb.invoke("op", f"{CAP}:echo", {"x": 1}, "k") == {"echo": {"x": 1}}


def test_where_the_ceiling_can_be_applied_nothing_changes(monkeypatch):
    monkeypatch.setattr(membrane, "memory_ceiling_enforceable", lambda: True)
    seen = {}

    def fake_run(cmd, **kw):
        seen["preexec"] = kw.get("preexec_fn")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": {"ok": true}}', stderr="")
    monkeypatch.setattr(membrane.subprocess, "run", fake_run)
    sb = LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=5, max_processes=1))
    assert sb.invoke("op", f"{CAP}:echo", {}, "k") == {"ok": True} and callable(seen["preexec"])


@pytest.mark.parametrize("behaviour", ["nonzero", "raises"])
def test_a_probe_that_fails_or_cannot_run_means_not_enforceable(monkeypatch, behaviour):
    def fake(cmd, **kw):
        if behaviour == "raises":
            raise OSError("no fork")
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
    monkeypatch.setattr(membrane.subprocess, "run", fake)
    assert membrane.memory_ceiling_enforceable() is False


def test_a_probe_that_succeeds_means_enforceable_and_is_cached(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
    monkeypatch.setattr(membrane.subprocess, "run", fake)
    assert membrane.memory_ceiling_enforceable() is True and membrane.memory_ceiling_enforceable() is True and len(calls) == 1


def test_linux_must_enforce_the_ceiling_or_ci_would_silently_skip_the_enforcement_tests():
    """The tests that kill a memory- or CPU-hungry capability are skipped where the ceiling cannot be applied. On Linux that must never happen: if this fails, the runner no longer allows RLIMIT_AS and
    the suite has stopped testing containment."""
    if not sys.platform.startswith("linux"):
        pytest.skip("only meaningful on Linux")
    assert membrane.memory_ceiling_enforceable() is True
