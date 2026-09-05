"""Device posture — the trust an install establishes *before* Sidekick recruits anything.

The membrane (:mod:`.membrane`) answers *"even if the agent is wrong, what can this one
action physically reach?"* This module answers the question that must be settled one step
earlier, at install time: *"what is this device allowed to do at all?"* Posture is therefore
a **containment input**, not a separate security product — it constrains which
:class:`~runtime_contracts.models.ExecutionConstraint` presets the membrane may ever issue
on this host.

Three types, no infrastructure, same discipline as the execution contract:

* :class:`DeviceFacts` — what was *observed* about the host (platform, container runtime,
  disk encryption, secure credential store, network exposure, GPU, Tailscale, whether the
  rootless membrane can even run here). Every field defaults to the **conservative** value,
  so a probe that fails or cannot answer never widens authority.
* :class:`DevicePosture` — the *derived* verdict: a decision per :class:`ExecutionClass`
  (ALLOW / REVIEW / DENY), an overall :attr:`verdict` (VERIFIED / BLOCKED), the reasons, and
  a content-addressed :attr:`posture_id` — change any observed fact and the id changes, so a
  posture is tamper-evident exactly like an :class:`~runtime_contracts.models.ExecutionEnvelope`.
* :class:`ExecutionClass` — the coarse risk tiers the plan asks for
  (``LOCAL_CONTAINER`` < ``HOST_PROCESS`` < ``RAW_CREDENTIAL_ACCESS`` / ``PUBLIC_INGRESS``),
  each mapped by :meth:`DevicePosture.ceiling` to a concrete ``ExecutionConstraint`` — the
  named preset over the fine-grained contract, never a parallel enforcement system.

Deny-by-default is the whole point: an all-unknown device is BLOCKED and grants no class.
The policy in :func:`derive` is a pure function of :class:`DeviceFacts`, so it is
deterministic and testable offline; :func:`probe_device` is the only part that touches the
real host, and each probe is best-effort and conservative on failure.
"""
from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Tuple

from runtime_contracts.canonical import content_hash
from runtime_contracts.models import ExecutionConstraint

CONTRACT_VERSION = "device-posture/v1"


class ExecutionClass(str, Enum):
    """Coarse risk tiers a device may or may not be trusted to run.

    Ordered least → most dangerous. A tier is a *named preset* over ``ExecutionConstraint``
    (see :meth:`DevicePosture.ceiling`), not a new enforcement mechanism.
    """

    #: A rootless, resource-capped child process (the :class:`.membrane.LocalContainmentSandbox`).
    LOCAL_CONTAINER = "local_container"
    #: Code running directly on the host, outside the membrane. High trust required.
    HOST_PROCESS = "host_process"
    #: Holding a *decrypted* secret at rest on this device (vs. a broker reference).
    RAW_CREDENTIAL_ACCESS = "raw_credential_access"
    #: Accepting inbound connections from the public internet on this device.
    PUBLIC_INGRESS = "public_ingress"


class Decision(str, Enum):
    ALLOW = "allow"     #: issuable without further gating
    REVIEW = "review"   #: issuable only behind an explicit human approval
    DENY = "deny"       #: never issuable on this device


class Verdict(str, Enum):
    VERIFIED = "verified"   #: the mandatory floor is met; the device may host missions
    BLOCKED = "blocked"     #: the floor is not met; no execution class is granted


#: The network posture strings a probe may report.
_NET_PRIVATE, _NET_PUBLIC, _NET_UNKNOWN = "private", "public", "unknown"


@dataclass(frozen=True)
class DeviceFacts:
    """What was observed about the host. Conservative defaults = an unknown, untrusted box."""

    platform: str = "unknown"
    #: "docker" | "podman" | "" (none found).
    container_runtime: str = ""
    disk_encryption: bool = False
    secure_credential_store: bool = False
    #: "private" | "public" | "unknown".
    network_exposure: str = _NET_UNKNOWN
    #: GPU model string, or "" when none detected.
    gpu: str = ""
    tailscale_connected: bool = False
    #: Whether the rootless membrane can actually run here (posix + `resource`).
    containment_supported: bool = False

    def canonical_form(self) -> Dict[str, object]:
        return {
            "platform": self.platform,
            "container_runtime": self.container_runtime,
            "disk_encryption": self.disk_encryption,
            "secure_credential_store": self.secure_credential_store,
            "network_exposure": self.network_exposure,
            "gpu": self.gpu,
            "tailscale_connected": self.tailscale_connected,
            "containment_supported": self.containment_supported,
        }


