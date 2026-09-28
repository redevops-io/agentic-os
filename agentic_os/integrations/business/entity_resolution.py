"""Generic cross-system entity resolution (Revenue & Execution Intelligence plan §12).

Both intelligence loops must resolve a real-world identity — a company / contact / email / domain / legal
entity — to a canonical business object BEFORE any consequential action runs against it. `supplier_resolution`
already does this for suppliers with a confidence float; this generalises it across ANY canonical business
object (Customer, Company, Contact, Party, Opportunity, …) and adds the plan's discrete state model:

    RESOLVED    exactly one strong match          → may drive consequential automated actions
    PROBABLE    one plausible-but-not-certain match → only with an explicit policy opt-in
    AMBIGUOUS   several comparably-strong matches   → never auto-acts; needs disambiguation
    UNRESOLVED  no usable match                     → never auto-acts

Matching ladder (strongest first, mirroring supplier_resolution): a shared external id (LEI/tax_id/duns/
CRM/ERP/domain key) > exact email > exact domain (incl. the domain of an email) > exact name > fuzzy name
(token Jaccard). Pure + deterministic; no I/O — feed it the candidate records the caller already has.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Mapping, Optional, Tuple


class ResolutionState(str, Enum):
    RESOLVED = "RESOLVED"
    PROBABLE = "PROBABLE"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class EntityQuery:
    """The identity hints to resolve. Any subset may be set; more hints = stronger, more certain matches."""
    name: str = ""
    email: str = ""
    domain: str = ""
    external_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Thresholds:
    resolved: float = 0.9        # a match at/above this, and unique, is RESOLVED
    probable: float = 0.6        # a match at/above this (but below resolved, or not unique) is PROBABLE
    ambiguity_margin: float = 0.1  # other matches within this of the top make the result AMBIGUOUS


@dataclass(frozen=True)
class EntityMatch:
    query: EntityQuery
    state: ResolutionState
    entity_ref: str = ""         # canonical ref of the resolved object ("" for AMBIGUOUS/UNRESOLVED)
    entity_type: str = ""        # the matched object's class name (Customer/Company/Contact/…)
    matched_on: str = ""         # external:<key> | email | domain | email_domain | name_exact | name_fuzzy
    confidence: float = 0.0
    candidate_refs: List[str] = field(default_factory=list)   # populated for AMBIGUOUS
    evidence: List[str] = field(default_factory=list)

    def may_drive_action(self, *, allow_probable: bool = False) -> bool:
        """§12: only RESOLVED, or an explicitly policy-approved PROBABLE, may drive consequential actions."""
        return self.state is ResolutionState.RESOLVED or (
            allow_probable and self.state is ResolutionState.PROBABLE)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return set(_norm(s).split())


def _email_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if email and "@" in email else ""


def _identity(obj: object) -> EntityQuery:
    """Extract identity hints from a canonical business object (duck-typed across the contract classes)."""
    name = getattr(obj, "name", "") or " ".join(
        p for p in (getattr(obj, "first_name", ""), getattr(obj, "last_name", "")) if p).strip()
    ext = {str(k).lower(): str(v) for k, v in (getattr(obj, "external_ids", {}) or {}).items()}
    return EntityQuery(name=name, email=getattr(obj, "email", "") or "",
                       domain=getattr(obj, "domain", "") or "", external_ids=ext)


def _ref(obj: object) -> str:
    """Canonical ref for a matched object: its provenance provider_ref, else its content digest."""
    prov = getattr(obj, "prov", None)
    ref = getattr(prov, "provider_ref", "") if prov is not None else ""
    if ref:
        return str(ref)
    digest = getattr(obj, "digest", None)
    return digest() if callable(digest) else ""


def _score(q: EntityQuery, c: EntityQuery) -> Tuple[str, float]:
    """Best (matched_on, confidence) between the query and one candidate's identity. 0.0 = no signal."""
    ql_ext = {k: v for k, v in q.external_ids.items() if v}
    for key, val in ql_ext.items():
        if c.external_ids.get(key) and c.external_ids[key].lower() == val.lower():
            return f"external:{key}", 0.99
    if q.email and c.email and q.email.lower() == c.email.lower():
        return "email", 0.95
    qd, cd = q.domain.lower(), c.domain.lower()
    ced = _email_domain(c.email)
    if qd and (qd == cd or qd == ced):
        return "domain", 0.9
    qed = _email_domain(q.email)
    if qed and (qed == cd or (ced and qed == ced)):
        return "email_domain", 0.82
    qn = _norm(q.name)
    if qn and _norm(c.name) == qn:
        return "name_exact", 0.85
    qt, ct = _tokens(q.name), _tokens(c.name)
    if qt and ct:
        j = len(qt & ct) / len(qt | ct)
        if j > 0:
            return "name_fuzzy", round(0.5 + 0.35 * j, 4)
    return "", 0.0


def resolve_entity(query: EntityQuery, candidates: Iterable[object], *,
                   thresholds: Optional[Thresholds] = None) -> EntityMatch:
    """Resolve `query` against `candidates` (any canonical business objects) to a discrete-state EntityMatch."""
    th = thresholds or Thresholds()
    scored = []
    for c in candidates:
        matched_on, conf = _score(query, _identity(c))
        if conf > 0.0:
            scored.append((conf, matched_on, c))
    if not scored:
        return EntityMatch(query=query, state=ResolutionState.UNRESOLVED,
                           evidence=["no candidate produced a match signal"])
    scored.sort(key=lambda t: t[0], reverse=True)
    top_conf, top_matched, top_obj = scored[0]
    near = [t for t in scored if top_conf - t[0] <= th.ambiguity_margin]

    if len(near) > 1 and top_conf >= th.probable:
        return EntityMatch(query=query, state=ResolutionState.AMBIGUOUS, confidence=top_conf,
                           candidate_refs=[_ref(o) for _, _, o in near],
                           evidence=[f"{len(near)} candidates within {th.ambiguity_margin} of top ({top_conf})"])
    if top_conf >= th.resolved:
        state = ResolutionState.RESOLVED
    elif top_conf >= th.probable:
        state = ResolutionState.PROBABLE
    else:
        return EntityMatch(query=query, state=ResolutionState.UNRESOLVED, confidence=top_conf,
                           evidence=[f"best match {top_conf} below probable threshold {th.probable}"])
    return EntityMatch(query=query, state=state, entity_ref=_ref(top_obj),
                       entity_type=type(top_obj).__name__, matched_on=top_matched, confidence=top_conf,
                       evidence=[f"matched on {top_matched} @ {top_conf}"])
