"""Phase 9 — the live "Inspect current ReDevOps demo deployments" mission (plan §12, §34).

This is what turns the demo from illustrative into real: it reads the LIVE Edge Sentinel
(sentinel.redevops.io) **read-only** and derives findings from actual deployment state — CrowdSec
threats, backup posture, network-policy risks — then composes the governed chain
(finding → approval-bound Decision → operator → receipt → verification → projection).

Two safety rules (hard guardrail — never disturb the live demos):
  * every read is GET-only against Edge Sentinel's read endpoints; nothing here mutates the SOC;
  * the governed remediation is a bounded PROPOSAL executed through a **fake** external-agent adapter,
    so the full receipt/verification path is demonstrated without touching live infrastructure. A real
    remediation would swap the adapter for a governed operator and still require a human Decision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentic_os.overlays import Principal

from .approval_bridge import ExternalApprovalBridge
from .contracts import AgentIdentity, AgentPermissionScope, AgentTaskRequest
from .fake_adapter import FakeExternalAgentAdapter
from .operator import ExternalAgentOperator
from .projections import external_action_view

SENTINEL_READ_ENDPOINTS: Tuple[str, ...] = (
    "/health", "/api/activity", "/api/backups/status", "/api/network/review")

_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


@dataclass(frozen=True)
class DeploymentFinding:
    source: str          # which read endpoint / plane produced it
    kind: str
    severity: str
    detail: str
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_ref:
            h = hashlib.sha256(f"{self.source}|{self.kind}|{self.detail}".encode()).hexdigest()[:16]
            object.__setattr__(self, "evidence_ref", f"dpev:{h}")

    def view(self) -> dict:
        return {"source": self.source, "kind": self.kind, "severity": self.severity,
                "detail": self.detail, "evidence_ref": self.evidence_ref}


@dataclass
class InspectionReport:
    target: str
    connected: bool
    kpis: List[dict] = field(default_factory=list)
    findings: List[DeploymentFinding] = field(default_factory=list)
    proposed_actions: List[str] = field(default_factory=list)

    @property
    def top_finding(self) -> Optional[DeploymentFinding]:
        if not self.findings:
            return None
        return sorted(self.findings, key=lambda f: -_SEVERITY_RANK.get(f.severity, 0))[0]

    def view(self) -> dict:
        return {"target": self.target, "connected": self.connected, "kpis": self.kpis,
                "findings": [f.view() for f in self.findings],
                "proposed_actions": self.proposed_actions}


# ── reading the live deployment (GET-only) ───────────────────────────────────────────
def _httpx_fetch(base_url: str, path: str, timeout: float = 8.0) -> Optional[dict]:
    import httpx  # noqa: PLC0415
    try:
        r = httpx.get(base_url.rstrip("/") + path, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def read_deployment(base_url: str, *, fetch: Optional[Callable[[str, str], Optional[dict]]] = None) -> Dict[str, Any]:
    """Fetch each read-only endpoint best-effort. ``fetch(base_url, path) -> dict|None`` is injectable
    (fixtures in tests, httpx live)."""
    f = fetch or _httpx_fetch
    return {path: f(base_url, path) for path in SENTINEL_READ_ENDPOINTS}


# ── deriving findings from real state ────────────────────────────────────────────────
def derive_findings(snapshot: Dict[str, Any]) -> List[DeploymentFinding]:
    findings: List[DeploymentFinding] = []

    health = snapshot.get("/health") or {}
    if health and not health.get("connected", True):
        findings.append(DeploymentFinding("health", "core_disconnected", "high",
                                          f"security core '{health.get('core', '?')}' is not connected"))

    net = snapshot.get("/api/network/review") or {}
    for risk in net.get("risks", []) or []:
        findings.append(DeploymentFinding("network", risk.get("kind", "network_risk"),
                                          risk.get("severity", "medium"), risk.get("detail", "")))

    bk = snapshot.get("/api/backups/status") or {}
    for name in bk.get("never", []) or []:
        findings.append(DeploymentFinding("backups", "no_backup", "high", f"{name} has never been backed up"))
    for name in bk.get("no_offsite", []) or []:
        findings.append(DeploymentFinding("backups", "no_offsite", "medium", f"{name} has no offsite copy"))
    for name in bk.get("unencrypted", []) or []:
        findings.append(DeploymentFinding("backups", "unencrypted_backup", "high", f"{name} backup is unencrypted"))

    act = snapshot.get("/api/activity") or {}
    if act.get("has_threat"):
        n = len(act.get("decisions", []) or [])
        findings.append(DeploymentFinding("crowdsec", "active_threats", "medium",
                                          f"{n} active CrowdSec decision(s) enforced against live sources"))
    return findings


def run_inspection(base_url: str, *, fetch: Optional[Callable[[str, str], Optional[dict]]] = None) -> InspectionReport:
    snap = read_deployment(base_url, fetch=fetch)
    health = snap.get("/health") or {}
    act = snap.get("/api/activity") or {}
    findings = derive_findings(snap)
    proposed = []
    if findings:
        proposed = ["Open remediation ticket for the top finding", "Track deployment posture",
                    "Draft an operator briefing"]
    return InspectionReport(
        target=base_url, connected=bool(health.get("connected")),
        kpis=act.get("kpis", []) or [], findings=findings, proposed_actions=proposed)


# ── the governed remediation proposal (SAFE — fake-executed) ─────────────────────────
def build_remediation_request(finding: DeploymentFinding, *, principal: Principal,
                              project_id: str, mission_id: str,
                              provider: str = "fake-external-agent") -> AgentTaskRequest:
    """A bounded, approval-required remediation for a finding — 'open a remediation ticket'. Carries the
    finding's evidence ref and nothing provider-private."""
    identity = AgentIdentity(provider=provider, adapter_version="v1", instance_id="demo",
                             principal=principal)
    return AgentTaskRequest(
        identity=identity, capability="personal_agent.connected_app_action",
        goal=f"open a remediation ticket: {finding.kind} — {finding.detail}",
        inputs={"_outcome": "succeed", "finding_kind": finding.kind},
        bounded_context={"finding_evidence": finding.evidence_ref},
        permission_scope=AgentPermissionScope(ask_before_capabilities=("personal_agent.connected_app_action",)),
        project_id=project_id, mission_id=mission_id, idempotency_key=f"remediate-{finding.evidence_ref}")