@dataclass(frozen=True)
class DevicePosture:
    """The derived, content-addressed verdict for a device."""

    facts: DeviceFacts
    verdict: Verdict
    #: Decision per execution class. Every class is present; absent ⇒ programmer error.
    classes: Dict[ExecutionClass, Decision]
    reasons: Tuple[str, ...] = ()
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "facts": self.facts.canonical_form(),
            "verdict": self.verdict.value,
            "classes": {c.value: d.value for c, d in sorted(
                self.classes.items(), key=lambda kv: kv[0].value)},
            "reasons": sorted(self.reasons),
        }

    @property
    def posture_id(self) -> str:
        """Content-addressed identity — any changed observed fact yields a new id."""
        return content_hash(self.canonical_form())

    def issuable(self, cls: ExecutionClass) -> bool:
        """True if this device may run ``cls`` at all (ALLOW or REVIEW), False if DENY."""
        return self.classes.get(cls, Decision.DENY) is not Decision.DENY

    def ceiling(self, cls: ExecutionClass) -> ExecutionConstraint | None:
        """The tightest ``ExecutionConstraint`` this device permits for ``cls``.

        ``None`` when the class is DENY'd — the membrane must not issue any constraint for it.
        Otherwise a *ceiling*: a concrete request is admissible only if it stays inside this.
        """
        if not self.issuable(cls):
            return None
        return _CEILINGS[cls]


# ── The ceiling presets: ExecutionClass → ExecutionConstraint (deny-by-default) ──────────
# These are the "named presets over ExecutionConstraint" the design calls for. Each widens
# the empty (grant-nothing) constraint only as far as its tier warrants.
_CEILINGS: Dict[ExecutionClass, ExecutionConstraint] = {
    # A contained compute step: the membrane supplies a throwaway cwd, so no host paths and
    # no egress are granted; only modest resource ceilings.
    ExecutionClass.LOCAL_CONTAINER: ExecutionConstraint(
        max_cpu_millis=2000, max_memory_mb=512, max_duration_seconds=30,
        max_processes=1, max_concurrency=1),
    # Uncontained host code — wider ceilings, but still no ambient egress by default; only
    # ever reached under REVIEW, where a human widens paths/egress deliberately.
    ExecutionClass.HOST_PROCESS: ExecutionConstraint(
        max_cpu_millis=8000, max_memory_mb=4096, max_duration_seconds=300,
        max_processes=16, max_concurrency=4),
    # A credential-holding step: tightest compute ceiling; egress stays empty so a leaked
    # secret cannot be exfiltrated by the same step that reads it.
    ExecutionClass.RAW_CREDENTIAL_ACCESS: ExecutionConstraint(
        max_memory_mb=256, max_duration_seconds=15, max_processes=1, max_concurrency=1),
    # An ingress-serving role: a server preset. Egress is intentionally left empty here —
    # a listener is not an outbound client; add allow_egress explicitly when it needs one.
    ExecutionClass.PUBLIC_INGRESS: ExecutionConstraint(
        max_cpu_millis=8000, max_memory_mb=2048, max_duration_seconds=0,
        max_processes=8, max_concurrency=32),
}


def derive(facts: DeviceFacts) -> DevicePosture:
    """Pure policy: :class:`DeviceFacts` → :class:`DevicePosture`. Deny-by-default.

    The mandatory floor for VERIFIED is a usable containment substrate: a container runtime
    *and* the rootless membrane actually running here. Absent either, the device is BLOCKED
    and every class is DENY'd — an install that cannot contain anything must not run anything.
    """
    reasons: list[str] = []
    classes: Dict[ExecutionClass, Decision] = {}

    # LOCAL_CONTAINER — the baseline. Allowed iff the rootless membrane can run here.
    if facts.containment_supported:
        classes[ExecutionClass.LOCAL_CONTAINER] = Decision.ALLOW
    else:
        classes[ExecutionClass.LOCAL_CONTAINER] = Decision.DENY
        reasons.append("containment unsupported on this platform "
                       "(rootless membrane needs a POSIX host; on Windows run inside WSL2)")

    # HOST_PROCESS — only on a hardened device, and never without a human.
    if facts.disk_encryption and facts.secure_credential_store:
        classes[ExecutionClass.HOST_PROCESS] = Decision.REVIEW
    else:
        classes[ExecutionClass.HOST_PROCESS] = Decision.DENY
        reasons.append("host-process execution denied: needs disk encryption "
                       "+ a secure credential store")

    # RAW_CREDENTIAL_ACCESS — prefer broker references; raw-at-rest needs a secure store + review.
    if facts.secure_credential_store:
        classes[ExecutionClass.RAW_CREDENTIAL_ACCESS] = Decision.REVIEW
    else:
        classes[ExecutionClass.RAW_CREDENTIAL_ACCESS] = Decision.DENY
        reasons.append("raw credential access denied: no secure credential store "
                       "(use CredentialBroker references instead)")

    # PUBLIC_INGRESS — a personal device is not an ingress host. Private ⇒ DENY; a box with a
    # public exposure ⇒ REVIEW (it *could* serve, but only a human designates it); unknown ⇒ DENY.
    if facts.network_exposure == _NET_PUBLIC:
        classes[ExecutionClass.PUBLIC_INGRESS] = Decision.REVIEW
    else:
        classes[ExecutionClass.PUBLIC_INGRESS] = Decision.DENY
        if facts.network_exposure == _NET_PRIVATE:
            reasons.append("public ingress denied: device is on a private network")
        else:
            reasons.append("public ingress denied: network exposure unknown")

    # Verdict floor.
    floor_ok = bool(facts.container_runtime) and facts.containment_supported
    if floor_ok:
        verdict = Verdict.VERIFIED
    else:
        verdict = Verdict.BLOCKED
        if not facts.container_runtime:
            reasons.append("blocked: no container runtime (docker/podman) found")
        # containment reason already recorded above when unsupported.
        # A BLOCKED device grants nothing, whatever the per-class analysis said.
        classes = {c: Decision.DENY for c in ExecutionClass}

    return DevicePosture(facts=facts, verdict=verdict, classes=classes,
                         reasons=tuple(reasons))


