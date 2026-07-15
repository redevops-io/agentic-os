"""Production runtime wiring — drive the LIVE agentic-app operators over HTTP.

`build_production_runtime(operators)` turns a map of `operator_name -> base_url` (in deployment,
parsed from `modules.yaml`) into a runnable `MissionRuntime`:

  1. **discover** — `GET {base}/capabilities` from each operator and register its manifest, so the
     planner reasons over the real fleet's capabilities (no hand-maintained mirror);
  2. **drive** — `HTTPOperatorClient` invokes `POST {base}/invoke` over the same capability contract.

`fetch` (GET) and `transport` (POST) are injectable, so the wiring is provable end-to-end without a
single live service — a fake operator app per name, driven exactly as production would drive it.
"""
from __future__ import annotations

from typing import Callable

from .executor import Executor
from .factory import default_planner
from .operators import HTTPOperatorClient
from .planner import Planner
from .registry import CapabilityRegistry
from .runtime import MissionRuntime
from .store import EventStore
from .types import CapabilityManifest, CapabilitySpec, NodeCost
from .util import Fetch, Transport, get_json

DiscoverFetch = Callable[[str], dict]  # GET a URL -> parsed JSON


def _spec_from_json(d: dict) -> CapabilitySpec:
    c = d.get("cost") or {}
    return CapabilitySpec(
        name=d["name"], operator=d.get("operator", ""),
        inputs=d.get("inputs") or {}, outputs=d.get("outputs") or {},
        cost=NodeCost(usd=c.get("usd", 0.0), latency_ms=c.get("latency_ms", 0),
                      tokens=c.get("tokens", 0), human_minutes=c.get("human_minutes", 0.0)),
        permissions=d.get("permissions") or [], deterministic=d.get("deterministic", True),
        side_effecting=d.get("side_effecting", False), undo=d.get("undo"),
        approval_required=d.get("approval_required", False), confidence=d.get("confidence", 0.9),
        estimated_value=d.get("estimated_value", "medium"), provides=d.get("provides") or [],
        embedding=d.get("embedding") or [],
    )


def _manifest_from_json(d: dict) -> CapabilityManifest:
    return CapabilityManifest(operator=d["operator"],
                              capabilities=[_spec_from_json(s) for s in d.get("capabilities", [])])


def discover(operators: dict[str, str], *, fetch: DiscoverFetch | None = None,
             timeout: float = 10.0) -> tuple[CapabilityRegistry, dict[str, str]]:
    """GET /capabilities from each operator; return (registry, resolve-map keyed by the operator
    name each manifest DECLARES) — so node.operator always resolves to the right base URL even if
    the caller's dict key differs from the declared operator name."""
    _fetch: DiscoverFetch = fetch or (lambda url: get_json(url, timeout))
    reg = CapabilityRegistry()
    resolve: dict[str, str] = {}
    for base in operators.values():
        base = base.rstrip("/")
        manifest = _manifest_from_json(_fetch(f"{base}/capabilities"))
        reg.register(manifest)
        resolve[manifest.operator] = base
    return reg, resolve


def operators_from_modules(modules: list[dict], *, host: str = "localhost", scheme: str = "http",
                           url_for: Callable[[dict], str] | None = None) -> dict[str, str]:
    """Build the operator base-URL map from `modules.yaml` entries (each `{name, port, ...}`).

    Single-host default (`scheme://host:port`); pass `url_for` for a per-app host map or a
    service-name scheme (compose / multi-host). Keys are the module names; `discover()` re-keys by
    the operator name each manifest declares, so a name mismatch here is harmless."""
    out: dict[str, str] = {}
    for m in modules:
        name = m.get("name")
        if not name:
            continue
        if url_for is not None:
            out[name] = url_for(m)
        elif m.get("port"):
            out[name] = f"{scheme}://{host}:{m['port']}"
    return out


def build_production_runtime(operators: dict[str, str], *, fetch: DiscoverFetch | None = None,
                             transport: Transport | None = None, planner: Planner | None = None,
                             store_path: str | None = None) -> MissionRuntime:
    """Assemble a MissionRuntime that plans over — and executes against — the live operator fleet."""
    registry, resolve = discover(operators, fetch=fetch)
    client = HTTPOperatorClient(resolve=resolve, transport=transport)
    return MissionRuntime(registry, Executor(client), store=EventStore(path=store_path),
                          planner=planner or default_planner())