def demo_inspection_mission(base_url: str = "https://sentinel.redevops.io", *,
                            fetch: Optional[Callable[[str, str], Optional[dict]]] = None,
                            project_id: str = "proj:redevops-demo",
                            mission_id: str = "mission:inspect-deployments") -> dict:
    """End-to-end demo: inspect the LIVE deployment (read-only) → govern a remediation of the top finding
    → project it for Projects/Sidekick. The remediation runs through a fake adapter (never touches live
    infra) but exercises the real approval → Decision → operator → receipt → verification path."""
    report = run_inspection(base_url, fetch=fetch)
    result: dict = {"inspection": report.view(), "governed_action": None}

    finding = report.top_finding
    if finding is None:
        return result

    principal = Principal(id="user:operator", kind="user", roles=("operator",), tenant="redevops")
    request = build_remediation_request(finding, principal=principal, project_id=project_id,
                                        mission_id=mission_id)

    bridge = ExternalApprovalBridge()
    operator = ExternalAgentOperator(FakeExternalAgentAdapter())
    approval_id = bridge.present(request)
    # In the live demo a human approves in Projects/Sidekick; here we approve to show the full path.
    decision = bridge.approve(approval_id, principal=principal, by="operator", attestation="sso-session")
    authz = bridge.authorize(request)
    out = operator.operator.invoke(request.capability,
                                   {"_outcome": "succeed", "decision_id": authz.decision_id,
                                    "mission_id": mission_id}, request.idempotency_key)
    result["governed_action"] = external_action_view(
        request, decision_id=decision.decision_id, receipt=out["receipt"],
        verification=out.get("verification", ""), task_state=out.get("state", ""))
    return result
