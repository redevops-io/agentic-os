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

import json
import os
import re

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


# ── fail-safe zoning-code classifier ──
# Only ANCHORED, whole-code patterns map to a family. Anything else (RC-1, ROS, RI, RT, RPD, PD, overlays such
# as "R-1/HD", free text, unknown) is "unknown" and never yields PERMITTED. Patterns are deliberately
# conservative: a code that merely *starts* with R is NOT residential (RC = regional commercial, ROS = open
# space, RI = research/institutional, ...).
_NUM = r"(?:-?\d+(?:\.\d+)?[A-Z]?)"
_RESIDENTIAL = [  # (pattern, density) — density 'sf' single-family oriented, 'mf' multi-family oriented, 'gen' generic R-n
    (re.compile(rf"^R-?\d+(?:\.\d+)?[A-Z]?$"), "gen"),
    (re.compile(rf"^(?:RS|RE|RR|SF|RSF){_NUM}?$"), "sf"),
    (re.compile(rf"^(?:RM|RH|MF|RMF){_NUM}?$"), "mf"),
]
_COMMERCIAL = re.compile(rf"^(?:C|CB|CG|CN|CS|CC|CBD|NC|GC|TC|B){_NUM}?$")
_INDUSTRIAL = re.compile(rf"^(?:I|M|IL|IG|IH|LI|HI|IND){_NUM}?$")
_MIXED = re.compile(rf"^(?:MU|MX|MUD|NMU|MXD){_NUM}?$")

