"""The execution membrane — a concrete containment Sandbox behind the existing seam.

`executor.Sandbox` has always been a seam ("injected, never imported here"). Credential
blindness, exactly-once, authority binding, and the security monitor already exist around
it. What was missing is an actual *enforcing* implementation and the validation of a
signed :class:`~runtime_contracts.models.ExecutionEnvelope` at the boundary. This module
supplies both, provider-neutrally and without root:

* :class:`EnvelopeGate` — validate an envelope before anything runs. A tampered field
  changes the envelope's ``binding`` (the contract guarantees this), so a presented
  binding that does not match the authority-issued one is refused; an expired envelope is
  refused even if otherwise valid. Every refusal is a first-class :class:`ExecutionReceipt`.

* :class:`LocalContainmentSandbox` — run a capability in a child process with real,
  rootless containment derived from the envelope's :class:`ExecutionConstraint`:
  a **scrubbed environment** (the child inherits *no* ambient secrets), **resource
  ceilings** via ``setrlimit`` (address space, CPU seconds, process count), and a
  **contained working directory**. It implements the existing ``Sandbox`` protocol, so the
  ``Executor`` routes ``"sandbox"``-class capabilities through it unchanged.

Honest threat model (see the decision record): this defends against a *compromised or
misbehaving agent* escalating beyond its capability on a *trusted host*. Hard filesystem
and network isolation need OS namespaces — a `"strict"` backend (bubblewrap/nsjail) plugs
into the same seam and adds bind-mounted paths + `--unshare-net`; remote execution
additionally needs host attestation. This backend enforces what is enforceable rootless
and refuses to *pretend* to do the rest.
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from runtime_contracts.canonical import content_hash
from runtime_contracts.models import (
    EnvelopeInvalid,
    ExecutionConstraint,
    ExecutionEnvelope,
    ExecutionReceipt,
)
from .device_posture import DevicePosture, ExecutionClass, admit, PostureDenied

# ── Envelope validation ────────────────────────────────────────────────────────

def _epoch(not_after: str) -> Optional[float]:
    """Parse the envelope expiry (RFC3339 UTC or epoch-seconds string). None = no expiry."""
    if not not_after:
        return None
    try:
        return float(not_after)
    except ValueError:
        pass
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise EnvelopeInvalid(f"unparseable not_after: {not_after!r}")


@dataclass
class EnvelopeGate:
    """Validate an :class:`ExecutionEnvelope` against the binding an authority issued.

    ``now`` is injectable so expiry is testable without sleeping. A real deployment also
    verifies :attr:`ExecutionEnvelope.signature` over the binding; that hook is left to the
    signer plugin — the gate here proves the tamper-evidence the contract guarantees.
    """

    now: Callable[[], float] = time.time

    def validate(self, envelope: ExecutionEnvelope, *, expected_binding: str) -> None:
        """Raise :class:`EnvelopeInvalid` (named reason) unless the envelope is honourable."""
        if envelope.binding != expected_binding:
            raise EnvelopeInvalid("binding_mismatch")          # a field was altered
        exp = _epoch(envelope.not_after)
        if exp is not None and self.now() > exp:
            raise EnvelopeInvalid("envelope_expired")

    def refusal_receipt(self, envelope: ExecutionEnvelope, reason: str) -> ExecutionReceipt:
        return ExecutionReceipt(
            envelope_binding=envelope.binding, mission_id=envelope.mission_id,
            capability_id=envelope.capability_id, idempotency_key=envelope.idempotency_key,
            outcome="refused", reason=reason,
        )


# ── Contained execution ────────────────────────────────────────────────────────

class ContainmentError(RuntimeError):
    """The contained process violated its constraint (killed on a resource ceiling, or
    the capability raised). Surfaces as a named refusal, never a silent pass."""


# Child entrypoint. Reads {capability, inputs} on stdin, imports "module:attr", runs it,
# writes {"ok": ...} on stdout. It starts from a scrubbed environment and rlimits already
# applied by the parent (preexec), so it cannot see ambient secrets or exceed its ceiling.
_RUNNER = r"""
import json, sys, importlib
job = json.load(sys.stdin)
mod, _, attr = job["capability"].partition(":")
fn = getattr(importlib.import_module(mod), attr)
out = fn(job["inputs"]) or {}
sys.stdout.write(json.dumps({"result": out}))
"""


def _limits(constraint: ExecutionConstraint) -> Callable[[], None]:
    """Build a preexec_fn that applies the constraint's ceilings to the child process."""
    def apply() -> None:  # runs in the child, before exec
        if constraint.max_memory_mb:
            b = constraint.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
        if constraint.max_duration_seconds:
            s = constraint.max_duration_seconds
            resource.setrlimit(resource.RLIMIT_CPU, (s, s))
        if constraint.max_processes:
            n = constraint.max_processes
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (n, n))
            except (ValueError, OSError):
                pass  # NPROC is per-user on some platforms; best-effort
    return apply


