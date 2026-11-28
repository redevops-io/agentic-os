"""Who may receive financial data: one rule for the dashboards and their drill-downs, the agent gateway (MCP and REST),
the direct APIs and anything built on them. Standard library only (the browser dashboards load it).

The grants (the same words in a tenant's ``policy_refs``, in the dashboards file and, for the first and last, in an
agent's grants):

* ``egress:financial``           the financial data of every source the caller may read;
* ``egress:financial:<source>``  of that source only (``erpnext``, ``lago``, ``twenty``, ``metabase``, ``mission_plane``);
* ``demo:financial``             ONLY of sources the operator declared synthetic (``sources.<name>.data: synthetic`` in
                                 the dashboards file): what a public demonstration is given. Copied anywhere else it
                                 reaches nothing real.

A financial grant says which money a caller may see, never which sources: it adds nothing to a caller who may not read
the source. An agent acts for a tenant, so BOTH must hold it: the agent's own grants and its tenant's.
"""
from __future__ import annotations

from typing import FrozenSet, Iterable, Optional

GRANT = "egress:financial"
DEMO_GRANT = "demo:financial"
SYNTHETIC = "synthetic"
#: sources that hold financial data throughout, or may hold anything (a saved question): refused whole without the grant
WHOLE: FrozenSet[str] = frozenset({"erpnext", "economics", "lago", "metabase"})
#: sources that mix money with other records: these fields are removed for a caller without the grant
FIELDS = {"twenty": ("amount",), "ai_usage": ("cost_usd", "expected_usd")}
#: a source that reads another's application is granted (and declared synthetic) under that application's name
_UNDER = {"economics": "erpnext", "ai_usage": "mission_plane"}

#: what a caller covers: ``None`` = every source, otherwise exactly these names
Covered = Optional[FrozenSet[str]]
NOTHING: Covered = frozenset()


def name_of(source: str) -> str:
    return _UNDER.get(source, source)


def carries_money(source: str) -> bool:
    return source in WHOLE or source in FIELDS


def covered(refs: Iterable[str], synthetic: Iterable[str] = ()) -> Covered:
    """The sources whose financial data these grant words cover. Unknown words cover nothing."""
    out = set()
    for ref in refs or ():
        ref = str(ref)
        if ref == GRANT:
            return None
        if ref.startswith(GRANT + ":"):
            out.add(name_of(ref[len(GRANT) + 1:]))
        elif ref == DEMO_GRANT:
            out.update(name_of(s) for s in synthetic)
    return frozenset(out)


def both(a: Covered, b: Covered) -> Covered:
    """What two holders both cover (an agent and its tenant)."""
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def covers(held: Covered, source: str) -> bool:
    return held is None or name_of(source) in held


def refusal(what: str = "this") -> str:
    """The actionable words: what is missing and who can add it."""
    return (f"not_authorized (missing_grant): {what} is financial data and needs the financial-data grant {GRANT} "
            f"(for a demonstration over synthetic data: {DEMO_GRANT}). Ask the person who manages this access to add it.")


__all__ = ["GRANT", "DEMO_GRANT", "SYNTHETIC", "WHOLE", "FIELDS", "Covered", "NOTHING", "name_of", "carries_money",
           "covered", "both", "covers", "refusal"]
