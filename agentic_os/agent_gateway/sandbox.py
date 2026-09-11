"""Phase 6 — the action sandbox (plan §8).

Per the plan, the gateway does NOT sandbox the external agent; sandboxing is a **separate internal
capability** the Mission Runtime can invoke:

    external agent → gateway → Mission Runtime → sandbox.execute → isolated runtime

This module is the contract (`SandboxSpec` / `SandboxAction` / `SandboxObservation` + the
`SandboxRuntime` seam + a complete event log) plus two reference runtimes:

- :class:`NullSandbox` — the safe default: executes nothing, fails closed. So `sandbox.execute`
  with no runtime wired returns a refusal, never silent success.
- :class:`SubprocessSandbox` — a reference giving **process / env / filesystem / wall-time**
  isolation: no ambient environment (only explicitly-injected scoped vars), a fresh temp working
  dir, a hard wall-time kill, output capture, and a **command allowlist** (deny-by-default — it runs
  only argv[0]s the deployer allowlisted). It does NOT provide network deny-all or hard CPU/memory
  quotas — those require a container/microVM backend behind the same `SandboxRuntime` seam
  (`network_isolated` says so honestly).

`sandbox.execute` is exposed as a CRITICAL, mandatory-approval capability, so it is reachable only
under the strictest gate.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Protocol, Tuple

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
from .registry import CapabilityRegistry, HandlerResult


@dataclass(frozen=True)
class SandboxSpec:
    argv: Tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)   # ONLY these — no ambient credentials
    wall_time_s: float = 30.0
    max_output: int = 20_000                                # truncate captured stdout/stderr
    egress_allowlist: Tuple[str, ...] = ()                  # default = deny-all (container backend enforces)


@dataclass(frozen=True)
class SandboxAction:
    input_files: Mapping[str, str] = field(default_factory=dict)   # name → content, written into cwd
    collect: Tuple[str, ...] = ()                                  # output filenames to export
    stdin: str = ""


@dataclass(frozen=True)
class SandboxObservation:
    ok: bool
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    artifacts: Mapping[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    truncated: bool = False
    network_isolated: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr, "artifacts": dict(self.artifacts),
                "duration_s": round(self.duration_s, 3), "truncated": self.truncated,
                "network_isolated": self.network_isolated, "error": self.error}


class SandboxRuntime(Protocol):
    def execute(self, spec: SandboxSpec, action: SandboxAction) -> SandboxObservation: ...


@dataclass
class NullSandbox:
    """Safe default — never executes. `sandbox.execute` without a real runtime fails closed."""
    events: List[dict] = field(default_factory=list)

    def execute(self, spec: SandboxSpec, action: SandboxAction) -> SandboxObservation:
        self.events.append({"event": "refused", "reason": "no sandbox runtime configured",
                            "argv": list(spec.argv), "ts": time.time()})
        return SandboxObservation(ok=False, error="no sandbox runtime configured")


@dataclass
class SubprocessSandbox:
    """Reference runtime: process/env/fs/wall-time isolation with a command allowlist. NOT network-
    or quota-isolated — use a container/microVM backend in production. Deny-by-default: with an empty
    allowlist it runs nothing."""

    allow_commands: FrozenSet[str] = frozenset()
    events: List[dict] = field(default_factory=list)

    def execute(self, spec: SandboxSpec, action: SandboxAction) -> SandboxObservation:
        start = time.time()
        if not spec.argv:
            return self._log(spec, start, SandboxObservation(ok=False, error="empty argv"))
        if spec.argv[0] not in self.allow_commands:
            return self._log(spec, start,
                             SandboxObservation(ok=False, error=f"command not allowlisted: {spec.argv[0]}"))
        with tempfile.TemporaryDirectory(prefix="rdo-sbx-") as tmp:
            cwd = Path(tmp)
            for name, content in action.input_files.items():
                # keep writes inside the sandbox dir (no path escape)
                target = (cwd / name).resolve()
                if not str(target).startswith(str(cwd.resolve())):
                    return self._log(spec, start, SandboxObservation(ok=False, error="path escape blocked"))
                target.write_text(content)
            try:
                proc = subprocess.run(
                    list(spec.argv), cwd=tmp, env=dict(spec.env),   # NO ambient env
                    input=action.stdin, capture_output=True, text=True,
                    timeout=spec.wall_time_s)
            except subprocess.TimeoutExpired:
                return self._log(spec, start,
                                 SandboxObservation(ok=False, error="wall_time exceeded",
                                                    duration_s=time.time() - start))
            except Exception as exc:  # spawn failure etc.
                return self._log(spec, start, SandboxObservation(ok=False, error=str(exc)))
            out, trunc1 = _cap(proc.stdout, spec.max_output)
            err, trunc2 = _cap(proc.stderr, spec.max_output)
            artifacts = {}
            for name in action.collect:
                f = (cwd / name)
                if f.is_file():
                    artifacts[name] = f.read_text()[: spec.max_output]
            obs = SandboxObservation(
                ok=(proc.returncode == 0), exit_code=proc.returncode, stdout=out, stderr=err,
                artifacts=artifacts, duration_s=time.time() - start, truncated=(trunc1 or trunc2),
                network_isolated=False)
            return self._log(spec, start, obs)

    def _log(self, spec: SandboxSpec, start: float, obs: SandboxObservation) -> SandboxObservation:
        self.events.append({"event": "executed", "argv": list(spec.argv), "ok": obs.ok,
                            "exit_code": obs.exit_code, "duration_s": round(time.time() - start, 3),
                            "ts": time.time()})
        return obs


def _cap(s: str, limit: int) -> Tuple[str, bool]:
    s = s or ""
    return (s[:limit], True) if len(s) > limit else (s, False)


# ── the governed capability (CRITICAL + mandatory approval) ────────────────────────
SANDBOX_EXECUTE = CapabilityManifest(
    "sandbox.execute",
    "Run a command in an isolated sandbox (no ambient credentials, wall-time limited).",
    CapabilityKind.DIRECT, permissions=("sandbox.execute",), risk_tier=RiskTier.CRITICAL,
    side_effecting=True, approval_policy=ApprovalPolicy.MANDATORY, provider="sandbox",
    input_schema={"type": "object",
                  "properties": {"argv": {"type": "array", "items": {"type": "string"}},
                                 "env": {"type": "object"}, "stdin": {"type": "string"}},
                  "required": ["argv"]},
    data_classes=(DataClass.INTERNAL,))


def register_sandbox_capability(registry: CapabilityRegistry, runtime: SandboxRuntime,
                                *, wall_time_s: float = 30.0) -> CapabilityRegistry:
    """Register `sandbox.execute` backed by a SandboxRuntime. Reachable only under CRITICAL +
    mandatory approval; the runtime's own allowlist is the second line of defence."""
    def _handler(req, env):
        args = req.arguments
        spec = SandboxSpec(argv=tuple(args.get("argv") or ()),
                           env={str(k): str(v) for k, v in (args.get("env") or {}).items()},
                           wall_time_s=float(args.get("wall_time_s", wall_time_s)))
        action = SandboxAction(input_files=dict(args.get("input_files") or {}),
                               collect=tuple(args.get("collect") or ()), stdin=str(args.get("stdin", "")))
        obs = runtime.execute(spec, action)
        return HandlerResult(ok=obs.ok, output=obs.as_dict(), data_classes=(DataClass.INTERNAL,),
                             error=("" if obs.ok else obs.error))
    return registry.register(SANDBOX_EXECUTE, _handler)