# ── Probes: the only part that touches the real host. Best-effort, conservative on failure ──

def _probe_platform() -> str:
    sysname = _platform.system().lower()          # 'linux' | 'darwin' | 'windows'
    arch = _platform.machine().lower()
    if sysname == "linux":
        try:
            with open("/proc/version", "r", encoding="utf-8", errors="ignore") as fh:
                if "microsoft" in fh.read().lower():
                    return f"windows-wsl2-{arch}"
        except OSError:
            pass
    return f"{sysname or 'unknown'}-{arch or 'unknown'}"


def _probe_container_runtime() -> str:
    for rt in ("docker", "podman"):
        if shutil.which(rt):
            return rt
    return ""


def _probe_tailscale() -> bool:
    if not shutil.which("tailscale"):
        return False
    try:
        out = subprocess.run(["tailscale", "status"], capture_output=True, text=True,
                             timeout=5)
        return out.returncode == 0 and "Logged out" not in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_gpu() -> str:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def _probe_secure_credential_store(platform_str: str) -> bool:
    # macOS Keychain and Windows Credential Manager are always present; on Linux we look for
    # a Secret Service provider (gnome-keyring / kwallet) on PATH. Conservative: unknown = False.
    if platform_str.startswith(("darwin", "windows")):
        return True
    return bool(shutil.which("gnome-keyring-daemon") or shutil.which("kwalletd6")
                or shutil.which("kwalletd5"))


def _probe_containment_supported() -> bool:
    # The rootless membrane uses `resource.setrlimit` + subprocess preexec — POSIX only.
    if os.name != "posix":
        return False
    try:
        import resource  # noqa: F401
        return True
    except ImportError:
        return False


def probe_device() -> DeviceFacts:
    """Inspect the real host. Each probe is best-effort; failures fall back to the
    conservative default, so a probe error narrows authority rather than widening it.

    Left at their conservative defaults (hard to detect portably without escalated calls,
    and safer under-reported): :attr:`DeviceFacts.disk_encryption` and
    :attr:`DeviceFacts.network_exposure`. A launcher with platform-specific rights
    (``fdesetup``/``manage-bde``, interface inspection) supplies these explicitly.
    """
    platform_str = _probe_platform()
    return DeviceFacts(
        platform=platform_str,
        container_runtime=_probe_container_runtime(),
        secure_credential_store=_probe_secure_credential_store(platform_str),
        gpu=_probe_gpu(),
        tailscale_connected=_probe_tailscale(),
        containment_supported=_probe_containment_supported(),
        # disk_encryption / network_exposure intentionally conservative (see docstring).
    )


def inspect() -> DevicePosture:
    """Probe this host and derive its posture — the raw inspection step."""
    return derive(probe_device())


# ── Reconstruction (for the ledger reader / replay) ──────────────────────────────────────

def _facts_from_canonical(d: Dict[str, object]) -> DeviceFacts:
    keys = ("platform", "container_runtime", "disk_encryption", "secure_credential_store",
            "network_exposure", "gpu", "tailscale_connected", "containment_supported")
    return DeviceFacts(**{k: d[k] for k in keys if k in d})  # type: ignore[arg-type]


