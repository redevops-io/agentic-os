"""Phase 3 — governed write capabilities (plan §3, §5).

Writes follow the ``prepare_* / request_*`` shape (plan §5): a ``prepare_*`` capability does the
non-side-effecting work an agent needs to compose an action (draft, look up, plan) and returns
something inspectable; a ``request_*`` capability is the governed side effect — risk-tiered,
approval-gated, idempotent, and executed only under a :class:`GovernedEnvelope`. Unrestricted
mutation primitives are deliberately not exposed.

The pipeline (Phase 0) already enforces the guarantees; these are just the manifests + handlers
over injected write-port seams. Acceptance (plan §3): risk tiers work · approvals gate before any
effect · retries are idempotent · a rejected/ungated request produces no side effect · audit
carries the evidence + policy provenance the handler and decision return.
"""
from __future__ import annotations

from typing import Optional, Protocol

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
from .registry import CapabilityRegistry, HandlerResult


# ── write-port seams (adapt real connectors/apps to these in wiring) ───────────────
class CrmWritePort(Protocol):
    def prepare_outreach(self, arguments: dict) -> dict: ...
    def send_outreach(self, arguments: dict, envelope: object) -> dict: ...


class ProjectsWritePort(Protocol):
    def request_change(self, arguments: dict, envelope: object) -> dict: ...


def _obj(**props: str) -> dict:
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()},
            "required": list(props)}


# ── manifests ────────────────────────────────────────────────────────────────────
#: prepare_* — non-side-effecting: draft outreach for a contact. Read-tier, auto, no envelope.
CRM_PREPARE_OUTREACH = CapabilityManifest(
    "crm.prepare_outreach",
    "Draft outreach for a contact (research + compose). Does NOT send anything.",
    CapabilityKind.DIRECT, permissions=("crm.read",), risk_tier=RiskTier.READ,
    input_schema=_obj(contact="string", goal="string"),
    output_schema=_obj(draft="string", prepared_ref="string"),
    data_classes=(DataClass.CUSTOMER_CONTENT,))

#: request_* — the governed side effect: send prepared outreach. Consequential ⇒ approval by
#: default; idempotent so a retry with the same key never double-sends; runs under an envelope.
CRM_REQUEST_OUTREACH_SEND = CapabilityManifest(
    "crm.request_outreach_send",
    "Send outreach to a contact. Consequential — requires approval; pass idempotency_key to retry safely.",
    CapabilityKind.DIRECT, permissions=("crm.write",), risk_tier=RiskTier.CONSEQUENTIAL,
    side_effecting=True, idempotent=True, provider="crm",
    input_schema={"type": "object",
                  "properties": {"contact": {"type": "string"}, "body": {"type": "string"},
                                 "prepared_ref": {"type": "string"},
                                 "idempotency_key": {"type": "string"}},
                  "required": ["contact"]},
    output_schema=_obj(sent="boolean", provider_object_id="string"),
    data_classes=(DataClass.CUSTOMER_CONTENT,))

PROJECTS_REQUEST_CHANGE = CapabilityManifest(
    "projects.request_change",
    "Request a change to a project (governed). Consequential — requires approval.",
    CapabilityKind.DIRECT, permissions=("projects.write",), risk_tier=RiskTier.CONSEQUENTIAL,
    side_effecting=True, idempotent=True, provider="projects",
    input_schema=_obj(project_id="string", change="string"),
    data_classes=(DataClass.INTERNAL,))


def _guard(fn) -> HandlerResult:
    """Run a port call, turning any fault into a clean handler error (never a gateway crash)."""
    try:
        out = fn()
    except Exception as exc:
        return HandlerResult(ok=False, error=str(exc))
    evidence = ()
    if isinstance(out, dict) and out.get("provider_object_id"):
        evidence = (str(out["provider_object_id"]),)      # provenance for the audit record
    return HandlerResult(ok=True, output=out, evidence_refs=evidence)


def build_write_registry(*, crm: Optional[CrmWritePort] = None,
                         projects: Optional[ProjectsWritePort] = None,
                         registry: Optional[CapabilityRegistry] = None) -> CapabilityRegistry:
    """Register the write capabilities whose backend is provided, onto a new or given registry."""
    reg = registry or CapabilityRegistry()
    if crm is not None:
        reg.register(CRM_PREPARE_OUTREACH,
                     lambda req, env: _guard(lambda: crm.prepare_outreach(dict(req.arguments))))
        reg.register(CRM_REQUEST_OUTREACH_SEND,
                     lambda req, env: _guard(lambda: crm.send_outreach(dict(req.arguments), env)))
    if projects is not None:
        reg.register(PROJECTS_REQUEST_CHANGE,
                     lambda req, env: _guard(lambda: projects.request_change(dict(req.arguments), env)))
    return reg


WRITE_CAPABILITIES = (CRM_PREPARE_OUTREACH, CRM_REQUEST_OUTREACH_SEND, PROJECTS_REQUEST_CHANGE)
