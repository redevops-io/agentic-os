"""S9 demo — the Social Intelligence Mission surface for Projects/Sidekick.

Builds the ``social_mission_view`` from the deterministic fixture corpus (there is no live social
provider — Reddit is CONTRACT_REQUIRED / POLICY_SCOPED and Meta/Muse is UNKNOWN), while the provider
boundary shown is the REAL capability-audit truth: a source is AVAILABLE only when its capability is
VERIFIED/POLICY_SCOPED. The observations are labelled as fixture so the demo never implies live scraping.
"""
from __future__ import annotations

from typing import Optional

from ..external.contracts import AgentCapabilities, CapabilityStatus
from ..external.projections import social_mission_view
from .discovery import market_signals, to_opportunity
from .fake_provider import replay_corpus

# The provider boundary as the capability audit declares it (external_agent_capabilities.yaml).
_PROVIDER_CAPS = {
    "reddit": AgentCapabilities(provider="reddit",
                               statuses={"social.search_public": CapabilityStatus.POLICY_SCOPED}),
    "meta-muse": AgentCapabilities(provider="meta-muse",
                                  statuses={"social.search_public": CapabilityStatus.UNKNOWN}),
}
_SOURCES = [("Reddit", "reddit", "social.search_public"),
            ("Meta/Muse", "meta-muse", "social.search_public")]

_PRODUCT_TERMS = ["context", "memory", "conversation", "retrieval", "rag", "vector"]


def demo_social_mission(*, now_ms: Optional[int] = None) -> dict:
    """The Social Intelligence card projection. Deterministic when ``now_ms`` is pinned."""
    corpus = replay_corpus(now_ms=now_ms) if now_ms is not None else replay_corpus()
    opps = [o for o in (to_opportunity(o, product_terms=_PRODUCT_TERMS, now_ms=now_ms) for o in corpus)
            if o is not None]
    signals = market_signals(corpus, window_seconds=7 * 86_400, now_ms=now_ms)
    view = social_mission_view(observations=len(corpus), opportunities=opps,
                               market_signal_count=len(signals), provider_capabilities=_PROVIDER_CAPS,
                               sources=_SOURCES)
    view["data_source"] = "fixture corpus (no live social provider is enabled)"
    return view