def _posture_from_canonical(d: Dict[str, object]) -> DevicePosture:
    facts = _facts_from_canonical(d.get("facts", {}) or {})   # type: ignore[arg-type]
    classes = {ExecutionClass(k): Decision(v)
               for k, v in (d.get("classes", {}) or {}).items()}   # type: ignore[union-attr]
    return DevicePosture(
        facts=facts, verdict=Verdict(d["verdict"]), classes=classes,
        reasons=tuple(d.get("reasons", ()) or ()),                 # type: ignore[arg-type]
        contract_version=str(d.get("contract_version", CONTRACT_VERSION)))


# ── The mission-ledger event: posture is a first-class, replayable artifact ───────────────
# Posture is device-scoped, so it is appended under a stable device scope rather than a
# mission id. A restart/replay rebuilds it by fold, exactly like mission state.
DEVICE_SCOPE = "__device__"
POSTURE_EVENT = "DevicePostureEstablished"


def record_posture(store, posture: DevicePosture, *, scope: str = DEVICE_SCOPE):
    """Append the posture to the event store (duck-typed: needs ``.append``)."""
    payload = dict(posture.canonical_form())
    payload["posture_id"] = posture.posture_id
    return store.append(POSTURE_EVENT, scope, payload)


def latest_posture(store, *, scope: str = DEVICE_SCOPE) -> DevicePosture | None:
    """Fold the store to the most recently established posture for ``scope`` (or None)."""
    latest = None
    for e in store.for_mission(scope):
        if getattr(e, "type", None) == POSTURE_EVENT:
            latest = e
    return _posture_from_canonical(latest.payload) if latest is not None else None


# ── Posture → containment: the enforcement link the membrane consults ─────────────────────

class PostureDenied(RuntimeError):
    """An execution class this device is not allowed to run was requested. Named, never degraded."""


class PostureBlocked(RuntimeError):
    """The device posture is BLOCKED — the install must not proceed. Carries the reasons."""


def _clamp_int(ceiling: int, requested: int) -> int:
    # 0 on the ceiling ⇒ no ceiling on this dimension; 0 on the request ⇒ unspecified.
    if ceiling == 0:
        return requested
    if requested == 0:
        return ceiling
    return min(requested, ceiling)


def admit(posture: DevicePosture, cls: ExecutionClass,
          requested: ExecutionConstraint) -> ExecutionConstraint:
    """Clamp a requested constraint to what ``posture`` permits for ``cls``.

    Raises :class:`PostureDenied` if the device DENYs the class (no ceiling). Otherwise the
    result is the *intersection*: allow-lists are narrowed to the ceiling's, and every
    resource ceiling is the tighter of request and posture. A request can only ever be
    reduced by posture, never widened — deny-by-default all the way down.
    """
    ceiling = posture.ceiling(cls)
    if ceiling is None:
        raise PostureDenied(f"execution_class_denied:{cls.value}")
    return ExecutionConstraint(
        read_paths=tuple(sorted(set(requested.read_paths) & set(ceiling.read_paths))),
        write_paths=tuple(sorted(set(requested.write_paths) & set(ceiling.write_paths))),
        allow_egress=tuple(sorted(set(requested.allow_egress) & set(ceiling.allow_egress))),
        max_cpu_millis=_clamp_int(ceiling.max_cpu_millis, requested.max_cpu_millis),
        max_memory_mb=_clamp_int(ceiling.max_memory_mb, requested.max_memory_mb),
        max_duration_seconds=_clamp_int(ceiling.max_duration_seconds, requested.max_duration_seconds),
        max_processes=_clamp_int(ceiling.max_processes, requested.max_processes),
        max_concurrency=_clamp_int(ceiling.max_concurrency, requested.max_concurrency),
    )


# ── The installer bootstrap gate (Sidekick step 1: establish trust before integrations) ───

def require_verified(posture: DevicePosture) -> None:
    """Raise :class:`PostureBlocked` (with reasons) unless the posture is VERIFIED."""
    if posture.verdict is not Verdict.VERIFIED:
        raise PostureBlocked("; ".join(posture.reasons) or "device posture not verified")


def bootstrap(store=None, *, scope: str = DEVICE_SCOPE,
              prober: Callable[[], DeviceFacts] | None = None) -> DevicePosture:
    """Inspect the device, record the posture (audit the attempt), then gate.

    The installer's very first step: it establishes what the device may do *before* Sidekick
    recruits any integration. A BLOCKED device is still recorded (so the refusal is auditable)
    and then raises :class:`PostureBlocked`. ``prober`` is injectable for tests.
    """
    posture = derive(prober()) if prober is not None else inspect()
    if store is not None:
        record_posture(store, posture, scope=scope)
    require_verified(posture)
    return posture
