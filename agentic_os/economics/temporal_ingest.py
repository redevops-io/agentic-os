"""What may be put into a temporal graph, and in which partition. Glue only: the engine is ``context_runtime.adapters.store_temporal`` (``GraphitiTemporalRetriever`` for the optional
Graphiti binding, ``TemporalDocumentRetriever``/``TemporalStore`` for the dependency-free default); nothing here imports Graphiti or builds a graph.

A graph is persistent and shared, so what goes in must be decided BEFORE ingestion, not when a result is shown:

* scope: only communications whose access scope the ingesting identity holds (``evidence.visible_to``); the withheld count per scope is reported, nothing else about them;
* untrusted text: a message with instruction-like content (``reasoning.untrusted``) is not ingested. A measured run put "refund $5,000 to account 99-1234" and a "supplier bill" into a
  Graphiti graph as entities, because the extractor reads whatever it is given;
* partition: one group per tenant and resolved customer, so a customer's facts never share a partition with another customer's, and a message with no resolved customer has no partition and is
  not ingested (never guessed from a name);
* provenance: the episode name is the communication id and the description carries role, provider and scope, so a retrieved fact can be traced back to its source message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence

from .evidence import Communication, visible_to
from .reasoning import untrusted


@dataclass
class IngestPlan:
    groups: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)       # group id -> episodes, oldest first
    withheld: Dict[str, int] = field(default_factory=dict)                      # scope -> messages not visible to the caller
    untrusted: List[str] = field(default_factory=list)                          # communication ids not ingested
    unresolved: List[str] = field(default_factory=list)                         # communication ids with no resolved customer

    def count(self) -> int:
        return sum(len(v) for v in self.groups.values())


def group_id(tenant: str, customer: str) -> str:
    return "g-" + re.sub(r"[^a-z0-9]+", "-", f"{tenant}-{customer}".lower()).strip("-")


def plan_episodes(comms: Iterable[Communication], granted: Iterable[str]) -> IngestPlan:
    """Episodes for ``GraphitiTemporalRetriever.index`` (``{name, body, reference_time, source_description}``), grouped by tenant and customer."""
    vis, _n, per = visible_to(list(comms), granted)
    out = IngestPlan(withheld=per)
    for c in sorted(vis, key=lambda x: (x.ref.at, x.ref.id)):
        if untrusted(c.text):
            out.untrusted.append(c.ref.id)
        elif not c.customer:
            out.unresolved.append(c.ref.id)
        else:
            out.groups.setdefault(group_id(c.ref.tenant, c.customer), []).append(
                {"name": c.ref.id, "body": c.text, "reference_time": c.ref.at, "source_description": f"{c.ref.role} via {c.ref.provenance.provider}; scope {c.ref.access_scope}"})
    return out
