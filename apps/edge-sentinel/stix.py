"""Minimal, faithful STIX 2.1 model + IOC normalization for Edge Sentinel CTI (Phase 2).

The plan makes structured CTI first-class: STIX 2.1 as the normalized representation, ATT&CK via its STIX
data, and a graph of provenanced relationships (never a prose-only intel store where structure exists).
This is a deliberately small subset — the object types the acceptance path needs (Indicator, AttackPattern,
Relationship, Sighting, and the SCOs behind an IOC) — not a full STIX library.

Two design choices carry the plan's requirements:
  * **Deterministic ids.** STIX ids are ``<type>--<uuid5(NS, key)>`` so the same IOC/technique always
    yields the same id — the CTI graph is replayable, and re-ingesting an indicator does not duplicate it.
  * **Provenance on everything derived.** Every object and every relationship carries a :class:`Provenance`
    (source, method, evidence_refs). "All derived edges with provenance" is an acceptance criterion, so it
    is a required field on relationships, not an afterthought.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from .evidence import _now, canonical_json

# Fixed namespace so uuid5 ids are stable across processes/replays.
NAMESPACE = uuid.UUID("8f9e6b1a-2c3d-5e4f-9a0b-1c2d3e4f5a6b")


def stix_id(stix_type: str, key: str) -> str:
    return f"{stix_type}--{uuid.uuid5(NAMESPACE, f'{stix_type}:{key}')}"


@dataclass(frozen=True)
class Provenance:
    """How a CTI object/edge came to exist. ``method`` is e.g. 'ioc-normalization', 'attack-map',
    'sighting-correlation'; ``evidence_refs`` link back to the immutable evidence spine (Phase 1)."""
    source: str
    method: str
    evidence_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["evidence_refs"] = list(self.evidence_refs); return d


@dataclass(frozen=True)
class StixObject:
    type: str
    key: str                        # the stable identity key (value for an SCO, technique id for ATT&CK)
    props: dict
    provenance: Provenance

    @property
    def id(self) -> str:
        return stix_id(self.type, self.key)

    def to_dict(self) -> dict:
        return {"type": self.type, "id": self.id, **self.props,
                "x_provenance": self.provenance.to_dict()}


@dataclass(frozen=True)
class Relationship:
    """A STIX SRO — a provenanced edge. ``provenance`` is required (acceptance: derived edges carry it)."""
    relationship_type: str
    source_ref: str
    target_ref: str
    provenance: Provenance
    confidence: int = 50

    @property
    def id(self) -> str:
        return stix_id("relationship",
                       f"{self.relationship_type}:{self.source_ref}->{self.target_ref}")

    def to_dict(self) -> dict:
        return {"type": "relationship", "id": self.id, "relationship_type": self.relationship_type,
                "source_ref": self.source_ref, "target_ref": self.target_ref,
                "confidence": self.confidence, "x_provenance": self.provenance.to_dict()}


# ──────────────────────────── IOC normalization ────────────────────────────

_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_MD5 = re.compile(r"^[a-fA-F0-9]{32}$")
_SHA1 = re.compile(r"^[a-fA-F0-9]{40}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")


def _refang(value: str) -> str:
    """Undo common defanging so 'hxxp://1[.]2[.]3[.]4' canonicalizes cleanly."""
    v = value.strip()
    v = v.replace("[.]", ".").replace("(.)", ".").replace("[dot]", ".")
    v = v.replace("hxxp://", "http://").replace("hxxps://", "https://")
    v = v.replace("[:]", ":").replace("\\", "")
    return v


def classify_ioc(value: str) -> tuple[str, str]:
    """Return (stix_sco_type, ioc_kind) for a raw IOC value. Deterministic, no network."""
    v = _refang(value)
    if _IPV4.match(v):
        return "ipv4-addr", "ipv4"
    if _SHA256.match(v):
        return "file", "sha256"
    if _SHA1.match(v):
        return "file", "sha1"
    if _MD5.match(v):
        return "file", "md5"
    if v.startswith(("http://", "https://")):
        return "url", "url"
    if _DOMAIN.match(v):
        return "domain-name", "domain"
    return "artifact", "unknown"


def normalize_ioc(value: str, *, source: str = "analyst", evidence_refs: tuple[str, ...] = ()
                  ) -> tuple[StixObject, StixObject]:
    """Canonicalize a raw IOC → (SCO observable, Indicator SDO), both provenanced and deterministically
    identified. The Indicator's ``pattern`` is a STIX pattern over the SCO."""
    v = _refang(value)
    sco_type, kind = classify_ioc(v)
    prov = Provenance(source=source, method="ioc-normalization", evidence_refs=tuple(evidence_refs))

    if sco_type == "file":
        sco_props = {"hashes": {kind.upper() if kind != "sha256" else "SHA-256": v.lower()}}
        pattern = f"[file:hashes.'{'SHA-256' if kind=='sha256' else kind.upper()}' = '{v.lower()}']"
        key = v.lower()
    elif sco_type == "ipv4-addr":
        sco_props = {"value": v}; pattern = f"[ipv4-addr:value = '{v}']"; key = v
    elif sco_type == "domain-name":
        sco_props = {"value": v.lower()}; pattern = f"[domain-name:value = '{v.lower()}']"; key = v.lower()
    elif sco_type == "url":
        sco_props = {"value": v}; pattern = f"[url:value = '{v}']"; key = v
    else:
        sco_props = {"value": v}; pattern = f"[artifact:payload_bin = '{v}']"; key = v

    sco = StixObject(type=sco_type, key=key, props=sco_props, provenance=prov)
    indicator = StixObject(
        type="indicator", key=f"{kind}:{key}",
        props={"name": f"{kind} {key}", "pattern_type": "stix", "pattern": pattern,
               "indicator_types": ["malicious-activity"], "valid_from": prov.created_at,
               "x_ioc_kind": kind, "x_ioc_value": key, "x_sco_ref": sco.id},
        provenance=prov)
    return sco, indicator
