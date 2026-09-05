"""PAIR runtime governance — record the physical node, and fall back PAIR→cloud by policy (#4, #5).

Two runtime concerns once PAIR is the resolved provider:

* **#4 Telemetry of the physical node.** PAIR routes each request to one machine; when it reports
  which (via a response header), record it durably so the ledger shows *where* an inference ran —
  the physical-independence thesis made auditable. `record_inference` appends an event;
  `pair_node_from_headers` reads the node id if PAIR exposes one (else "unknown").

* **#5 Fallback by Mission policy.** A resolved local/PAIR provider can be unreachable at call time
  (node asleep, engine down). `build_llm_chain` turns the resolver's choice into an ordered try
  list — PAIR/local first, then a configured cloud provider **only if the Mission policy permits
  it**. A `local_only` Mission (privacy=local) gets NO cloud fallback: it fails closed rather than
  silently sending private prompts to a third party. `attempt` runs the chain and records failures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .default_llm import LLMChoice, REGISTRY, resolve_llm

INFERENCE_SERVED = "InferenceServed"

# response headers PAIR might use to name the serving node (checked case-insensitively)
_NODE_HEADERS = ("x-pair-node", "x-pair-node-id", "x-served-by", "x-pair-served-by")


# ── #4 physical-node telemetry ────────────────────────────────────────────────────────────

def pair_node_from_headers(headers: Optional[Dict[str, str]]) -> str:
    """Extract the serving node id from PAIR's response headers, or '' if not reported."""
    if not headers:
        return ""
    lower = {str(k).lower(): v for k, v in headers.items()}
    for h in _NODE_HEADERS:
        if lower.get(h):
            return str(lower[h])
    return ""


def record_inference(store, *, mission_id: str, provider: str, model: str,
                     node: str = "", base_url: str = "") -> None:
    """Durably record which provider/model served, and (for PAIR) the physical node chosen."""
    if store is None:
        return
    store.append(INFERENCE_SERVED, mission_id or "__telemetry__", {
        "provider": provider, "model": model,
        "node": node or "unknown", "base_url": base_url})


# ── #5 fallback by Mission policy ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FallbackPolicy:
    allow_cloud_fallback: bool = True   # may we fall back to a cloud provider at all?
    local_only: bool = False            # Mission requires local (privacy) → NEVER cloud
    cloud_provider: str = "groq"        # preferred cloud fallback (registry id)


def build_llm_chain(env: Optional[Dict[str, str]] = None, *,
                    policy: FallbackPolicy = FallbackPolicy(),
                    resolve: Callable[..., LLMChoice] = resolve_llm) -> Tuple[LLMChoice, ...]:
    """Ordered providers to try: the resolved primary, then a cloud fallback if policy permits.

    A local_only Mission gets NO cloud fallback (fail closed, don't leak private prompts). A cloud
    fallback is added only after a local/PAIR primary — a primary that is already cloud/guided needs none.
    """
    primary = resolve(env)
    chain: List[LLMChoice] = [primary]
    if policy.local_only or not policy.allow_cloud_fallback:
        return tuple(chain)
    if primary.source in ("pair", "local"):
        prov = next((p for p in REGISTRY if p.id == policy.cloud_provider), None)
        if prov is not None:
            chain.append(LLMChoice(
                source="guided", provider=prov.id, base_url=prov.base_url, model=prov.model,
                api_key_env=prov.api_key_env, needs_setup=True, signup_url=prov.signup_url,
                trains_on_data=prov.trains_on_data,
                message=f"cloud fallback: {prov.name} (only if local AI is unreachable)"))
    return tuple(chain)


class AllProvidersFailed(RuntimeError):
    """Every provider in the chain failed (or the chain was empty)."""


def attempt(chain: Tuple[LLMChoice, ...], call_fn: Callable[[LLMChoice], object], *,
            store=None, mission_id: str = "") -> Tuple[object, LLMChoice]:
    """Try each provider in order; return (result, provider_used) on the first success. Records
    each failure to the ledger. Raises AllProvidersFailed if none succeed."""
    errors: List[str] = []
    for choice in chain:
        try:
            result = call_fn(choice)
        except Exception as e:   # noqa: BLE001 — try the next provider
            errors.append(f"{choice.provider}:{type(e).__name__}")
            if store is not None:
                store.append("InferenceProviderFailed", mission_id or "__telemetry__",
                             {"provider": choice.provider, "error": type(e).__name__})
            continue
        return result, choice
    raise AllProvidersFailed("; ".join(errors) or "no providers in chain")
