"""agentic-guide as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive the onboarding guide as a capability operator — the same
redevops-rag retrieval the console exposes at /api/ask + /api/walkthrough, now
discoverable + idempotent on the wire.

Capabilities (syscalls):
  guide.retrieve     — RBAC-scoped Q&A over the app corpus (fact `guide_answer`)
  guide.walkthrough  — structured per-app onboarding walkthrough (fact `guide_walkthrough`)

Both are read-only retrieval — no side effects, no approval gate (matches modules.yaml,
where `guide` declares `approval_required: []`), so the runtime can run them freely.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_guide_operator() -> Operator:
    return Operator("guide", [
        capability(
            "guide.retrieve",
            lambda inp: core.answer(inp.get("question", ""), inp.get("role", "admin")),
            provides=["guide_answer"],
            outputs={"guide_answer": "RBAC-scoped answer + cited apps from the redevops-rag corpus"},
            estimated_value="medium", deterministic=True, latency_ms=200,
            concurrency_mode="read_only",   # retrieval only — never blocks
        ),
        capability(
            "guide.walkthrough",
            lambda inp: core.walkthrough(
                inp.get("app") or inp.get("name") or next(iter(core.APP_DOCS))
            ),
            provides=["guide_walkthrough"],
            outputs={"guide_walkthrough": "structured per-app onboarding walkthrough"},
            estimated_value="medium", deterministic=True, latency_ms=100,
            concurrency_mode="read_only",
        ),
    ])
