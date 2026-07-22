"""Federate external operator *services* into Sidekick's Mission Runtime.

Sidekick's default runtime co-locates a couple of operators in-process (LocalOperatorClient). But
the operators a real DevOps deployment needs — supply-chain scanning, incident response, compliance,
privacy — usually live in their own repos and run as their own `/invoke` services (see the operator
SDK's `router()`). This module lets Sidekick treat those as first-class capabilities *without*
importing their code: it reads a `modules.yaml` (operator name → base URL), discovers each service's
manifest over `GET /capabilities`, and resolves invocations over `POST /invoke` via HTTPOperatorClient.

    modules.yaml
    ------------
    operators:
      infra:              http://infra:8230
      edge-sentinel:      http://edge-sentinel:8241
      operate:            http://operate:8242
      agentic-compliance: http://agentic-compliance:8243
      agentic-privacy:    http://agentic-privacy:8244

Point Sidekick at it with SIDEKICK_MODULES=/path/to/modules.yaml. Absent that env var the server
keeps its built-in local operators, so nothing changes for the standalone bundle.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Any

from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, NodeCost
from agentic_os.mission.util import get_json


def load_modules(path: str) -> dict[str, str]:
    """Parse modules.yaml → {operator_name: base_url}. Tolerates JSON too (a YAML superset case)."""
    text = pathlib.Path(path).read_text()
    data: Any
    try:
        import yaml  # pyyaml is in the image; fall back to json for the JSON subset
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        import json
        data = json.loads(text)
    ops = (data or {}).get("operators", data) or {}
    return {str(k): str(v).rstrip("/") for k, v in ops.items() if v}


def _spec_from_dict(d: dict, operator: str) -> CapabilitySpec:
    """Rebuild a CapabilitySpec from a service's `/capabilities` JSON, ignoring unknown keys."""
    fields = {f.name for f in dataclasses.fields(CapabilitySpec)}
    kw = {k: v for k, v in d.items() if k in fields and k not in ("cost", "embedding")}
    kw.setdefault("name", d.get("name", ""))
    kw["operator"] = d.get("operator") or operator
    cost = d.get("cost") or {}
    if isinstance(cost, dict):
        cost_fields = {f.name for f in dataclasses.fields(NodeCost)}
        kw["cost"] = NodeCost(**{k: v for k, v in cost.items() if k in cost_fields})
    # a remote capability is never trusted-builtin: tag its provenance so risk-scoring treats it as such
    kw["source"] = d.get("source") or f"http:{operator}"
    return CapabilitySpec(**kw)


def discover_manifest(operator: str, base_url: str, timeout: float = 8.0,
                      fetch=None) -> CapabilityManifest:
    """GET {base_url}/capabilities and reconstruct the operator's CapabilityManifest."""
    doc = get_json(f"{base_url.rstrip('/')}/capabilities", timeout=timeout, fetch=fetch)
    caps = [_spec_from_dict(c, operator) for c in (doc.get("capabilities") or [])]
    return CapabilityManifest(operator=doc.get("operator") or operator, capabilities=caps)


def federate(registry, modules: dict[str, str], timeout: float = 8.0, fetch=None) -> dict[str, str]:
    """Discover + register every configured operator's manifest into `registry`.

    Returns the operator→URL map (for HTTPOperatorClient). Unreachable services are skipped with a
    warning rather than failing startup — the reachable operators still plan/execute.
    """
    resolved: dict[str, str] = {}
    for name, url in modules.items():
        try:
            manifest = discover_manifest(name, url, timeout=timeout, fetch=fetch)
            registry.register(manifest)
            resolved[name] = url
        except Exception as e:  # noqa: BLE001 — a down operator shouldn't take Sidekick down
            print(f"[federation] skip operator '{name}' at {url}: {e}")
    return resolved


def modules_path_from_env() -> str | None:
    p = os.environ.get("SIDEKICK_MODULES")
    return p if p and pathlib.Path(p).exists() else None
