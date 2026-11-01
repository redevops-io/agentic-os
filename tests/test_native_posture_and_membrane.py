"""The membrane and the device posture on hosts without POSIX resource limits or a container runtime (native Windows, native macOS).

Three promises, each tested on every OS (the Windows case is also SIMULATED on POSIX by hiding the `resource` module, so the logic is tested
on Linux CI too):

* importing the membrane, the execution backend and the posture never fails for want of a POSIX module;
* where a memory ceiling cannot be enforced the membrane refuses BY NAME (`CapabilityUnavailable`) before it starts anything, and the backend does
  not advertise resource limits it does not enforce;
* a native host with no container runtime gets an explicit `NATIVE_PROCESS_ONLY` posture that claims no isolation and denies every high-risk class;
  Linux and every posture recorded before the new type existed are unchanged."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from agentic_os.mission import device_posture as dp
from agentic_os.mission import membrane
from agentic_os.mission.device_posture import (
    Decision, DeviceFacts, ExecutionClass, PostureBlocked, PostureDenied, Verdict, admit, bootstrap, derive, latest_posture, require_verified,
)
from agentic_os.mission.membrane import CapabilityUnavailable, ContainmentError, LocalContainmentSandbox
from agentic_os.mission.store import MemoryEventStore
from runtime_contracts.models import ExecutionConstraint
from tests._proc_support import child_env

CAP = "agentic_os.mission._membrane_selftest"

NATIVE_MAC = DeviceFacts(platform="darwin-arm64", secure_credential_store=True, containment_supported=True, native_process_boundary=True)
NATIVE_WIN = DeviceFacts(platform="windows-amd64", secure_credential_store=True, containment_supported=False, native_process_boundary=True)


# ── import without POSIX modules (a simulated Windows) ──

_NO_POSIX = textwrap.dedent("""
    import importlib.abc, json, os, sys
    BLOCKED = {"fcntl", "resource", "termios", "pwd", "grp", "pty", "tty"}
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("No module named %r (simulated: not POSIX)" % name)
    sys.meta_path.insert(0, Block())
    for attr in ("geteuid", "getuid", "fork", "killpg", "setsid", "chown"):
        if hasattr(os, attr):
            delattr(os, attr)
    mods = json.loads(sys.argv[1])
    failed = {}
    import importlib
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            failed[m] = "%s: %s" % (type(e).__name__, e)
    from agentic_os.mission import membrane, device_posture
    from agentic_os.mission.execution_backend import MembraneExecutionBackend
    out = {"failed": failed, "resource_is_none": membrane.resource is None,
           "ceiling": membrane.memory_ceiling_enforceable(), "caps": membrane.containment_capabilities(),
           "resource_limits_advertised": MembraneExecutionBackend().capabilities().resource_limits}
    from runtime_contracts.models import ExecutionConstraint
    sb = membrane.LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=5, max_processes=1))
    try:
        sb.invoke("op", "agentic_os.mission._membrane_selftest:echo", {"x": 1}, "k")
        out["refusal"] = None
    except membrane.CapabilityUnavailable as e:
        out["refusal"] = [e.capability, str(e)]
    print(json.dumps(out))
