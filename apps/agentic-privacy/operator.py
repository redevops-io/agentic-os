"""agentic-privacy as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive DSAR fulfilment as a capability operator — the same core fan-out
actions the console exposes at `/request`, now discoverable + idempotent on the wire.

Capabilities (syscalls) — one per modules.yaml agent (intake · access · delete · retention):
  privacy.intake      — open a DSAR: verification email + audited request record
  privacy.access      — deliver the export of a VERIFIED access request (counts only in the result)
  privacy.delete      — erasure of a VERIFIED request id [approval gate]; verify-after-act (erasure_verified, residual)
  privacy.retention   — policy check: PII past the retention window (dry-run report)

Per modules.yaml the gate is `[delete]`: erasure moves personal data OUT permanently and is
irreversible, so `privacy.delete` is approval_required=True + side_effecting=True — the runtime
parks it as a HumanTask before executing the confirmed erasure. There is no `undo` (an erasure
cannot be reversed); the dry-run preview (core.delete(confirm=False)) is the safe rehearsal.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability, pending_approval

from . import core


def _delete(inp: dict) -> dict:
    """privacy.delete — executes ONLY with a runtime-delivered `_approval` AND a request id that is verified (the
    emailed token was redeemed) and bound to this email. Anything else STAGES (nothing is erased)."""
    rid, email = inp.get("request_id", ""), inp.get("email", "")
    staged = lambda reason=None: pending_approval(  # noqa: E731
        "privacy.delete", request_id=rid, email=email, erasure=False, erasure_verified=False,
        residual=None, **({"reason": reason} if reason else {}))
    if not inp.get("_approval"):
        return staged()
    res = core.execute_erasure(rid, email, actor="runtime-approval")
    if res.get("status") == "refused":
        return staged(res.get("reason"))
    # belt-and-braces for the kernel verifier: a partial erasure can never carry a truthy erasure_verified
    res["erasure_verified"] = bool(res.get("erasure_verified")) and res.get("residual") == 0
    return res


def build_privacy_operator() -> Operator:
    return Operator("agentic-privacy", [
        capability(
            "privacy.intake",
            lambda inp: core.intake(inp.get("email", ""), inp.get("type", "access"),
                                    inp.get("base_url", "")),
            provides=["dsar_opened"],
            outputs={"dsar_opened": "DSAR recorded as PENDING; identity-verification token emailed (not returned)"},
            side_effecting=True, permissions=["privacy:write"],
            estimated_value="medium", deterministic=False, latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="privacy:subject:{email}",
        ),
        capability(
            "privacy.access",
            lambda inp: _access(inp),
            provides=["subject_data"],
            outputs={"subject_data": "export delivered to the verified subject (counts only here; no records)"},
            inputs={"request_id": "a VERIFIED access request id", "email": "must match the request"},
            side_effecting=True, permissions=["privacy:read"], estimated_value="medium",
            deterministic=False, latency_ms=1500,
            concurrency_mode="exclusive", concurrency_key="privacy:subject:{email}",
        ),
        capability(
            "privacy.delete",
            _delete,
            provides=["erasure"],
            inputs={"request_id": "a VERIFIED delete request id", "email": "must match the request"},
            outputs={"erasure_verified": "True only if the post-erasure connector re-find found zero residual",
                     "residual": "records still present across connectors after erasure (0 == verified)"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "erasure is not reversible"}, approval_required=True,
            permissions=["privacy:write", "privacy:erase"],
            estimated_value="high", deterministic=False, latency_ms=2500,
            concurrency_mode="exclusive", concurrency_key="privacy:subject:{email}",
        ),
        capability(
            "privacy.retention",
            lambda inp: core.retention_scan(confirm=False),
            provides=["retention_report"],
            outputs={"retention_report": "PII past the retention window (dry-run, counts only)"},
            permissions=["privacy:read"], estimated_value="low",
            deterministic=False, latency_ms=1000, concurrency_mode="read_only",
        ),
    ])


def _access(inp: dict) -> dict:
    res = core.access(inp.get("request_id", ""), inp.get("email", ""))
    if res.get("status") == "refused":
        return pending_approval("privacy.access", reason=res.get("reason"), subject_data=False)
    return {**res, "subject_data": {"delivery": res.get("delivery"), "record_count": res.get("record_count")}}