#: Extension point: per-jurisdiction code tables. JSON file (path in $ZONING_CODE_TABLE) shaped
#: {"<jurisdiction>": {"<CODE>": "residential|commercial|industrial|mixed|unknown"}}. Entries override patterns.
def _load_code_table() -> dict:
    path = os.environ.get("ZONING_CODE_TABLE", "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_FAMILIES = {"residential", "commercial", "industrial", "mixed", "unknown"}


def classify_code(code: str, jurisdiction: str = "") -> tuple[str, str]:
    """Return (family, density). family in residential|commercial|industrial|mixed|unknown."""
    c = (code or "").strip().upper()
    if not c:
        return "unknown", ""
    table = _load_code_table().get(jurisdiction or "", {})
    hit = table.get(c) if isinstance(table, dict) else None
    if isinstance(hit, str) and hit.lower() in _FAMILIES:
        return hit.lower(), "gen"
    for pat, density in _RESIDENTIAL:
        if pat.match(c):
            return "residential", density
    if _MIXED.match(c):
        return "mixed", ""
    if _COMMERCIAL.match(c):
        return "commercial", ""
    if _INDUSTRIAL.match(c):
        return "industrial", ""
    return "unknown", ""


def code_family(code: str, jurisdiction: str = "") -> str:
    """Coarse, fail-safe family classifier (see :func:`classify_code`). Ambiguous codes are 'unknown'."""
    return classify_code(code, jurisdiction)[0]


_FETCHERS: dict = {}


def register_fetcher(name: str, fn) -> None:
    """Register a real evidence fetcher (e.g. a GIS harvest). Only evidence it produces may be labelled with
    its name as the source; otherwise evidence is `caller_supplied`."""
    _FETCHERS[name] = fn


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
    """Return the zoning evidence available for a parcel. Without a registered fetcher this ECHOES what the
    caller supplied and says so (`caller_supplied`) — provenance is never invented. A named, registered
    fetcher (body["fetcher"]) may supply the zoning code, in which case its name is the source. Read-only."""
    src = "caller_supplied"
    code = body.get("zoning_code") or ""
    overlays = list(body.get("overlays", []))
    ordinance = body.get("ordinance_url", "")
    fetcher = _FETCHERS.get(body.get("fetcher", ""))
    if fetcher is not None:
        got = fetcher(body) or {}
        if got:
            src = body["fetcher"]
            code = got.get("zoning_code", code)
            overlays = list(got.get("overlays", overlays))
            ordinance = got.get("ordinance_url", ordinance)
    jur = body.get("jurisdiction", "")
    return {
        "parcel_id": body.get("parcel_id", ""),
        "base_zoning": code.strip(),
        "code_family": code_family(code, jur),
        "ordinance_url": ordinance,
        "overlays": overlays,
        "evidence_sources": [src] + ([f"{src}:ordinance_url"] if ordinance else []),
        "verified": src != "caller_supplied",
    }


def evaluate_use(body: dict) -> dict:
    """Deterministic-first, fail-safe disposition for a use against a parcel's base zoning.

    PERMITTED only for an unambiguous residential code with a residential use of compatible density, or a
    mixed-use code where the caller supplies the use in `permitted_uses` (from the ordinance). Clear
    cross-family conflicts are PROHIBITED; everything ambiguous (unknown codes, overlays, mixed, commercial
    base with a non-commercial use) is UNKNOWN. `false_permit` is checked against an INDEPENDENT rule table
    (`_never_permit`), not the classifier that produced the disposition."""
    use = body.get("use", "")
    code = body.get("base_zoning") or body.get("zoning_code", "")
    jur = body.get("jurisdiction", "")
    fam, density = classify_code(code, jur)
    if body.get("overlays"):
        fam, density = "unknown", ""   # overlays can restrict base rights; we have not parsed them
    ufam = USE_FAMILY.get(use)
    if ufam is None:
        disp, why = "UNKNOWN", f"use {use!r} is not in the normalized ontology"
    elif fam == "unknown":
        disp, why = "UNKNOWN", "zoning code unrecognized/ambiguous (or overlay present) - verify against the ordinance"
    elif fam == "mixed":
        if use in (body.get("permitted_uses") or ()):
            disp, why = "PERMITTED", f"mixed-use base {code}: use listed as permitted in the supplied ordinance table"
        else:
            disp, why = "UNKNOWN", f"mixed-use base {code}: permitted uses vary by district - verify"
    elif fam == "residential":
        if ufam == "residential":
            ok = (use == "RESIDENTIAL_SINGLE_FAMILY" and density in ("gen", "sf")) or \
                 (use == "RESIDENTIAL_MULTI_FAMILY" and density == "mf")
            if ok:
                disp, why = "PERMITTED", f"base zoning {code} (residential) permits {use.lower().replace('_', ' ')} by right"
            else:
                disp, why = "UNKNOWN", f"{code} is residential but density fit for {use} needs the ordinance - verify"
        else:
            disp, why = "PROHIBITED", f"base zoning {code} (residential) excludes a {ufam} use"
    elif fam == "industrial" and ufam == "residential":
        disp, why = "PROHIBITED", f"base zoning {code} (industrial) excludes a residential use"
    elif fam == "commercial" and ufam == "industrial":
        disp, why = "PROHIBITED", f"base zoning {code} (commercial) excludes an industrial use"
    elif fam == ufam and use in NEEDS_ORDINANCE:
        disp, why = "UNKNOWN", "base-compatible, but permitted/conditional status needs the ordinance - verify"
    else:
        disp, why = "UNKNOWN", f"{code} ({fam}) vs {ufam} use: depends on the ordinance - verify"
    return {"parcel_id": body.get("parcel_id", ""), "use": use, "base_zoning": code,
            "disposition": disp, "reason": why, "ordinance_url": body.get("ordinance_url", ""),
            "family": fam, "false_permit": _false_permit(disp, use, code, body)}


# Independent tripwire: codes that must NEVER yield PERMITTED regardless of the classifier. Deliberately a
# separate, literal rule (leading-token blocklist), so a classifier bug cannot cancel it out.
_RES_OK = re.compile(r"^(?:R-?\d|RS|RE|RR|SF|RM|RH|MF)")


def _false_permit(disp: str, use: str, code: str, body: dict) -> bool:
    if disp != "PERMITTED":
        return False
    c = (code or "").strip().upper()
    if use not in USE_FAMILY:
        return True
    if body.get("overlays"):
        return True
    if USE_FAMILY[use] == "residential" and c.startswith(("RC", "ROS", "RI", "RT", "RPD", "PD")):
        return True
    in_table = c in (_load_code_table().get(body.get("jurisdiction", ""), {}) or {})
    if USE_FAMILY[use] == "residential" and not in_table and not _RES_OK.match(c) \
            and use not in (body.get("permitted_uses") or ()):
        return True
    return False


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