""")


def test_nothing_imports_a_posix_only_module_unguarded():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "scripts" / "native_core_manifest.json").read_text(encoding="utf-8"))
    mods = sorted(set(manifest["modules"]) | {
        "agentic_os.mission.membrane", "agentic_os.mission.execution_backend", "agentic_os.mission.device_posture",
        "agentic_os.mission.fork_join", "agentic_os.mission.gate_notify", "agentic_os.outcome_store", "agentic_os.permissions_setup",
        "agentic_os.secrets.stores", "agentic_os.secrets.bootstrap"})
    if os.name != "nt":
        mods = [m for m in mods if m != "agentic_os._platform._win32"]          # the Windows API module imports msvcrt by design: it is loaded on Windows only
    p = subprocess.run([sys.executable, "-c", _NO_POSIX, json.dumps(mods)], capture_output=True, text=True, timeout=180,
                       env=child_env(), cwd=str(root))
    assert p.returncode == 0, p.stderr[-1500:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["failed"] == {}, out["failed"]
    assert out["resource_is_none"] is True and out["ceiling"] is False
    assert out["caps"]["memory_ceiling"] is False and out["caps"]["posix_rlimits"] is False
    assert out["caps"]["scrubbed_environment"] and out["caps"]["wall_clock_timeout"]
    assert out["resource_limits_advertised"] is False                          # no resource limits are claimed where none are enforced
    assert out["refusal"][0] == "memory_ceiling" and out["refusal"][1].startswith("memory_ceiling_unenforceable")


# ── the membrane where it cannot enforce a memory ceiling ──

def test_a_missing_ceiling_capability_is_a_named_refusal_that_starts_nothing(monkeypatch):
    monkeypatch.setattr(membrane, "memory_ceiling_enforceable", lambda: False)
    monkeypatch.setattr(membrane.subprocess, "run", lambda *a, **k: pytest.fail("a child process was started"))
    sb = LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=256, max_duration_seconds=5, max_processes=1))
    with pytest.raises(CapabilityUnavailable) as e:
        sb.invoke("op", f"{CAP}:echo", {}, "k")
    assert isinstance(e.value, ContainmentError) and e.value.capability == "memory_ceiling"
    assert str(e.value).startswith("memory_ceiling_unenforceable") and "RLIMIT_AS" in str(e.value)


def test_without_the_resource_module_no_preexec_function_is_passed_and_the_env_is_scrubbed(monkeypatch):
    """Windows rejects `preexec_fn`; the membrane must not pass one where it has no rlimits to apply, and must still scrub the environment."""
    monkeypatch.setattr(membrane, "resource", None)
    monkeypatch.setattr(membrane, "memory_ceiling_enforceable", lambda: False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-leak")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": {"ok": true}}', stderr="")
    monkeypatch.setattr(membrane.subprocess, "run", fake_run)
    sb = LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=0, max_duration_seconds=7, max_processes=0))
    assert sb.invoke("op", f"{CAP}:echo", {}, "k") == {"ok": True}
    assert "preexec_fn" not in seen and seen["timeout"] == 7
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"] and "MEMBRANE_PATH" in seen["env"]


def test_the_capabilities_report_what_the_platform_enforces(monkeypatch):
    caps = membrane.containment_capabilities()
    assert caps["scrubbed_environment"] and caps["contained_cwd"] and caps["wall_clock_timeout"]
    assert caps["memory_ceiling"] is membrane.memory_ceiling_enforceable()
    monkeypatch.setattr(membrane, "resource", None)
    membrane.memory_ceiling_enforceable.cache_clear()
    try:
        assert membrane.memory_ceiling_enforceable() is False and membrane.containment_capabilities()["posix_rlimits"] is False
    finally:
        membrane.memory_ceiling_enforceable.cache_clear()


def test_the_backend_does_not_advertise_resource_limits_it_cannot_apply(monkeypatch):
    from agentic_os.mission import execution_backend as eb
    monkeypatch.setattr(eb, "memory_ceiling_enforceable", lambda: False)
    assert eb.MembraneExecutionBackend().capabilities().resource_limits is False
    monkeypatch.setattr(eb, "memory_ceiling_enforceable", lambda: True)
    assert eb.MembraneExecutionBackend().capabilities() == eb.MEMBRANE_CAPABILITIES


# ── the native posture ──

@pytest.mark.parametrize("facts", [NATIVE_MAC, NATIVE_WIN])
def test_a_native_host_without_a_container_runtime_is_reported_as_such_and_is_not_verified(facts):
    p = derive(facts)
    assert p.verdict is Verdict.NATIVE_PROCESS_ONLY and p.verdict.value == "native_process_only"
    text = " ".join(p.reasons)
    assert "native process boundary only" in text and "no container runtime" in text
    assert "no filesystem or network isolation" in text                      # says what it is NOT
    assert "isolat" not in text.replace("no filesystem or network isolation", "")   # and never claims isolation
    with pytest.raises(PostureBlocked, match="native process boundary only"):
        require_verified(p)                                                   # the installer gate still refuses it


@pytest.mark.parametrize("facts", [NATIVE_MAC, NATIVE_WIN])
def test_every_high_risk_class_is_denied_under_a_native_process_boundary(facts):
    p = derive(facts)
    for cls in (ExecutionClass.HOST_PROCESS, ExecutionClass.RAW_CREDENTIAL_ACCESS, ExecutionClass.PUBLIC_INGRESS):
        assert p.classes[cls] is Decision.DENY and p.ceiling(cls) is None
        with pytest.raises(PostureDenied):
            admit(p, cls, ExecutionConstraint(max_duration_seconds=5))
    # even a hardened native device (encrypted disk, credential store) gets no host-process class without a container runtime
    hard = derive(DeviceFacts(platform="darwin-arm64", disk_encryption=True, secure_credential_store=True, containment_supported=True,
                              network_exposure="public", native_process_boundary=True))
    assert hard.verdict is Verdict.NATIVE_PROCESS_ONLY
    assert hard.classes[ExecutionClass.HOST_PROCESS] is Decision.DENY and hard.classes[ExecutionClass.PUBLIC_INGRESS] is Decision.DENY


def test_the_one_remaining_class_needs_a_human_where_limits_exist_and_is_denied_where_they_do_not():
    assert derive(NATIVE_MAC).classes[ExecutionClass.LOCAL_CONTAINER] is Decision.REVIEW
    win = derive(NATIVE_WIN)
    assert win.classes[ExecutionClass.LOCAL_CONTAINER] is Decision.DENY
    assert "POSIX-only" in " ".join(win.reasons) and "WSL2" not in " ".join(win.reasons)
    with pytest.raises(ContainmentError):                                      # the sandbox honours the denial (posture -> containment link)
        LocalContainmentSandbox(posture=win).run_contained("op", f"{CAP}:echo", {}, "k", constraint=ExecutionConstraint(max_duration_seconds=5))


def test_linux_without_a_container_runtime_and_unknown_hosts_stay_blocked():
    linux = derive(DeviceFacts(platform="linux-x86_64", containment_supported=True, secure_credential_store=True))
    assert linux.verdict is Verdict.BLOCKED and all(d is Decision.DENY for d in linux.classes.values())
    wsl = derive(DeviceFacts(platform="windows-wsl2-x86_64", containment_supported=True))
    assert wsl.verdict is Verdict.BLOCKED
    assert derive(DeviceFacts()).verdict is Verdict.BLOCKED


@pytest.mark.parametrize("facts,expected", [
    (DeviceFacts(), "rcv1:b46b5c2e8429e6dd7e335232d12576d176710a3a450aab5439286f68ccc6f3f5"),
    (DeviceFacts(platform="linux-x86_64", container_runtime="docker", disk_encryption=True, secure_credential_store=True,
                 network_exposure="private", containment_supported=True), "rcv1:18099450889a5d60af61f5143a09aaad3964461f109806375a302d8e25c7fc43"),
    (DeviceFacts(platform="linux-x86_64", containment_supported=True, secure_credential_store=True),
     "rcv1:3c2e46359b1cf04a6a40d88b761dc326be2bc7b01b584bd6a1bea9db9b9ced12"),
    (DeviceFacts(platform="windows-amd64", container_runtime="docker", secure_credential_store=True),
     "rcv1:80115084b3877d5a746c5d72d1d3a1c13d71cb992b8a0a0689d957cf358edff7"),
])
def test_postures_that_predate_the_native_type_keep_their_ids(facts, expected):
    """The new fact is left out of the canonical form while False, so these ids (computed with the code before the change) do not move."""
    assert "native_process_boundary" not in facts.canonical_form()
    assert derive(facts).posture_id == expected


def test_the_probe_marks_native_desktops_only(monkeypatch):
    for probe in ("_probe_container_runtime", "_probe_tailscale", "_probe_gpu"):
        monkeypatch.setattr(dp, probe, lambda: "" if probe in ("_probe_container_runtime", "_probe_gpu") else False)
    monkeypatch.setattr(dp, "_probe_containment_supported", lambda: False)
    for platform_str, native in [("windows-amd64", True), ("darwin-arm64", True), ("windows-wsl2-x86_64", False), ("linux-x86_64", False)]:
        monkeypatch.setattr(dp, "_probe_platform", lambda p=platform_str: p)
        assert dp.probe_device().native_process_boundary is native, platform_str


def test_the_native_posture_is_recorded_replayed_and_stops_the_bootstrap():
    store = MemoryEventStore()
    with pytest.raises(PostureBlocked, match="native process boundary only"):
        bootstrap(store, prober=lambda: NATIVE_MAC)
    back = latest_posture(store)
    assert back.verdict is Verdict.NATIVE_PROCESS_ONLY and back.facts.native_process_boundary is True
    assert back.posture_id == derive(NATIVE_MAC).posture_id                      # the replayed posture is the derived one, byte for byte
