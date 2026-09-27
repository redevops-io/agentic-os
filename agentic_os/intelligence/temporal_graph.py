"""Temporal entity/relationship graph projection (Intelligence-APIs plan §2, §11).

The canonical business graph the plan calls for: entities, relationships and lifecycle events, each carrying
bi-temporal timestamps — `valid_from`/`valid_to` (when the fact holds in the world) and `known_at` (when we
learned it). `as_of(valid_time, known_at)` reconstructs the graph the way `historical_replay` reconstructs
evidence: it never leaks a fact that had not happened, nor one we did not yet know — so a graph decision is
replayable exactly as it stood at the moment it was made.

The reference projection is KYC ownership (the graph grc.redevops.io screens): a counterparty, its ownership
chain, and a `sanctioned` lifecycle event on an upstream owner. Screening the same counterparty **before**
the sanction's `known_at` returns GO; **after**, NO-GO — the bi-temporal point made concrete.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True)
class GraphEntity:
    id: str
    kind: str = ""
    name: str = ""
    attrs: Mapping[str, str] = field(default_factory=dict)
    valid_from: str = ""       # ISO; "" = always valid
    known_at: str = ""         # ISO; "" = always knowable


@dataclass(frozen=True)
class GraphRelationship:
    src: str
    rel: str
    dst: str
    valid_from: str = ""
    valid_to: str = ""         # ISO; "" = still open
    known_at: str = ""
    attrs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleEvent:
    entity: str
    event: str                 # e.g. sanctioned | acquired | dissolved
    at: str = ""               # valid time it took effect
    known_at: str = ""
    attrs: Mapping[str, str] = field(default_factory=dict)


def _knowable(valid_from: str, known_at: str, vt: str, kt: str) -> bool:
    if valid_from and vt and valid_from > vt:
        return False
    if known_at and kt and known_at > kt:
        return False
    return True


@dataclass
class GraphSnapshot:
    """The graph as it stood at one (valid_time, known_at) — leakage-safe, ready to traverse."""
    entities: dict[str, GraphEntity]
    edges: tuple[GraphRelationship, ...]
    events: tuple[LifecycleEvent, ...]

    def neighbors(self, node: str, rel: Optional[str] = None) -> list[str]:
        return [e.dst for e in self.edges if e.src == node and (rel is None or e.rel == rel)]

    def reverse_neighbors(self, node: str, rel: Optional[str] = None) -> list[str]:
        return [e.src for e in self.edges if e.dst == node and (rel is None or e.rel == rel)]

    def flagged(self, event: str) -> set[str]:
        return {ev.entity for ev in self.events if ev.event == event}

    def chain(self, node: str, rel: str) -> list[str]:
        """Walk `rel` forward from `node` (e.g. owned_by → the ownership chain to the ultimate owner),
        cycle-guarded. Follows the first edge at each step; returns [node, …, ultimate]."""
        out = [node]
        seen = {node}
        cur = node
        while True:
            nxt = next((n for n in self.neighbors(cur, rel) if n not in seen), None)
            if nxt is None:
                return out
            out.append(nxt)
            seen.add(nxt)
            cur = nxt

    def path_to_flagged(self, node: str, event: str, rel: str) -> list[str]:
        """BFS over `rel` from `node`; the first path ending at an entity carrying `event`, else []."""
        flagged = self.flagged(event)
        q: deque[list[str]] = deque([[node]])
        seen = {node}
        while q:
            path = q.popleft()
            if path[-1] in flagged and len(path) > 0:
                return path
            for n in self.neighbors(path[-1], rel):
                if n not in seen:
                    seen.add(n)
                    q.append(path + [n])
        return []


class TemporalGraph:
    def __init__(self) -> None:
        self._entities: list[GraphEntity] = []
        self._edges: list[GraphRelationship] = []
        self._events: list[LifecycleEvent] = []

    def add_entity(self, e: GraphEntity) -> "TemporalGraph":
        self._entities.append(e); return self

    def add_relationship(self, r: GraphRelationship) -> "TemporalGraph":
        self._edges.append(r); return self

    def add_event(self, ev: LifecycleEvent) -> "TemporalGraph":
        self._events.append(ev); return self

    def as_of(self, valid_time: str, known_at: Optional[str] = None) -> GraphSnapshot:
        """Reconstruct the graph knowable at `known_at` (default = `valid_time`) and valid at `valid_time`."""
        kt = valid_time if known_at is None else known_at
        ents = {e.id: e for e in self._entities if _knowable(e.valid_from, e.known_at, valid_time, kt)}
        edges = tuple(r for r in self._edges
                      if _knowable(r.valid_from, r.known_at, valid_time, kt)
                      and (not r.valid_to or r.valid_to > valid_time)
                      and r.src in ents and r.dst in ents)
        events = tuple(ev for ev in self._events
                       if (not ev.at or ev.at <= valid_time) and (not ev.known_at or ev.known_at <= kt)
                       and ev.entity in ents)
        return GraphSnapshot(ents, edges, events)


# ── KYC ownership projection (the graph grc.redevops.io screens) ─────────────────────────────────────────
def project_kyc_ownership(vendors: Mapping[str, dict], *, sanction_known_at: str,
                          base_known_at: str = "") -> TemporalGraph:
    """Build a temporal ownership graph from the KYC demo vendors dict (id → {name, country, kyc,
    sanctioned_owner, hops_upstream}). The sanctioned-owner chain is materialised as `owned_by` edges up to
    the ultimate owner, which carries a `sanctioned` lifecycle event known only from `sanction_known_at`."""
    g = TemporalGraph()
    for vk, v in vendors.items():
        g.add_entity(GraphEntity(vk, "organization", v.get("name", vk),
                                 {"country": v.get("country", ""), "kyc": v.get("kyc", "")},
                                 known_at=base_known_at))
        owner, hops = v.get("sanctioned_owner"), int(v.get("hops_upstream", 0) or 0)
        if not owner or hops <= 0:
            continue
        prev = vk
        for i in range(1, hops):          # hops-1 intermediate owners, then the ultimate owner
            nid = f"{vk}::owner{i}"
            g.add_entity(GraphEntity(nid, "organization", f"intermediate owner {i} of {v.get('name', vk)}",
                                     known_at=base_known_at))
            g.add_relationship(GraphRelationship(prev, "owned_by", nid, known_at=base_known_at))
            prev = nid
        g.add_entity(GraphEntity(owner, "organization", owner, known_at=base_known_at))
        g.add_relationship(GraphRelationship(prev, "owned_by", owner, known_at=base_known_at))
        g.add_event(LifecycleEvent(owner, "sanctioned", at=sanction_known_at, known_at=sanction_known_at))
    return g


def screen_ownership(graph: TemporalGraph, applicant: str, *, valid_time: str,
                     known_at: Optional[str] = None, rel: str = "owned_by") -> dict:
    """A KYC screening decision over the graph as-of a decision time (mirrors the KYC decider):
    unresolved/ambiguous identity → ABSTAIN; any sanctioned entity in the ownership chain → NO-GO (cite it);
    else GO (cite the ultimate owner). Reconstructed leakage-safe, so it replays exactly as of the date."""
    snap = graph.as_of(valid_time, known_at)
    ent = snap.entities.get(applicant)
    if ent is None:
        return {"decision": "ABSTAIN", "reason": "identity not resolved", "chain": [], "flagged": None}
    if ent.attrs.get("kyc") == "ABSTAIN":
        return {"decision": "ABSTAIN", "reason": "ambiguous identity — escalate, do not guess",
                "chain": [applicant], "flagged": None}
    path = snap.path_to_flagged(applicant, "sanctioned", rel)
    if path:
        return {"decision": "NO-GO", "reason": f"sanctioned entity in the ownership chain: {path[-1]}",
                "chain": path, "flagged": path[-1]}
    chain = snap.chain(applicant, rel)
    return {"decision": "GO", "reason": f"clean ownership chain to {chain[-1]}", "chain": chain, "flagged": None}
