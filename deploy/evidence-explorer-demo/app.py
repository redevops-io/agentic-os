"""Evidence Explorer - AUTHENTICATED profile (the normal deployment).

Fail-closed by construction: importing this module REFUSES to start unless an owner token is configured
(``EVIDENCE_EXPLORER_TOKEN``, at least 24 characters). There is no setting here that turns authentication off; the public
synthetic demo is a different entrypoint (``public_app.py``) and a different image target.

Every query is authorized through the permission plane (grant ``evidence.<scope>.inspect``), executed in a sandboxed
read-only worker over only that scope's data, and audited (who, scope, statement hash, rows, outcome) to a hash-chained
JSONL file (``EVIDENCE_AUDIT_PATH``)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explorer_core as core  # noqa: E402
from agentic_os.evidence_query import (  # noqa: E402
    EvidenceQueryService, GrantsCeiling, JsonlAuditSink, Limits, SingleOwnerTokenAuthenticator)
from agentic_os.permissions import GrantStore, PermissionsPlane  # noqa: E402


class ProfileConfigError(RuntimeError):
    """The authenticated profile was started without what it needs; it will not start in a weaker mode."""


def create_app(env=None):
    env = os.environ if env is None else env
    token = (env.get("EVIDENCE_EXPLORER_TOKEN") or "").strip()
    try:
        authenticator = SingleOwnerTokenAuthenticator(token, principal_id="owner")
    except ValueError as e:
        raise ProfileConfigError(
            "EVIDENCE_EXPLORER_TOKEN is required (>= 24 characters). The authenticated profile does not start without "
            "authentication; for the public synthetic demo use public_app.py (a separate deployment profile).") from e
    store_path = env.get("EVIDENCE_PATH") or core.STORE_PATH
    store = core.build_corpus(store_path)
    core.set_summary(store)
    catalog = core.build_catalog(store_path)
    # the owner's grants live in the ordinary permission plane (encrypted grant store); Enterprise replaces the ceiling
    perm_dir = env.get("EVIDENCE_PERMISSIONS_DIR") or tempfile.mkdtemp(prefix="aos-evidence-perm-")
    grants = GrantStore(perm_dir)
    for scope in catalog.scope_ids():
        grants.add("user", "owner", "tool", f"evidence.{scope}.inspect", actions=["read"], created_by="bootstrap")
    audit_path = env.get("EVIDENCE_AUDIT_PATH") or os.path.join(tempfile.gettempdir(), "evidence-explorer-audit.jsonl")
    service = EvidenceQueryService(catalog, GrantsCeiling(PermissionsPlane(grants)), JsonlAuditSink(audit_path),
                                   limits=Limits(timeout_s=10, memory_mb=256, max_rows=500))
    return core.make_app(profile="authenticated", service=service, store_path=store_path, authenticator=authenticator,
                         audit_path=audit_path)


app = create_app()
