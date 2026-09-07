"""Vehicle identity from a VIN via NHTSA vPIC — free, keyless, public API.

``decode_vin`` maps vPIC's ``DecodeVinValues`` output onto a :class:`VehicleRef`, inferring powertrain
across ICE / hybrid / PHEV / EV so the product works on any vehicle (not EV-only). The HTTP fetch is
injectable, so this is unit-tested offline with no network; a live smoke hits the real vPIC endpoint.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, Optional

from .contracts import Powertrain, VehicleRef

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"

# A fetch takes a URL and returns the decoded JSON dict.
Fetch = Callable[[str], Dict[str, Any]]


def _urllib_fetch(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _infer_powertrain(row: Dict[str, Any]) -> Powertrain:
    fuel = (row.get("FuelTypePrimary") or "").lower()
    fuel2 = (row.get("FuelTypeSecondary") or "").lower()
    elec = (row.get("ElectrificationLevel") or "").lower()
    if "fuel cell" in fuel or "bev" in elec or (("electric" in fuel) and not fuel2):
        return Powertrain.EV
    if "phev" in elec or "plug-in" in elec:
        return Powertrain.PHEV
    if "hev" in elec or "hybrid" in elec or ("electric" in fuel2) or ("electric" in fuel and fuel2):
        return Powertrain.HYBRID
    if fuel:  # gasoline / diesel / etc.
        return Powertrain.ICE
    return Powertrain.UNKNOWN


def decode_vin(vin: str, *, fetch: Optional[Fetch] = None) -> VehicleRef:
    """Decode a 17-char VIN into a VehicleRef. Falls back to a bare VehicleRef(vin) on any error, so a
    diagnosis can still proceed with the driver's description when identity is unavailable."""
    vin = (vin or "").strip().upper()
    fetch = fetch or _urllib_fetch
    try:
        data = fetch(VPIC_URL.format(vin=vin))
        row = (data.get("Results") or [{}])[0]
    except Exception:  # noqa: BLE001 — identity is best-effort; never block a diagnosis on it
        return VehicleRef(vin=vin)
    return VehicleRef(
        vin=vin,
        make=(row.get("Make") or "").title(),
        model=row.get("Model") or "",
        year=row.get("ModelYear") or "",
        powertrain=_infer_powertrain(row),
        body_class=row.get("BodyClass") or "",
        fuel_primary=row.get("FuelTypePrimary") or "",
        engine_cylinders=row.get("EngineCylinders") or "",
        displacement_l=row.get("DisplacementL") or "",
    )
