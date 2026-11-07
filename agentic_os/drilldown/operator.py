"""The explicit operator action that gives the local session holder the inspect permission for ONE project, and takes it away.

Holding the launch capability proves that the person (or program) is the local user of this sidecar. It does not by itself allow
reading a mission's recorded history: the drill-down still asks the permission plane for ``evidence.<project>.inspect`` on every
request, and a fresh installation has no such grant. This module is the recorded way to create (or remove) it:

* the grant is an ordinary entry in the encrypted grant store (subject: user ``local-owner``, resource: tool
  ``evidence.<project>.inspect``, action ``read``), so it is listed and revocable with the existing ``agentic-os permissions list`` /
  ``revoke`` commands as well;
* every grant and revocation appends a receipt (who, what, grant id) to the hash-chained drill-down audit log, and a grant whose
  receipt cannot be written is removed again (no grant without its record);
* it runs where the grant store's key is: the command line of the operator, not an HTTP route of the sidecar, so nothing a web page
  can reach is able to create it.
"""
from __future__ import annotations

import re
from typing import Any, List

from ..evidence_query.audit import new_receipt
from ..evidence_query.authz import perm

#: the one principal id the capability-session bridge produces
LOCAL_OWNER_ID = "local-owner"
_OPERATOR = re.compile(r"^[A-Za-z0-9_.@+-]{1,64}$")


class GrantRefused(ValueError):
    """The action was refused or could not be recorded. The message is fixed text and never echoes the input."""


def _permission(project: Any) -> str:
    if not isinstance(project, str) or "." in project:
        raise GrantRefused("the project id is not a valid permission scope")
    try:
        return perm(project, "inspect")
    except ValueError:
        raise GrantRefused("the project id is not a valid permission scope") from None


def _operator(name: Any) -> str:
    if not isinstance(name, str) or not _OPERATOR.match(name):
        raise GrantRefused("name the operator (1 to 64 characters: letters, digits, . _ @ + -)")
    return "operator:" + name


def local_inspect_grants(grants, project: str) -> List[Any]:
    """The grants that currently let the local owner inspect ``project`` (exactly this subject, resource and action)."""
    name = _permission(project)
    return [g for g in grants.list() if (g.subject_kind, g.subject, g.resource_kind, g.resource_name) == ("user", LOCAL_OWNER_ID, "tool", name)]


def grant_local_inspect(grants, project: str, *, operator: str, audit) -> Any:
    """Create the grant (or return the existing one) and record it. Raises ``GrantRefused`` if it cannot be recorded; then no grant remains."""
    name, who = _permission(project), _operator(operator)
    existing = local_inspect_grants(grants, project)
    if existing:
        g, outcome = existing[0], "unchanged"
    else:
        g, outcome = grants.add("user", LOCAL_OWNER_ID, "tool", name, actions=["read"], created_by=who), "ok"
    try:
        audit.append(new_receipt("drilldown.grant", who, project, mission_id="", side_effects=0, outcome=outcome, subject=LOCAL_OWNER_ID,
                                 permission=name, grant_id=g.id))
    except Exception:                                       # noqa: BLE001 - an unrecorded grant must not stand
        if outcome == "ok":
            grants.remove(g.id)
        raise GrantRefused("the audit log could not be written; the grant was not kept") from None
    return g


def revoke_local_inspect(grants, project: str, *, operator: str, audit) -> int:
    """Remove every grant that lets the local owner inspect ``project`` and record it. Returns how many were removed. The removal comes
    first: if the record then cannot be written the safe direction stands and ``GrantRefused`` says so."""
    name, who = _permission(project), _operator(operator)
    found = local_inspect_grants(grants, project)
    removed = [g.id for g in found if grants.remove(g.id)]
    try:
        audit.append(new_receipt("drilldown.revoke", who, project, mission_id="", side_effects=0, outcome=("ok" if removed else "none"),
                                 subject=LOCAL_OWNER_ID, permission=name, grant_ids=removed))
    except Exception:                                       # noqa: BLE001
        raise GrantRefused(f"revoked {len(removed)} grant(s), but the audit log could not be written") from None
    return len(removed)
