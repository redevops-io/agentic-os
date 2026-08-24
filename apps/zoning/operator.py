"""Zoning intelligence as a Mission Runtime operator.

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) so the Mission Runtime can drive
zoning intelligence as a capability operator — parcel-first ("what can I build here?") and use-first
("find compatible parcels"), each a governed, replayable capability.

Capabilities (syscalls):
  zoning.resolve_parcel   — resolve a parcel to its canonical geometry identity (a GeoRef)
  zoning.acquire_evidence — acquire the official base zoning / ordinance / overlays for a parcel
  zoning.evaluate_use     — deterministic-first, fail-safe disposition of a use against a parcel
  zoning.search_parcels   — use-first: the parcels a use is not prohibited on

All four are read-only, deterministic, and side-effect-free — zoning intelligence *concludes*, it does not
mutate the world — so none carries an approval gate. The false-permitted = 0 SLO lives in the conclusion
itself (evaluate_use never returns PERMITTED without base compatibility), not in a human gate.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_zoning_operator() -> Operator:
    return Operator("zoning", [
        capability(
            "zoning.resolve_parcel",
            lambda inp: core.resolve_parcel(inp),
            provides=["parcel_resolved"],
            outputs={"parcel_resolved": "canonical GeoRef identity for the parcel"},
            side_effecting=False, deterministic=True,
            permissions=["zoning:read"], estimated_value="medium", latency_ms=40,
        ),
        capability(
            "zoning.acquire_evidence",
            lambda inp: core.acquire_evidence(inp),
            provides=["zoning_evidence"],
            outputs={"zoning_evidence": "official base zoning, ordinance link and overlays for the parcel"},
            side_effecting=False, deterministic=True,
            permissions=["zoning:read"], estimated_value="medium", latency_ms=120,
        ),
        capability(
            "zoning.evaluate_use",
            lambda inp: core.evaluate_use(inp),
            provides=["use_disposition"],
            outputs={"use_disposition": "PERMITTED | PROHIBITED | UNKNOWN — fail-safe, false-permit=0"},
            side_effecting=False, deterministic=True,
            permissions=["zoning:read"], estimated_value="high", latency_ms=15,
        ),
        capability(
            "zoning.search_parcels",
            lambda inp: core.search_parcels(inp),
            provides=["compatible_parcels"],
            outputs={"compatible_parcels": "parcels a use is not prohibited on, with dispositions"},
            side_effecting=False, deterministic=True,
            permissions=["zoning:read"], estimated_value="high", latency_ms=60,
        ),
    ])
