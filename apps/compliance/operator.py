"""agentic-compliance as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive compliance as a capability operator — the same core
OpenSCAP actions the console exposes at `/agent/run`, now discoverable + idempotent on
the wire.

Capabilities (syscalls):
  compliance.scan          — re-run the oscap scan, return updated pass rate (read-only)
  compliance.explain       — plain-English explanation + fix for a failing rule (read-only)
  compliance.remediate     — stage a host fix for approval   [approval gate: policy_change]
  compliance.file_consent  — file the customer's consent record  [approval gate: onboarding]

Applying a system fix changes the host, so compliance.remediate carries
approval_required=True (matches modules.yaml policy_change gate); the runtime parks it as
a HumanTask before execution. compliance.file_consent is the onboarding mission's human
gate — filing a consent/GDPR record is side-effecting audit evidence that must be
human-reviewed, so it too carries approval_required=True.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_compliance_operator() -> Operator:
    return Operator("compliance", [
        capability(
            "compliance.scan",
            lambda inp: core.scan(),
            provides=["compliance_scan"],
            outputs={"compliance_scan": "updated pass rate + pass/fail counts from a fresh oscap scan"},
            estimated_value="medium", deterministic=False, latency_ms=2000,
            concurrency_mode="read_only",
        ),
        capability(
            "compliance.explain",
            lambda inp: core.explain(inp),
            provides=["control_explanation"],
            outputs={"control_explanation": "plain-English explanation + remediation for a failing CIS rule"},
            estimated_value="low", latency_ms=300,
            concurrency_mode="read_only",
        ),
        capability(
            "compliance.remediate",
            lambda inp: core.remediate(inp),
            provides=["remediation_staged"],
            outputs={"remediation_staged": "host fix staged for human approval (policy_change)"},
            side_effecting=True, approval_required=True,
            permissions=["compliance:write"], estimated_value="high", latency_ms=500,
            concurrency_mode="exclusive", resource_keys=["compliance:host-config"],  # one host fix at a time
        ),
        capability(
            "compliance.file_consent",
            lambda inp: core.file_consent(inp),
            provides=["consent_filed"],
            outputs={"consent_filed": "customer consent/GDPR record appended to the append-only evidence ledger"},
            side_effecting=True, approval_required=True,
            permissions=["compliance:write"], estimated_value="high", latency_ms=200,
            concurrency_mode="exclusive", resource_keys=["compliance:evidence-ledger"],  # serialize appends (commutative merge is future work)
        ),
    ])
