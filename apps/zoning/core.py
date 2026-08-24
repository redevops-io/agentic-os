"""Zoning-intelligence core — the capability logic behind the Mission Runtime operator.

Deterministic-first and fail-safe: a use is concluded PERMITTED only where the official base zoning makes
it permitted *by right*; anything base-incompatible is PROHIBITED with certainty; and wherever the answer
depends on the ordinance's permitted-use table or an overlay we have not parsed, the runtime abstains to
UNKNOWN and cites the source — never a false "permitted". That abstention is the **false-permitted = 0**
blocking SLO.

Self-contained in agentic-os (the Mission Runtime does not import Context Runtime): it uses only the
canonical `GeoRef` spatial-evidence primitive promoted to `runtime-contracts`. The adaptive, cost-aware
evidence *optimization* is Context Runtime's job; this operator is the governed capability boundary.
"""
from __future__ import annotations

from runtime_contracts import GeoRef, geometry_hash

# Normalized target uses (a starter set; jurisdictions map their local rules onto these).
USE_FAMILY = {
    "RESIDENTIAL_SINGLE_FAMILY": "residential", "RESIDENTIAL_MULTI_FAMILY": "residential",
    "OFFICE": "commercial", "RETAIL": "commercial", "RESTAURANT": "commercial", "HOTEL": "commercial",
    "WAREHOUSE": "industrial", "LIGHT_INDUSTRIAL": "industrial", "HEAVY_INDUSTRIAL": "industrial",
}
# Uses whose permitted/conditional status needs the ordinance's use table before PERMITTED can be confirmed.
NEEDS_ORDINANCE = {"OFFICE", "RETAIL", "RESTAURANT", "HOTEL", "WAREHOUSE",
                   "LIGHT_INDUSTRIAL", "HEAVY_INDUSTRIAL"}


def code_family(code: str) -> str:
    """Coarse, honest family classifier from a real zoning-code prefix (per-jurisdiction taxonomies vary —
    this is deliberately not a claim of full ordinance parsing)."""
    c = (code or "").strip().upper()
    if c.startswith(("R-", "RH", "RM", "RS", "RR", "RE")) or (c.startswith("R") and not c.startswith("RET")):
        return "residential"
    if c.startswith(("C-", "CB", "CG", "CN", "CS", "TC", "MU", "MX")) or c.startswith("C"):
        return "commercial"
    if c.startswith(("M-", "I-", "IL", "IG", "IND", "PI")) or c.startswith(("M", "I")):
        return "industrial"
    return "unknown"


def resolve_parcel(body: dict) -> dict:
    """Resolve a parcel to its canonical geometry identity (a GeoRef). Accepts either raw geometry
    (`rings` + `crs`) or a pre-harvested record carrying `parcel_id`/`bbox`/`centroid`. Read-only."""
    crs = body.get("crs", "EPSG:4326")
    rings = body.get("rings")
    if rings:
        gid = geometry_hash(rings, crs)
        xs = [x for ring in rings for x, _ in ring]
        ys = [y for ring in rings for _, y in ring]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        centroid = (sum(xs) / len(xs), sum(ys) / len(ys))
    else:
        gid = body.get("parcel_id") or body.get("geometry_hash", "")
        bbox = tuple(body.get("bbox", (0, 0, 0, 0)))
        centroid = tuple(body.get("centroid", (0, 0)))
    ref = GeoRef(geometry_type=body.get("geometry_type", "Polygon"), geometry_hash=gid, crs=crs,
                 bbox=bbox, centroid=centroid, jurisdiction=body.get("jurisdiction", ""),
                 source=body.get("source_url", body.get("source", "")))
    return {"parcel_id": gid, "geometry_ref": ref.canonical_form(), "envelope_ref": ref.ref,
            "jurisdiction": ref.jurisdiction}


def acquire_evidence(body: dict) -> dict:
    """Return the zoning evidence available for a parcel — the official base zoning code, ordinance link,
    and overlays. In production this is where the cost-aware evidence bundle (zoning-sources-mcp harvest +
    Context Runtime optimization) is acquired; here it reads what the caller already resolved. Read-only."""
    return {
        "parcel_id": body.get("parcel_id", ""),
        "base_zoning": (body.get("zoning_code") or "").strip(),
        "code_family": code_family(body.get("zoning_code", "")),
        "ordinance_url": body.get("ordinance_url", ""),
        "overlays": list(body.get("overlays", [])),
        "evidence_sources": ["official_gis"] + (["ordinance"] if body.get("ordinance_url") else []),
    }


def evaluate_use(body: dict) -> dict:
    """Deterministic-first, fail-safe disposition for a use against a parcel's base zoning. Never returns a
    confident PERMITTED without base compatibility — the false-permitted = 0 SLO."""
    use = body.get("use", "")
    code = body.get("base_zoning") or body.get("zoning_code", "")
    fam = code_family(code)
    if use not in USE_FAMILY:
        disp, why = "UNKNOWN", f"use {use!r} is not in the normalized ontology"
    elif fam == "unknown":
        disp, why = "UNKNOWN", "zoning-code family unrecognized — cannot conclude from base alone"
    elif USE_FAMILY[use] != fam:
        disp, why = "PROHIBITED", f"official base zoning {code} ({fam}) excludes a {USE_FAMILY[use]} use"
    elif use in NEEDS_ORDINANCE:
        disp, why = "UNKNOWN", "base-compatible, but permitted/conditional status needs the ordinance — verify"
    else:
        disp, why = "PERMITTED", f"official base zoning {code} permits {use.lower().replace('_', ' ')} by right"
    # Fail-safe invariant, asserted at the boundary: PERMITTED implies base compatibility.
    false_permit = disp == "PERMITTED" and (use not in USE_FAMILY or USE_FAMILY.get(use) != fam)
    return {"parcel_id": body.get("parcel_id", ""), "use": use, "base_zoning": code,
            "disposition": disp, "reason": why, "ordinance_url": body.get("ordinance_url", ""),
            "false_permit": false_permit}


def search_parcels(body: dict) -> dict:
    """Use-first: over a set of parcels, return those a use is NOT prohibited on (compatible or needs-review),
    each with its disposition. Deterministic — the same fail-safe rule, never a false include."""
    use = body.get("use", "")
    parcels = body.get("parcels", [])
    matches = []
    for p in parcels:
        verdict = evaluate_use({**p, "use": use})
        if verdict["disposition"] != "PROHIBITED":
            matches.append({"parcel_id": verdict["parcel_id"], "disposition": verdict["disposition"],
                            "base_zoning": verdict["base_zoning"], "reason": verdict["reason"]})
    return {"use": use, "evaluated": len(parcels), "matches": matches}
