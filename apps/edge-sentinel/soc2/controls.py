"""The control catalog: what is tested, against which criterion, with which procedure and which evidence.

Criterion references use the AICPA Trust Services Criteria identifiers (2017, points of focus revised 2022). The text of the criteria is not reproduced here
(it is licensed content); the references are a STARTING HYPOTHESIS that the control owner and the auditor must confirm. A control that maps to a criterion
supports it; it does not satisfy it. `manual_dependency` names what a person must still attest: a control with one is never an automatic PASS.

Procedures are named, not embedded: `assess.PROCEDURES` holds the code. A control with no procedure is evidence-gathering only and ends MANUAL_REVIEW or
NOT_TESTED, never PASS.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlDef:
    control_id: str
    version: str
    title: str
    criterion_refs: tuple
    owner_role: str                      # the role accountable for the control (a person's identity is bound at assessment time)
    frequency_days: int                  # how often the control operates / is tested
    procedure: str                       # key into assess.PROCEDURES ("" = none: manual only)
    evidence_kinds: tuple                # kinds of evidence the procedure reads
    min_coverage: float = 0.95           # fraction of the period the evidence must cover (for coverage-based procedures)
    manual_dependency: str = ""          # what a person must attest for the control to be satisfied
    scope_note: str = ""                 # what the control applies to and what it does not
    empty_ok: bool = False               # an empty population is normal (no leavers, no releases): complete AND empty is then a PASS, otherwise it is INCONCLUSIVE


CATALOG: tuple = (
    ControlDef("SEC-ACC-01", "2", "Privileged accounts require multi-factor authentication", ("CC6.1",), "security-owner", 30, "mfa_coverage",
               ("access_review", "mfa_status"), scope_note="privileged accounts of the in-scope systems; `mfa_status` is an account list with no review or leaver data"),
    ControlDef("SEC-ACC-02", "1", "Privileged access is reviewed each period by someone other than the account owner", ("CC6.2", "CC6.3"), "security-owner", 90,
               "access_review", ("access_review",), scope_note="privileged accounts; the review must be inside the period"),
    ControlDef("SEC-ACC-03", "1", "Access is removed promptly when a person leaves", ("CC6.2", "CC6.3"), "security-owner", 30, "termination_revocation",
               ("access_review",), scope_note="accounts terminated in the period; revocation within one day", empty_ok=True),
    ControlDef("SEC-CHG-01", "1", "Production changes are independently approved and pass the required checks", ("CC8.1",), "engineering-owner", 30,
               "change_management", ("change_records",), scope_note="every production release in the period", empty_ok=True),
    ControlDef("SEC-MON-01", "1", "Monitoring sources report continuously", ("CC7.2",), "security-owner", 7, "monitoring_coverage", ("heartbeat",),
               min_coverage=0.99, scope_note="coverage of the period by monitoring heartbeats"),
    ControlDef("SEC-CFG-01", "1", "Hosts are scanned against a hardening baseline with no open critical finding", ("CC7.1",), "security-owner", 30,
               "host_baseline_scan", ("scanner_result",), scope_note="a HOST scan; a container-image scan does not satisfy it"),
    ControlDef("AVL-BCK-01", "1", "Backups succeed and a restore is tested within the period", ("A1.2", "A1.3"), "platform-owner", 90, "backup_restore",
               ("backup_restore",), scope_note="a backup log alone is not enough: a successful restore test is required"),
    ControlDef("PIN-REC-01", "1", "Unknown outcomes of side-effecting actions are reconciled within the threshold", ("PI1.3", "PI1.4"), "platform-owner", 7,
               "reconciliation_open", ("reconciliation",), scope_note="mission and gateway actions whose outcome was unknown", empty_ok=True),
    ControlDef("SEC-INC-01", "1", "Security events are evaluated and escalated", ("CC7.3", "CC7.4"), "security-owner", 90, "", ("attestation",),
               manual_dependency="the incident register and an exercise record, reviewed by someone other than the owner"),
    ControlDef("CNF-SEC-01", "1", "Secrets are scoped, stored in a broker and rotated", ("C1.1",), "platform-owner", 90, "", ("attestation",),
               manual_dependency="the rotation records and the broker's access policy, reviewed by someone other than the owner"),
    ControlDef("PRV-DSR-01", "1", "Data-subject requests are received, verified and completed", ("P5.1", "P5.2"), "privacy-owner", 90, "", ("attestation",),
               manual_dependency="the request log sampled and checked by someone other than the owner"),
)


def by_id(control_id: str) -> "ControlDef | None":
    return next((c for c in CATALOG if c.control_id == control_id), None)
