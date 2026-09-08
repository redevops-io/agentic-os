"""Integration Plane — the Connect Compiler's deterministic core (W0).

The wizard's front end is forgiving: an LLM reads a plain-English request into an
editable proposal, and a human confirms it. This package is the strict back end that
confirmation compiles into. Two contracts sit on either side of that boundary — an
editable :class:`IntegrationProposal` and, once a person confirms it, a sealed,
content-addressed :class:`ConfirmedIntegrationIntent` — plus an
:class:`IntegrationManifest` that says, per provider x capability, what can actually
run.

The manifest is a *reality filter* on the proposal (only ever suggest what is
buildable) and the *post-confirmation safety net* (refuse by name, never nearest-
runnable). It is never a gate on the user's language, and its vocabulary never reaches
the small-business UI. Additive: nothing here reads a model or rewires a Mission — it
is the shape the wizard's later slices compile into.
"""
from __future__ import annotations

from .manifest import CapabilityDimension, IntegrationManifest, Support
from .contracts import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    CapabilityRequirementGraph,
    ConfirmedIntegrationIntent,
    IntegrationProposal,
)

__all__ = [
    "Support",
    "CapabilityDimension",
    "IntegrationManifest",
    "CapabilityRequirement",
    "CapabilityRequirementGraph",
    "IntegrationProposal",
    "ConfirmedIntegrationIntent",
    "CONTRACT_VERSION",
]
