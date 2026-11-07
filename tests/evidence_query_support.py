"""Shared fixtures for the evidence-query boundary tests (synthetic data only)."""
from __future__ import annotations

import hashlib
import json
import os

from agentic_os.evidence_query import (
    ArtifactFile, ArtifactRoot, DatasetSpec, GrantsCeiling, Limits, MemoryAuditSink, ScopeCatalog,
    EvidenceQueryService, ViewSpec)
from agentic_os.overlays import Principal
from agentic_os.permissions import GrantStore, PermissionsPlane

EVENT_COLS = (("mission_id", "VARCHAR"), ("tenant", "VARCHAR"), ("capability", "VARCHAR"),
              ("verdict", "VARCHAR"), ("amount", "DOUBLE"), ("payload", "JSON"))

ALPHA_ROWS = [
    {"mission_id": "m-a1", "tenant": "alpha", "capability": "crm.read", "verdict": "ALLOW", "amount": 10.0, "payload": '{"k": [1, 2]}'},
    {"mission_id": "m-a1", "tenant": "alpha", "capability": "storage.upload", "verdict": "DENY", "amount": 20.0, "payload": '{"k": [3]}'},
    {"mission_id": "m-a2", "tenant": "alpha", "capability": "crm.read", "verdict": "ALLOW", "amount": 30.0, "payload": '{"k": []}'},
]
BETA_ROWS = [
    {"mission_id": "m-b1", "tenant": "beta", "capability": "vault.read", "verdict": "DENY", "amount": 99.0, "payload": '{"secret": "BETA-ONLY-MARKER"}'},
    {"mission_id": "m-b2", "tenant": "beta", "capability": "email.send", "verdict": "ALLOW", "amount": 5.0, "payload": '{}'},
]
CANARY = "HOST-CANARY-9f3c1d7e-do-not-leak"


def _authn(p: Principal):
    """The principal as the authentication layer issues it after checking a credential (resume grants require this)."""
    from agentic_os.mission.principal import default_issuer
    return default_issuer().issue(p, method="test-login")


def owner() -> Principal:
    return _authn(Principal("owner", "user", ("operator",)))


def stranger() -> Principal:
    return _authn(Principal("stranger", "user", ()))


def make_ceiling(tmp_path, grants):
    """grants: iterable of (user, resource_name). Real permission plane (encrypted grant store in tmp)."""
    store = GrantStore(str(tmp_path / "perm"))
    for user, name in grants:
        store.add("user", user, "tool", name, actions=["read"], created_by="test")
    return GrantsCeiling(PermissionsPlane(store))


def make_catalog():
    cat = ScopeCatalog()
    cat.register_scope("alpha", [DatasetSpec("events", EVENT_COLS, rows=ALPHA_ROWS)],
                       views=[ViewSpec("denials", "SELECT mission_id, capability FROM events WHERE verdict = 'DENY'")])
    cat.register_scope("beta", [DatasetSpec("events", EVENT_COLS, rows=BETA_ROWS),
                                DatasetSpec("beta_only", (("x", "VARCHAR"),), rows=[{"x": "BETA-TABLE-ROW"}])])
    return cat


def make_service(tmp_path, grants=(("owner", "evidence.alpha.inspect"),), limits=None, catalog=None, **kw):
    sink = MemoryAuditSink()
    svc = EvidenceQueryService(catalog or make_catalog(), make_ceiling(tmp_path, grants), sink,
                               limits=limits or Limits(timeout_s=20), **kw)
    return svc, sink


def sha256_file(path) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def tree_state(root) -> dict:
    """Byte-level + inode/mtime snapshot of a directory tree (artifact integrity assertions)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in sorted(dns + fns):
            p = os.path.join(dp, n)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode,
                                             sha256_file(p) if os.path.isfile(p) and not os.path.islink(p) else None)
    return out


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
