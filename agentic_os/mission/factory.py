"""Assemble a MissionRuntime from configuration — pick the planner and executor by env.

Keeps the wiring in one place so callers (control_plane, demo, tests) don't repeat it:

  * planner  — ModelPlanner(Qwen3-Coder-Next-NVFP4) if MISSION_PLANNER_BASE_URL is set, else the
    offline TemplatePlanner (so it always runs).
  * executor — an explicit `operator_client`, else an HTTPOperatorClient from MISSION_OPERATOR_BASES
    (a JSON map operator->base_url), else in-memory (caller supplies handlers) is a test concern.
"""
from __future__ import annotations

import json
import os

from .executor import Executor
from .models import OpenAICompatModel
from .operators import HTTPOperatorClient
from .planner import TemplatePlanner, ModelPlanner, Planner
from .registry import CapabilityRegistry
from .runtime import MissionRuntime
from .store import EventStore


def default_planner(transport=None) -> Planner:
    model = OpenAICompatModel.from_env(transport=transport)
    return ModelPlanner(model) if model is not None else TemplatePlanner()


def default_operator_client():
    bases = os.environ.get("MISSION_OPERATOR_BASES")
    if bases:
        return HTTPOperatorClient(json.loads(bases))
    return None


def build_runtime(registry: CapabilityRegistry, *, operator_client=None, store_path: str | None = None,
                  planner: Planner | None = None) -> MissionRuntime:
    client = operator_client or default_operator_client()
    if client is None:
        raise ValueError("no operator client: pass operator_client=, or set MISSION_OPERATOR_BASES")
    store = EventStore(path=store_path or os.environ.get("MISSION_EVENT_LOG"))
    return MissionRuntime(
        registry, Executor(client), store=store,
        planner=planner or default_planner(),
        secure_executor_for=_secure_executor_for(client, registry),
    )


def _secure_executor_for(client, registry):
    """When RDO_SECURITY_ENABLED and the enterprise plugins are importable, return a factory that builds a
    per-mission secured executor (credential broker + monitor + sandbox + delegated authority) from that
    mission's leased scope. Returns None otherwise — the open runtime is unchanged. Soft-imports the private
    package, so the public image (which cannot import it) silently keeps the open executor."""
    if (os.environ.get("RDO_SECURITY_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        return None
    try:
        from runtime_security_enterprise.wiring import build_secured_executor  # noqa: PLC0415
        from credentials_enterprise import broker_from_env  # noqa: PLC0415
        from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - public image has no enterprise package
        return None

    broker = broker_from_env(os.environ)                       # built once, shared across missions
    sandbox = _sandbox_from_env()

    def secure_executor_for(m):
        pol = getattr(m, "policy", None)
        scope = tuple(getattr(pol, "grants", ()) or ()) if pol is not None else tuple(getattr(m, "policy_refs", ()) or ())
        tenant = getattr(m, "tenant", "") or os.environ.get("RDO_RUNTIME_TENANT", "")
        authority = AuthorityContext(
            authority_id=(getattr(pol, "ref", "") or m.id), purpose=getattr(m, "goal", ""),
            principal=PrincipalRef(id=(tenant or "runtime"), tenant=tenant), scope=scope)
        executor, _monitor = build_secured_executor(
            client, descriptor_for=registry.get, mission_id=m.id, authority=authority,
            sandbox=sandbox, broker=broker)
        return executor

    return secure_executor_for


def _sandbox_from_env():
    """The SubprocessSandbox when RDO_SANDBOX_ENABLED and available; else None (isolation-declaring
    capabilities then fail closed, by design)."""
    if (os.environ.get("RDO_SANDBOX_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        return None
    try:
        from runtime_security_enterprise import SubprocessSandbox  # noqa: PLC0415
        return SubprocessSandbox()
    except Exception:  # noqa: BLE001
        return None
