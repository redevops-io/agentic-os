"""Render an Analysis (+ Rust crates, launcher, platform notes) as a CycloneDX 1.5 BOM (JSON)."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .analyze import Analysis, Comp

NS = uuid.UUID("6f3d1d0e-8d84-5a38-9b0b-2c0b7f0a5c11")
TYPE_MAP = {"library": "library", "application": "application", "file": "file", "data": "data", "platform": "platform", "framework": "framework"}


def bom_ref(key: str) -> str:
    return "rdo:" + key


def licences(l: dict | None) -> list[dict]:
    if not l:
        return []
    if l.get("expression"):
        return [{"expression": l["expression"]}]
    return [{"license": {"name": l["name"]}}]


def component(c: Comp) -> dict[str, Any]:
    d: dict[str, Any] = {"type": TYPE_MAP.get(c.kind, "library"), "bom-ref": bom_ref(c.key), "name": c.name}
    if c.version:
        d["version"] = c.version
    if c.purl:
        d["purl"] = c.purl
    if c.scope != "required":
        d["scope"] = c.scope
    if c.hashes:
        d["hashes"] = [{"alg": a, "content": v} for a, v in sorted(c.hashes.items())]
    lic = licences(c.licence)
    if lic:
        d["licenses"] = lic
    if c.refs:
        d["externalReferences"] = [{"type": t, "url": u} for t, u in sorted(c.refs.items())]
    p = {"redevops:key": c.key,
         "redevops:licence-status": "declared" if c.licence else "unverified (no licence read)",
         "redevops:shipped_in": ",".join(sorted(c.shipped_in)) or "none",
         "redevops:evidence": " | ".join(c.evidence) or "attribution only",
         }
    if c.licence:
        p["redevops:licence-source"] = c.licence.get("source", "")
    if c.entries:
        p["redevops:archive-entries"] = str(len(c.entries))
    if c.nested_of:
        p["redevops:part-of"] = bom_ref(c.nested_of)
    if c.limitations:
        p["redevops:limitations"] = " | ".join(c.limitations)
    p.update(c.props)
    d["properties"] = [{"name": k, "value": v} for k, v in sorted(p.items())]
    return d


def build(root: dict, comps: list[Comp], limitations: dict[str, str], timestamp: str, tool_versions: dict[str, str],
          deps: dict[str, list[str]], notes: dict[str, str]) -> dict[str, Any]:
    cs = [component(c) for c in sorted(comps, key=lambda c: (c.kind, c.name.lower(), c.version or "", c.key))]
    refs = [c["bom-ref"] for c in cs]
    assert len(refs) == len(set(refs)), "duplicate bom-ref"
    body = json.dumps(cs, sort_keys=True)
    props = [{"name": f"redevops:limitation:{k}", "value": v} for k, v in sorted(limitations.items())]
    props += [{"name": f"redevops:{k}", "value": v} for k, v in sorted(notes.items())]
    props.append({"name": "redevops:licence-wording", "value": "Licences are DECLARED by package metadata; none is asserted as cleared. Product licence: source-available (family licence: AGPL-3.0-or-later with the Commons Clause)."})
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(NS, hashlib.sha256(body.encode()).hexdigest())), "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {"components": [{"type": "application", "name": "redevops packaging/sbom/generate_sbom.py", "version": tool_versions.get("generator", "1")}]},
            "component": root, "properties": props,
        },
        "components": cs,
        "dependencies": [{"ref": r, "dependsOn": sorted(set(v))} for r, v in sorted(deps.items())],
        "compositions": [{"aggregate": "incomplete", "assemblies": [root["bom-ref"]]}],
    }
    return bom
