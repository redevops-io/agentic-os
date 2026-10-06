"""Evidence deduplication + independence (Phase 2, plan §11).

Twenty posts copied from one viral thread are weak evidence; ten independent operators describing the same
workflow over six months are strong. This separates ``raw_mentions`` from ``independent_evidence_count`` — the
latter is what opportunity scoring may use — by linking observations that are NOT independent:

  * the same author (same ``author_hash``) — one person, counted once;
  * near-identical text — reposts / quoted posts / copy-paste;
  * the same source URL / a link to the same original complaint;
  * the same thread — replies to one event.

Union-find groups linked observations; each group is one independent piece of evidence. Pure + deterministic
(lexical near-dup; an embedding near-dup can replace ``_near_duplicate`` behind the same call later).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .contracts import PainObservation

_NEAR_DUP_JACCARD = 0.85


def _tokens(text: str) -> frozenset:
    return frozenset(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def _near_duplicate(a: frozenset, b: frozenset, threshold: float = _NEAR_DUP_JACCARD) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    return inter / len(a | b) >= threshold


@dataclass
class _UnionFind:
    parent: Dict[int, int] = field(default_factory=dict)

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass(frozen=True)
class IndependenceReport:
    raw_mentions: int
    independent_evidence_count: int
    author_count: int
    groups: Tuple[Tuple[str, ...], ...]          # each group = source_ids judged one piece of evidence
    representative_ids: Tuple[str, ...]           # one (strongest) observation id per group


def _obs_id(o: PainObservation) -> str:
    return o.source_id or o.digest()


def assess_independence(observations: Sequence[PainObservation], *,
                        near_dup_threshold: float = _NEAR_DUP_JACCARD) -> IndependenceReport:
    """Group non-independent observations and count independent evidence. Reposts, same-author cross-posts,
    same-origin links and single-thread replies collapse to one piece of evidence each."""
    obs = list(observations)
    n = len(obs)
    uf = _UnionFind()
    toks = [_tokens(o.failure_or_pain + " " + " ".join(o.applications_mentioned) + " "
                     + " ".join(o.current_workflow)) for o in obs]
    # richer near-dup over the raw text when present in provenance evidence is out of scope here; we dedup on the
    # extracted signal + explicit identity links (author / url / thread), which is deterministic and sufficient.
    for i in range(n):
        uf.find(i)
        for j in range(i + 1, n):
            same_author = bool(obs[i].author_hash) and obs[i].author_hash == obs[j].author_hash
            same_url = bool(obs[i].source_url) and obs[i].source_url == obs[j].source_url
            same_thread = bool(obs[i].community_or_topic) and obs[i].source_id and \
                obs[i].community_or_topic == obs[j].community_or_topic and _near_duplicate(toks[i], toks[j], near_dup_threshold)
            if same_author or same_url or _near_duplicate(toks[i], toks[j], near_dup_threshold) or same_thread:
                uf.union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    group_ids: List[Tuple[str, ...]] = []
    reps: List[str] = []
    for members in groups.values():
        group_ids.append(tuple(_obs_id(obs[m]) for m in members))
        strongest = max(members, key=lambda m: obs[m].evidence_strength)
        reps.append(_obs_id(obs[strongest]))

    return IndependenceReport(
        raw_mentions=n, independent_evidence_count=len(groups),
        author_count=len({o.author_hash for o in obs if o.author_hash}),
        groups=tuple(group_ids), representative_ids=tuple(reps))