@dataclass
class LocalContainmentSandbox:
    """A rootless containment backend implementing ``executor.Sandbox``.

    Capabilities are referenced as importable ``"module:attr"`` targets (a stateless
    compute step, per the stateless-agent contract). The sandbox runs one in a child with
    a scrubbed env, resource ceilings, and a contained cwd, then returns its result.
    """

    #: The physical ceilings/allow-lists for this run (from the envelope).
    constraint: ExecutionConstraint = ExecutionConstraint(
        max_memory_mb=256, max_duration_seconds=10, max_processes=1)
    #: Env vars explicitly allowed through. Default: none — the child inherits no ambient
    #: environment, so process secrets (AWS_SECRET_ACCESS_KEY, …) are invisible to it.
    env_passthrough: tuple = ()
    #: Optional device posture. When set, the effective constraint is *clamped* to what this
    #: device permits for :attr:`execution_class` before anything runs — and a class the
    #: device DENYs is refused outright. None ⇒ unchanged behaviour (host is trusted as-is).
    posture: "DevicePosture | None" = None
    execution_class: ExecutionClass = ExecutionClass.LOCAL_CONTAINER

    def run_contained(self, operator: str, capability: str, inputs: dict,
                      idempotency_key: str, *, constraint: ExecutionConstraint) -> dict:
        """The core rootless mechanism, driven by an explicit :class:`ExecutionConstraint`.

        Split out from :meth:`invoke` so a hardened wrapper (e.g. the Enterprise
        ``SubprocessSandbox``, which adds network namespaces / privilege-drop / secret
        redaction) can *compose* this as its runner — the canonical containment spec is the
        one ``ExecutionConstraint``, enforced here; the wrapper adds policy on top.

        When a :attr:`posture` is present, the requested constraint is narrowed to the
        device's ceiling for :attr:`execution_class` (deny-by-default: never widened), and a
        DENY'd class surfaces as a named :class:`ContainmentError` — the posture→containment
        link, so an install can never exceed the trust its device was granted.
        """
        if self.posture is not None:
            try:
                constraint = admit(self.posture, self.execution_class, constraint)
            except PostureDenied as e:
                raise ContainmentError(str(e))
        scrubbed = {k: os.environ[k] for k in self.env_passthrough if k in os.environ}
        scrubbed.setdefault("PATH", "/usr/bin:/bin")
        # Import paths are not secrets — preserve them so the capability resolves, while
        # ambient credential env vars stay scrubbed (that is the property under test).
        scrubbed["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        # Redeemed credential material for grants would be injected here — inside the
        # boundary only — never returned to or visible from the caller/agent.
        with tempfile.TemporaryDirectory(prefix="membrane-") as cwd:
            timeout = constraint.max_duration_seconds or None
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", _RUNNER],
                    input=json.dumps({"capability": capability, "inputs": inputs}),
                    text=True, capture_output=True, cwd=cwd, env=scrubbed,
                    preexec_fn=_limits(constraint), timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise ContainmentError("duration_exceeded")
        if proc.returncode != 0:
            # Non-zero: killed on a resource ceiling (OOM/CPU) or the capability raised.
            raise ContainmentError(
                f"contained_failure(rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return json.loads(proc.stdout)["result"]

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, isolation: str = "sandbox", grants: "list | None" = None) -> dict:
        """``executor.Sandbox`` entrypoint — runs against this instance's default constraint."""
        return self.run_contained(operator, capability, inputs, idempotency_key,
                                  constraint=self.constraint)

    def receipt(self, envelope: ExecutionEnvelope, result: dict,
                *, started: float, finished: float) -> ExecutionReceipt:
        """A success receipt bound to the envelope, with a digest of the real side effect."""
        return ExecutionReceipt(
            envelope_binding=envelope.binding, mission_id=envelope.mission_id,
            capability_id=envelope.capability_id, idempotency_key=envelope.idempotency_key,
            outcome="executed", side_effect_digest=content_hash(result),
            started_at=str(started), finished_at=str(finished),
        )
