"""PR-A compatibility pins: the authz contract is CONTRACT-ONLY — nothing here was allowed to
change behaviour, and these tests are the proof (owner clarification: behavior-preserving)."""
from __future__ import annotations

import json

import pytest

from agentic_os.authz_contract import (TenantContext, cache_entry_is_fresh, canonical_decision,
                                       decision_digest)
from agentic_os.permissions import (AccessDecision, Grant, GrantStore, Identity, PermissionsPlane,
                                    grant_problem)

LEGACY_GRANT_KEYS = ["id", "subject_kind", "subject", "resource_kind", "resource_name", "actions",
                     "row_scope", "row_column", "row_values", "masked_columns", "created_by", "at"]


def _legacy_grant():
    return Grant(id="g1", subject_kind="user", subject="amy", resource_kind="table",
                 resource_name="crm.customers", actions=["read"], row_scope="own",
                 row_column="owner", row_values=[], masked_columns=["email"], created_by="sys", at=1.0)


# ── byte-compatibility of the pre-contract shape ───────────────────────────────────────────────────

def test_legacy_grant_serializes_byte_identically():
    g = _legacy_grant()
    d = g.to_dict()
    assert list(d.keys()) == LEGACY_GRANT_KEYS, "legacy grant gained a key — stored files must not drift"
    g2 = Grant.from_dict(json.loads(json.dumps(d)))
    assert g2 == g and g2.tenant == "" and g2.revocation_epoch == 0
    assert g2.to_dict() == d


def test_legacy_access_decision_serializes_byte_identically():
    dec = AccessDecision(True, "allow · own rows", row_filter={"column": "owner", "op": "eq", "value": "amy"},
                         masked_columns=["email"], rules=[{"row_filter": None, "masked_columns": []}])
    assert list(dec.to_dict().keys()) == ["allowed", "reason", "row_filter", "masked_columns", "rules"]
    bare = AccessDecision(False, "denied: nope")
    assert list(bare.to_dict().keys()) == ["allowed", "reason", "row_filter", "masked_columns", "rules"]


def test_contract_fields_appear_only_when_set():
    g = _legacy_grant()
    g2 = Grant(**{**g.to_dict(), "tenant": "acme", "revocation_epoch": 7})
    d = g2.to_dict()
    assert d["tenant"] == "acme" and d["revocation_epoch"] == 7
    assert Grant.from_dict(d) == g2
    dec = AccessDecision(False, "denied", tenant="acme", decision_epoch=3)
    assert dec.to_dict()["tenant"] == "acme" and dec.to_dict()["decision_epoch"] == 3


# ── malformed contract values fail closed like every other malformed grant field ───────────────────

@pytest.mark.parametrize("tenant,epoch,expect", [
    ("", 0, None), ("acme", 3, None),
    # the house valid_identifier rejects Unicode hazards, not character classes: a zero-width (Cf) is the honest "malformed"
    ("acme​me", 0, "tenant is not a valid identifier"),
    ("acme", -1, "revocation_epoch must be a non-negative integer"),
])
def test_grant_problem_on_contract_fields(tenant, epoch, expect):
    g = Grant(**{**_legacy_grant().to_dict(), "tenant": tenant, "revocation_epoch": epoch})
    assert grant_problem(g) == expect


def test_stored_legacy_grant_roundtrips_unchanged(tmp_path, monkeypatch):
    """A grant written before the contract, stored as plaintext JSONL bytes, is read and matched WITHOUT
    gaining keys — the on-disk shape is part of the compatibility surface. tests/conftest.py gives the suite a
    development key, so the kernel encrypts a plaintext store on first load and keeps the original bytes as a
    `grants.jsonl.migrated-*` backup; both halves are asserted here, because both are the kernel's own
    behaviour and neither is allowed to rewrite the pre-contract shape."""
    monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "1")
    legacy = {"id": "g-old", "subject_kind": "user", "subject": "amy", "resource_kind": "table",
              "resource_name": "crm.customers", "actions": ["read"], "row_scope": "all",
              "row_column": "owner", "row_values": [], "masked_columns": [], "created_by": "sys", "at": 1.0}
    original = json.dumps({"version": 1, "grants": [legacy], "meta": {"counter": 1}}) + "\n"
    (tmp_path / "grants.jsonl").write_text(original)
    store = GrantStore(str(tmp_path), allow_plaintext_dev=True)
    plane = PermissionsPlane(store)
    d = plane.authorize(Identity(user="amy", app="crm"), "table", "crm.customers", "read")
    assert d.allowed and d.tenant == "" and d.decision_epoch == 0     # pre-contract decision shape
    stored = [g.to_dict() for g in store.list()]
    assert legacy in stored, "stored legacy grant drifted on read"
    backups = sorted(tmp_path.glob("grants.jsonl.migrated-*"))
    if backups:                                                       # the kernel migrated the store
        assert backups[0].read_text() == original, "migration rewrote the pre-contract bytes"
        assert (tmp_path / "grants.enc").is_file(), "the migrated store must be the encrypted one"


# ── digest + epoch semantics ───────────────────────────────────────────────────────────────────────

def test_decision_digest_is_stable_and_context_bound():
    dec = AccessDecision(True, "allow · all rows")
    kw = dict(principal="amy", resource_kind="table", resource_name="crm.customers", action="read", decision=dec)
    a = decision_digest(tenant="", **kw)
    b = decision_digest(tenant="", **kw)
    assert a == b and a.startswith("sha256:")
    assert decision_digest(tenant="acme", **kw) != a          # tenant is part of the identity
    assert decision_digest(tenant="", revocation_epoch=1, **kw) != a   # epoch is part of the key
    assert decision_digest(tenant="", **{**kw, "action": "write"}) != a


def test_epoch_0_is_the_pre_contract_epoch_and_bump_invalidates_all():
    legacy_entry = {"decision": "sha256:x"}                  # written before the contract existed
    assert cache_entry_is_fresh(legacy_entry, current_epoch=0)      # untouched deployments: unchanged
    assert not cache_entry_is_fresh(legacy_entry, current_epoch=1)  # a bump invalidates everything at once
    assert cache_entry_is_fresh({"epoch": 2}, current_epoch=2)
    assert not cache_entry_is_fresh({"epoch": 3}, current_epoch=2)


def test_tenant_context_validation():
    assert TenantContext().is_single_operator
    assert not TenantContext("acme​me").is_valid       # zero-width inside: Unicode hazard
    assert not TenantContext("acme", -1).is_valid
    assert TenantContext("acme", 2).is_valid


def test_canonical_decision_roundtrips_dict_and_object():
    dec = AccessDecision(True, "allow", row_filter=None, masked_columns=["a"], rules=[])
    assert canonical_decision(dec) == canonical_decision(dec.to_dict())
