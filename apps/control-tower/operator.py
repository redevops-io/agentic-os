"""agentic-control-tower as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive Control Tower as a capability operator — the same core
Metabase analytics the console exposes at `/agent/run`, now discoverable + idempotent on
the wire.

Capabilities (syscalls):
  bi.summary   — read-only KPI scorecards + trend + breakdown (fact `bi_activity`)
  bi.ask       — natural-language question -> pre-written SQL template -> live query result
  bi.refresh   — re-run the dashboard queries (bust the cache) and report fresh KPIs

Control Tower is read-only analytics — nothing moves money or mutates the core — so per
modules.yaml there is NO approval gate (`approval_required: []`) on any capability, and none
are side-effecting: a query only reads. (Persisting a card/dashboard would be the side-effecting
exception, but Control Tower does not persist.)
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_control_tower_operator() -> Operator:
    return Operator("control-tower", [
        capability(
            "bi.summary",
            lambda inp: core.fetch_activity(),
            provides=["bi_activity"],
            outputs={"bi_activity": "KPI scorecards + revenue trend + service breakdown from the live Metabase core"},
            permissions=["bi:read"], estimated_value="low", deterministic=False, latency_ms=400,
            concurrency_mode="read_only",   # read-only analytics — never blocks another query
        ),
        capability(
            "bi.ask",
            lambda inp: core.ask(inp.get("question", "")),
            provides=["bi_answer"],
            outputs={"bi_answer": "answer + rows + viz spec from a pre-written SQL template (never model-authored SQL)"},
            permissions=["bi:read"], estimated_value="medium", deterministic=False, latency_ms=800,
            concurrency_mode="read_only",
        ),
        capability(
            "bi.refresh",
            lambda inp: core.refresh(),
            provides=["bi_activity"],
            outputs={"bi_activity": "dashboard queries re-run against Metabase (cache busted)"},
            permissions=["bi:read"], estimated_value="low", deterministic=False, latency_ms=1200,
            concurrency_mode="read_only",   # cache-bust re-query; no core mutation
        ),
    ])
