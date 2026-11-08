"""Authorization contract v1 — PR-A, CONTRACT ONLY: nothing here enforces anything yet.

Defines the vocabulary that PR-B/PR-E (kernel routes, enterprise adapters) implement against, so the
AGPL governance plane and the enterprise permissioning plane agree on tenancy, revocation epochs and
decision digests before either behaviour changes. Terminology follows the cross-app permissions plan
(§4.5): the initiating human operator's effective grants are the ceiling; a mission/step/host adds no
default narrowing; an explicit workflow overlay may only restrict.

Compatibility rules (binding, pinned by tests/test_authz_contract.py):

* ``tenant == ""`` and ``epoch == 0`` are EXACTLY the pre-contract semantics: single-operator,
  no epoch tracking. A pre-contract :class:`~agentic_os.permissions.Grant` or
  :class:`~agentic_os.permissions.AccessDecision` serializes, digests and answers byte-identically.
* A tenant id, when set, must be a valid identifier (``grant_problem`` refuses anything else, and a
  malformed grant is filtered by ``PermissionsPlane.authorize`` — fail closed, as with every other
  malformed grant field).
* A decision digest is the SHA-256 over the canonical JSON of the decision plus the identity the
  decision was made for. Consumers that cache authorization (enterprise adapters, plan caches) key on
  ``(tenant, principal, resource, action, digest, epoch)`` and MUST treat
  ``entry_epoch != current_epoch`` as stale — a revocation bumps the epoch, and a stale entry may
  never serve a decision (plan §4.5 "Revocation/erasure epoch checked at retrieval").
* The public-demo compatibility contract (audit 2026-11-08 §10, ``audit/public_demo_contract.json``)
  is part of this vocabulary: authorization changes are action/resource-scoped, never blanket, and
  a documented public demo journey returning anything but its pinned response is a release blocker.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import identifiers as _ids

CONTRACT_VERSION = 1
CANONICAL_PREFIX = "agentic-os/authz-contract/v1"


@dataclass(frozen=True)
class TenantContext:
    """Who the request is acting FOR, tenancy-wise. ``tenant == ""`` is the community single-operator
    deployment: everything tenant-shaped is bypassed exactly as before this contract existed."""

    tenant: str = ""
    epoch: int = 0

    def invalid_reason(self) -> str | None:
        if self.tenant and not _ids.valid_identifier(self.tenant):
            return "tenant is not a valid identifier"
        if type(self.epoch) is not int or self.epoch < 0:
            return "epoch must be a non-negative integer"
        return None

    @property
    def is_valid(self) -> bool:
        return self.invalid_reason() is None

    @property
    def is_single_operator(self) -> bool:
        return not self.tenant and not self.epoch


def canonical_decision(decision) -> str:
    """Deterministic JSON for an ``AccessDecision`` (or its dict form) — same decision, same bytes."""
    d = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def decision_digest(*, principal: str, tenant: str, resource_kind: str, resource_name: str,
                    action: str, decision, revocation_epoch: int = 0) -> str:
    """The digest a receipt or cache entry stores. Covers WHO, FOR WHICH TENANT, ON WHAT, TO DO WHAT,
    the FULL decision (scope + masks + rules) and the EPOCH it was evaluated under — a revocation
    epoch change changes the key, so no stale entry can be looked up even by accident."""
    payload = {"prefix": CANONICAL_PREFIX, "principal": principal, "tenant": tenant,
               "resource_kind": resource_kind, "resource_name": resource_name, "action": action,
               "epoch": revocation_epoch, "decision": json.loads(canonical_decision(decision))}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cache_entry_is_fresh(entry: dict, *, current_epoch: int) -> bool:
    """A cache entry survives only under the SAME epoch it was written under. ``epoch 0`` is the
    pre-contract epoch: entries without an epoch field are treated as epoch 0 (compatibility), so a
    deployment that never bumps epochs behaves exactly as before, and one that bumps it invalidates
    everything at once (revoke-all) — there is no middle state where a bumped epoch still hits."""
    return int(entry.get("epoch", 0) or 0) == int(current_epoch or 0)
