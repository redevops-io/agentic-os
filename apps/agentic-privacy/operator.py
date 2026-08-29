"""agentic-privacy as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive DSAR fulfilment as a capability operator — the same core fan-out
actions the console exposes at `/request`, now discoverable + idempotent on the wire.

Capabilities (syscalls) — one per modules.yaml agent (intake · access · delete · retention):
  privacy.intake      — open a DSAR: verification email + audited request record
  privacy.access      — gather the subject's data across every live system (read-only fan-out)
  privacy.delete      — the cascading erasure across every system   [approval gate: delete]
  privacy.retention   — policy check: PII past the retention window (dry-run report)

Per modules.yaml the gate is `[delete]`: erasure moves personal data OUT permanently and is
irreversible, so `privacy.delete` is approval_required=True + side_effecting=True — the runtime
parks it as a HumanTask before executing the confirmed erasure. There is no `undo` (an erasure
cannot be reversed); the dry-run preview (core.delete(confirm=False)) is the safe rehearsal.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_privacy_operator() -> Operator:
    return Operator("agentic-privacy", [
        capability(
            "privacy.intake",
            lambda inp: core.intake(inp.get("email", ""), inp.get("type", "access"),
                                    inp.get("base_url", "")),
            provides=["dsar_opened"],
            outputs={"dsar_opened": "DSAR recorded + identity-verification email sent"},
            side_effecting=True, permissions=["privacy:write"],
            estimated_value="medium", deterministic=False, latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="privacy:subject:{email}",
        ),
        capability(
            "privacy.access",
            lambda inp: core.access(inp.get("email", "")),
            provides=["subject_data"],
            outputs={"subject_data": "personal data gathered across every live system"},
            permissions=["privacy:read"], estimated_value="medium",
            deterministic=False, latency_ms=1500, concurrency_mode="read_only",
        ),
        capability(
            "privacy.delete",
            lambda inp: core.delete(inp.get("email", ""), confirm=True),
            provides=["erasure"],
            outputs={"erasure": "subject erased across every live connector (confirmed)"},
            side_effecting=True, approval_required=True,
            permissions=["privacy:write", "privacy:erase"],
            estimated_value="high", deterministic=False, latency_ms=2500,
            concurrency_mode="exclusive", concurrency_key="privacy:subject:{email}",
        ),
        capability(
            "privacy.retention",
            lambda inp: core.retention_scan(confirm=False),
            provides=["retention_report"],
            outputs={"retention_report": "PII past the retention window (dry-run policy check)"},
            permissions=["privacy:read"], estimated_value="low",
            deterministic=False, latency_ms=1000, concurrency_mode="read_only",
        ),
    ])
