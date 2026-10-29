"""The PUBLIC DEMO scenario engine for the permissions showcase.

A visitor of the public demo can pick a scenario from a fixed list and see what it yields. Everything here is SYNTHETIC: the grants are
built in memory from the definitions below, the data is the bundled fake customer table, and the plane that evaluates them is a
throw-away ``PermissionsPlane`` over an in-memory store. Nothing here reads, writes, mirrors or describes the REAL grant store; the
visitor chooses a scenario id and nothing else — never an identity, a role, a resource or an action.

What a result carries: the scenario's title and canned explanation, the synthetic rows the scenario's principal may see (masked cells
as ``None``), counts and the column list. It never carries a decision object, a rule, a row predicate, a reason string from the plane, a
principal, store status or any real grant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .permissions import (PREVIEW_COLS, PREVIEW_ROWS, Grant, Identity, PermissionsPlane)

SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_RESOURCE = ("table", "crm.customers")


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    explanation: str
    identity: Identity
    grants: tuple
    action: str = "read"


def _grant(gid, kind, subject, **kw):
    return Grant(id=f"demo-{gid}", subject_kind=kind, subject=subject, resource_kind=_RESOURCE[0], resource_name=_RESOURCE[1],
                 created_by="demo-scenario", **kw)


SCENARIOS: dict[str, Scenario] = {s.id: s for s in (
    Scenario("support-us-masked", "A support app: US rows only, email hidden",
             "An application that may read customers in one region, with one column masked.",
             "The grant limits the rows to region 'us' and hides the email column; every other row is withheld.",
             Identity(app="support"),
             (_grant("support", "app", "support", row_scope="in", row_column="region", row_values=["us"], masked_columns=["email"]),)),
    Scenario("finance-all-notes-masked", "A finance role: every row, notes hidden",
             "A role with access to every row but not to the free-text notes.",
             "The grant covers all rows and masks the notes column.",
             Identity(roles=["finance"]),
             (_grant("finance", "role", "finance", masked_columns=["notes"]),)),
    Scenario("analyst-own-or-eu", "An analyst: own accounts OR the EU region",
             "A role holding two row grants on one resource: the rows she owns, and the EU rows.",
             "Grants add up as a union of whole predicates: owner = the user, OR region in ('eu'). Neither replaces the other.",
             Identity(user="alice", roles=["analyst"]),
             (_grant("own", "role", "analyst", row_scope="own", row_column="owner"),
              _grant("eu", "role", "analyst", row_scope="in", row_column="region", row_values=["eu"]))),
    Scenario("masks-per-row", "Two grants that mask differently: decided row by row",
             "One grant masks email on EU rows; another exposes it on US rows.",
             "A column is masked on a row only if every grant that authorises THAT row masks it, so EU rows hide the email and US rows show it.",
             Identity(roles=["regional"]),
             (_grant("eu-masked", "role", "regional", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"]),
              _grant("us-open", "role", "regional", row_scope="in", row_column="region", row_values=["us"]))),
    Scenario("read-only-cannot-write", "A read-only grant asked to write",
             "A principal that may read customers tries to write them.",
             "A read grant never authorises a write: the request is denied and nothing is returned.",
             Identity(app="reporting"), (_grant("reporting", "app", "reporting"),), action="write"),
    Scenario("no-grant-denied", "No grant: denied",
             "A principal with no grant on the table.",
             "Without a grant the answer is no: access is denied by default.",
             Identity(app="stranger"), ()),
)}


class _SyntheticStore:
    """An in-memory, read-only stand-in store holding ONE scenario's grants. Never the real store."""
    sealed = False

    def __init__(self, grants):
        self._grants = list(grants)

    def list(self):
        return list(self._grants)

    def snapshot(self):
        return list(self._grants), False


def list_scenarios() -> list[dict]:
    """The fixed list a visitor can choose from: id, title, description. Nothing else."""
    return [{"id": s.id, "title": s.title, "description": s.description} for s in SCENARIOS.values()]


def evaluate(scenario_id: str) -> dict:
    """Run one scenario over the synthetic table. ``KeyError`` for an id that is not in the allowlist (callers validate first)."""
    sc = SCENARIOS[scenario_id]
    plane = PermissionsPlane(_SyntheticStore(sc.grants))
    decision = plane.authorize(sc.identity, _RESOURCE[0], _RESOURCE[1], sc.action)
    full = len(PREVIEW_ROWS)
    rows = plane.slice([dict(r) for r in PREVIEW_ROWS], decision) if decision.allowed else []
    return {"id": sc.id, "title": sc.title, "description": sc.description, "explanation": sc.explanation, "action": sc.action,
            "allowed": bool(decision.allowed), "columns": list(PREVIEW_COLS), "full_count": full, "visible_count": len(rows),
            "withheld": full - len(rows), "records": rows}


def valid_scenario_id(value) -> bool:
    return isinstance(value, str) and SCENARIO_ID.fullmatch(value) is not None
