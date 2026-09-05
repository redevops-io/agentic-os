"""NVIDIA PAIR (Personal AI Router) detection — the local inference fabric as a provider.

PAIR discovers home/LAN machines running Ollama / LM Studio and exposes ONE **loopback**
OpenAI- and Ollama-compatible endpoint on the machine running the app, routing each request to
an eligible node over mTLS. ReDevOps treats it as a *physical inference operator* beneath a
stable model requirement: **PAIR decides which machine serves; ReDevOps decides whether it
should, what model class the Mission needs, the cost/authority, and the fallback.**

Loosely coupled on purpose — PAIR is Apache-2.0 and young (v0.1.x), so we do **not** fork or
vendor its Go services. We speak only to its documented **loopback OpenAI-compatible endpoint**
(reusing the ordinary OpenAI-compatible client path) and read its model inventory from
``/v1/models``. Detection is best-effort and never raises.

Endpoints (from PAIR docs): OpenAI proxy ``127.0.0.1:1234/v1`` and Ollama proxy
``127.0.0.1:11434`` are loopback-only (a non-loopback request is refused 403) — which is why
PAIR sits *behind* the Runtime and Tailscale, never exposed directly to a phone or peer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from .util import Fetch, get_json

# Loopback-only proxies PAIR exposes on the machine running the application.
PAIR_OPENAI_BASE = "http://127.0.0.1:1234/v1"
PAIR_OLLAMA_BASE = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class PairStatus:
    """What a probe found. `available` False ⇒ PAIR not running here (fall through the resolver)."""

    available: bool
    base_url: str = ""
    models: Tuple[str, ...] = ()

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


def detect_pair(*, base_url: str = PAIR_OPENAI_BASE, fetch: Optional[Fetch] = None,
                timeout: float = 1.5) -> PairStatus:
    """Probe PAIR's loopback OpenAI endpoint and read its model inventory (``GET {base}/models``).

    ``fetch`` is injectable for tests; the default does a short-timeout localhost GET, so when PAIR
    is absent the connection is refused immediately and this returns ``available=False`` cheaply.
    """
    try:
        doc = (fetch or get_json)(f"{base_url.rstrip('/')}/models", timeout)
    except Exception:      # not running / unreachable / bad response — simply "not available"
        return PairStatus(available=False)
    data = doc.get("data") if isinstance(doc, dict) else None
    models = tuple(str(m.get("id")) for m in (data or []) if isinstance(m, dict) and m.get("id"))
    return PairStatus(available=True, base_url=base_url, models=models)
