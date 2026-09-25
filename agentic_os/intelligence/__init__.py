"""Agentic Apps external/professional intelligence layer (moat plan WP2+).

Adapters implement the runtime_contracts intelligence provider contract; the Discovery bridge is the single seam
apps use to acquire external evidence for a decision (gate → acquire → value-account). The open stack ships the
open adapters (GLEIF/OpenSanctions/OpenCorporates); paid providers register on top as Bring-Your-Own.
"""
from .adapters.gleif import GleifProvider
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.opensanctions import OpenSanctionsProvider
from .discovery_bridge import acquire_for_decision, default_registry
from .value_store import EvidenceValueStore

__all__ = [
    "GleifProvider",
    "OpenSanctionsProvider",
    "OpenCorporatesProvider",
    "default_registry",
    "acquire_for_decision",
    "EvidenceValueStore",
]
