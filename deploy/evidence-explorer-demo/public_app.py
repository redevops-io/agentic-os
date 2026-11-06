"""Evidence Explorer - PUBLIC SYNTHETIC profile. A SEPARATE deployment profile (own entrypoint, own image target), never
a switch on the authenticated one.

What it is: anonymous, read-only SQL over the one built-in synthetic corpus, executed in the sandboxed worker (empty
environment, no network at the OS level where the host provides a sandbox, no extension loading, locked DuckDB
configuration, no file access), with per-client and global rate limits, a time/memory/row cap and an audit trail.
What it is not: it has no token, cannot name any other scope, and has no replay, resume, approval or operational route
(the routes do not exist - ``test_public_profile_route_table``). It must be deployed with no host mounts, no Docker socket
and no credentials in its environment (see the Dockerfile ``public-synthetic`` target and docs/EVIDENCE_REPLAY_BOUNDARY.md).

OWNER DECISION: enabling this profile on a public host is the owner's call; nothing starts it implicitly."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explorer_core as core  # noqa: E402
from agentic_os.evidence_query import (  # noqa: E402
    EvidenceQueryService, JsonlAuditSink, Limits, RateLimiter, StaticCeiling)


def create_app(env=None):
    env = os.environ if env is None else env
    store_path = env.get("EVIDENCE_PATH") or core.STORE_PATH
    store = core.build_corpus(store_path)
    core.set_summary(store)
    catalog = core.build_catalog(store_path, tenant_scopes=False)     # the ONE allowlisted synthetic dataset
    assert catalog.scope_ids() == [core.SYNTHETIC_SCOPE]
    audit_path = env.get("EVIDENCE_AUDIT_PATH") or os.path.join(tempfile.gettempdir(), "evidence-explorer-public-audit.jsonl")
    service = EvidenceQueryService(
        catalog, StaticCeiling({f"evidence.{core.SYNTHETIC_SCOPE}.inspect"}), JsonlAuditSink(audit_path),
        limits=Limits(timeout_s=5, memory_mb=128, max_rows=200, max_result_bytes=500_000, max_sql_chars=2000),
        os_sandbox="required" if env.get("EVIDENCE_REQUIRE_OS_SANDBOX") == "1" else "auto")
    limiter = RateLimiter(per_key_per_min=int(env.get("EVIDENCE_RATE_PER_MIN", "20")), global_per_min=int(env.get("EVIDENCE_RATE_GLOBAL_PER_MIN", "120")))
    return core.make_app(profile="public-synthetic", service=service, store_path=store_path, limiter=limiter, audit_path=audit_path)


app = create_app()
