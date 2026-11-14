"""Edge Sentinel adapter: the product's own ``/api/v1`` as typed calls, and the pure mapping from its records to what
Projects shows (mission rows, attention items, approval cards).

Nothing here stores anything. Every function that returns a record returns the product's JSON as it came; the mapping
helpers only derive display fields and ALWAYS keep the product's own ids (``case-…``, ``act-…``, ``rcpt-…``) so the
standalone Edge Sentinel UI and Projects show the same records. Nothing in Projects can mark an action verified: the
``verification`` field is rendered exactly as the product persisted it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .client import ProductClient, ProductError
from .registry import ProductSpec

OPERATOR = "edge-sentinel"
CAP_DECIDE = "sentinel.decide"
CAP_EXECUTE = "sentinel.execute"
CAP_BAN = "sentinel.block_ip"
CAP_UNBAN = "sentinel.unblock_ip"

OPEN_STATES = ("AWAITING_APPROVAL", "APPROVED")
TERMINAL_STATES = ("EXECUTED", "FAILED", "REJECTED")


class SentinelAdapter:
    def __init__(self, client: ProductClient) -> None:
        self.client = client
        self.spec: ProductSpec = client.spec
        self.p = client.spec.api_prefix

    # ── reads (service token) ────────────────────────────────────────────────────────────────────
    def info(self, actor: str = "") -> Dict[str, Any]:
        return self.client.get(self.spec.info_path, actor=actor)

    def posture(self, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/posture", actor=actor, source=source)

    def cases(self, *, status: str = "", severity: str = "", q: str = "", limit: int = 25, cursor: str = "",
              actor: str = "", source: str = "projects") -> Dict[str, Any]:
        params = {k: v for k, v in (("status", status), ("severity", severity), ("q", q), ("limit", limit),
                                    ("cursor", cursor)) if v not in ("", None)}
        return self.client.get(f"{self.p}/cases", params=params, actor=actor, source=source)

    def case(self, case_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/cases/{case_id}", actor=actor, source=source)

    def actions(self, *, state: str = "", limit: int = 50, cursor: str = "", actor: str = "",
                source: str = "projects") -> Dict[str, Any]:
        params = {k: v for k, v in (("state", state), ("limit", limit), ("cursor", cursor)) if v not in ("", None)}
        return self.client.get(f"{self.p}/actions", params=params, actor=actor, source=source)

    def action(self, request_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/actions/{request_id}", actor=actor, source=source)

    def verification(self, request_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/actions/{request_id}/verification", actor=actor, source=source)

    def changes(self, after: int = 0, limit: int = 100, actor: str = "") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/changes", params={"after": after, "limit": limit}, actor=actor)

    # ── staging (service token; creates a proposal, which does nothing) ─────────────────────────────
    def propose(self, *, capability: str, ip: str, reason: str, duration_hours: Optional[int] = None,
                case_id: str = "", actor: str = "", source: str = "sidekick") -> Dict[str, Any]:
        body: Dict[str, Any] = {"ip": ip, "reason": reason}
        if duration_hours is not None:
            body["duration_hours"] = duration_hours
        if case_id:
            body["capability"] = capability
            return self.client.post(f"{self.p}/cases/{case_id}/actions", body, actor=actor, source=source)
        body["type"] = "manual_block" if capability == CAP_BAN else "manual_unblock"
        body["capability"] = capability
        return self.client.post(f"{self.p}/cases", body, actor=actor, source=source)

    # ── human decisions (decision token + signed approval token; see approvals.py) ───────────────────
    def decide(self, case_id: str, request_id: str, *, approved: bool, digest: str, rationale: str,
               approval_token: str, actor: str) -> Dict[str, Any]:
        body = {"approved": approved, "digest": digest}
        if rationale:
            body["rationale"] = rationale
        return self.client.post(f"{self.p}/cases/{case_id}/actions/{request_id}/decision", body, auth="decision",
                                actor=actor, source="projects", headers={"X-Approval-Token": approval_token})

    def execute(self, case_id: str, request_id: str, *, digest: str, approval_token: str, actor: str) -> Dict[str, Any]:
        body = {"digest": digest}
        return self.client.post(f"{self.p}/cases/{case_id}/actions/{request_id}/execute", body, auth="decision",
                                actor=actor, source="projects", headers={"X-Approval-Token": approval_token})


# ── pure mapping ──────────────────────────────────────────────────────────────────────────────────
def _params(a: Mapping[str, Any]) -> Mapping[str, Any]:
    p = a.get("params")
    return p if isinstance(p, Mapping) else {}


def action_ip(a: Mapping[str, Any]) -> str:
    return str(_params(a).get("ip") or _params(a).get("target") or "")


def is_unban(a: Mapping[str, Any]) -> bool:
    return str(a.get("capability", "")).endswith("unblock_ip")


def dry_run(a: Mapping[str, Any]) -> bool:
    mode = a.get("mode")
    return bool(mode.get("dry_run")) if isinstance(mode, Mapping) else False


def action_title(a: Mapping[str, Any]) -> str:
    ip = action_ip(a) or "an address"
    if is_unban(a):
        return f"Approve lifting the ban of {ip}"
    hours = _params(a).get("duration_hours")
    return f"Approve ban of {ip}" + (f" for {hours} h" if hours else "")


def action_consequence(a: Mapping[str, Any]) -> str:
    if is_unban(a):
        what = "Removes the CrowdSec ban decision for this address at the edge."
    else:
        what = "Adds a CrowdSec ban decision at the edge."
    if dry_run(a):
        what += " Dry run: blocking is not enabled in this deployment, so nothing will be blocked."
    return what + " The product re-reads the edge afterwards and reports whether the decision is present."


def read_back_text(a: Mapping[str, Any]) -> str:
    return ("After execution the product re-reads the CrowdSec decisions and records whether the ban is present; "
            "Projects only displays that record.")


def verification_line(a: Mapping[str, Any]) -> str:
    v = a.get("verification")
    state = str(v.get("state") or "") if isinstance(v, Mapping) else ""
    if state == "verified":
        return f"Verified: the {'lift' if is_unban(a) else 'ban'} is confirmed at the edge" + \
            (f" (read back {v.get('at')})" if v.get("at") else "")
    if state == "unverified":
        return "Unverified: the request was accepted but the read-back did not observe it"
    if state == "dry_run":
        return "Dry run: nothing was blocked"
    if str(a.get("state")) == "EXECUTED":
        return "Executed; read-back not recorded yet"
    return ""


def mission_state(a: Mapping[str, Any]) -> Tuple[str, str]:
    """(UI mission state, progress text) for an action. running|needs|scheduled|completed|failed."""
    st = str(a.get("state") or "")
    v = a.get("verification")
    vstate = str(v.get("state") or "") if isinstance(v, Mapping) else ""
    if st == "AWAITING_APPROVAL":
        return "needs", "1/4 awaiting approval"
    if st == "APPROVED":
        return "needs", "2/4 approved, awaiting execution"
    if st == "EXECUTED":
        if vstate == "verified":
            return "completed", "Verified"
        if vstate == "dry_run":
            return "completed", "Dry run"
        if vstate == "unverified":
            return "failed", "Read-back did not confirm"
        return "running", "3/4 read-back pending"
    if st == "FAILED":
        return "failed", "Execution failed"
    if st == "REJECTED":
        return "failed", "Rejected"
    return "scheduled", st.lower() or "unknown"


def mission_row(spec: ProductSpec, a: Mapping[str, Any]) -> Dict[str, Any]:
    state, progress = mission_state(a)
    rid, cid = str(a.get("request_id", "")), str(a.get("case_id", ""))
    return {"id": f"{spec.id}:{rid}", "title": ("Lift ban of " if is_unban(a) else "Ban ") + (action_ip(a) or "address"),
            "workflow": f"{spec.display_name} response", "state": state, "progress": progress,
            "context_used": [x for x in (f"case {cid}" if cid else "", "CrowdSec decisions") if x],
            "source_runtime": "security", "source_refs": [f"{spec.id}:{rid}"] + ([f"{spec.id}:{cid}"] if cid else [])}


def mission_steps(a: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The REAL lifecycle of the action as the product recorded it (observe, propose, approval, execute, read-back)."""
    st = str(a.get("state") or "")
    dec, rec = a.get("decision"), a.get("receipt")
    v = a.get("verification") if isinstance(a.get("verification"), Mapping) else {}
    decided = isinstance(dec, Mapping) and dec.get("approved") is True
    rejected = isinstance(dec, Mapping) and dec.get("approved") is False
    executed = st in ("EXECUTED", "FAILED")
    steps = [
        {"n": 1, "capability": "sentinel.observe", "provider": "crowdsec", "tier": 0, "status": "done",
         "why": f"case {a.get('case_id', '')} opened from CrowdSec alerts or a manual request"},
        {"n": 2, "capability": "sentinel.propose", "provider": "edge-sentinel", "tier": 1, "status": "done",
         "why": action_consequence(a)},
        {"n": 3, "capability": "approval.request", "provider": "edge-sentinel", "tier": 3,
         "status": "done" if decided else "failed" if rejected else "waiting",
         "why": ("approved by " + str(dec.get("actor") or dec.get("actor_user") or "a named user") +
                 (f" via {dec.get('via')}" if dec.get("via") else "")) if decided
         else "rejected by " + str(dec.get("actor") or dec.get("actor_user") or "a named user") if rejected
         else "policy requires a named person to approve before anything is blocked"},
        {"n": 4, "capability": CAP_UNBAN if is_unban(a) else CAP_BAN, "provider": "crowdsec", "tier": 4,
         "status": ("failed" if st == "FAILED" else "done") if executed else "todo",
         "why": (f"receipt {rec.get('receipt_id') or rec.get('id')}" if isinstance(rec, Mapping) and
                 (rec.get("receipt_id") or rec.get("id")) else "runs only after the approval, as a separate human step")},
        {"n": 5, "capability": "sentinel.read_back", "provider": "crowdsec", "tier": 0,
         "status": "done" if v.get("state") in ("verified", "dry_run") else "failed" if v.get("state") == "unverified" else "todo",
         "why": verification_line(a) or "the product re-reads the edge after execution and persists the result"},
    ]
    return steps


def attention_item(spec: ProductSpec, a: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """An Attention row for an action that needs a person, or whose read-back did not confirm."""
    st = str(a.get("state") or "")
    rid, cid = str(a.get("request_id", "")), str(a.get("case_id", ""))
    refs = [f"{spec.id}:{rid}"] + ([f"{spec.id}:{cid}"] if cid else [])
    v = a.get("verification")
    vstate = str(v.get("state") or "") if isinstance(v, Mapping) else ""
    if st == "AWAITING_APPROVAL":
        return {"id": f"{spec.id}:{rid}", "kind": "approval", "title": action_title(a),
                "reason": f"Edge Sentinel case {cid}" if cid else "Edge Sentinel proposal",
                "consequence": action_consequence(a), "available_actions": ["Review", "Approve", "Reject"],
                "priority": 90, "source_runtime": "security", "source_refs": refs}
    if st == "APPROVED":
        return {"id": f"{spec.id}:{rid}", "kind": "approval", "title": action_title(a).replace("Approve ", "Execute "),
                "reason": "Approved; execution is a separate human step", "consequence": action_consequence(a),
                "available_actions": ["Review", "Execute"], "priority": 85, "source_runtime": "security",
                "source_refs": refs}
    if st == "EXECUTED" and vstate == "unverified":
        return {"id": f"{spec.id}:{rid}:verify", "kind": "approval", "title": f"Check {action_ip(a) or 'the'} ban: read-back failed",
                "reason": verification_line(a), "consequence": "The edge may not be blocking this address.",
                "available_actions": ["Review"], "priority": 95, "source_runtime": "security", "source_refs": refs}
    if st == "FAILED":
        return {"id": f"{spec.id}:{rid}:failed", "kind": "approval", "title": f"Ban of {action_ip(a) or 'address'} failed",
                "reason": "The product could not execute the action", "consequence": "Nothing was blocked.",
                "available_actions": ["Review"], "priority": 80, "source_runtime": "security", "source_refs": refs}
    return None


def approval_card(spec: ProductSpec, a: Mapping[str, Any]) -> Dict[str, Any]:
    rid, cid = str(a.get("request_id", "")), str(a.get("case_id", ""))
    return {"kind": "approval", "product": spec.id, "record": {"case_id": cid, "request_id": rid},
            "title": action_title(a), "consequence": action_consequence(a), "read_back": read_back_text(a),
            "digest": str(a.get("digest") or ""), "state": str(a.get("state") or ""),
            "open": {"label": f"Open in {spec.display_name}", "app": spec.id, "kind": "action",
                     "url": spec.ui_link(f"/ui/actions/{rid}") if rid else spec.ui_link("/ui/"),
                     "params": {"request_id": rid}},
            "decide": {"route": f"POST /api/products/{spec.id}/actions/{rid}/decision",
                       "actor_required": "product-linked user"}}


def case_line(c: Mapping[str, Any]) -> str:
    return f"{c.get('case_id', '?')} [{c.get('severity', '?')}/{c.get('status', '?')}] {c.get('title') or c.get('case_type') or ''}".strip()


def activity_text(ch: Mapping[str, Any]) -> str:
    t = str(ch.get("type") or "change")
    actor = ch.get("actor") or ch.get("actor_user") or ""
    ref = ch.get("request_id") or ch.get("case_id") or ""
    return f"Edge Sentinel {t.replace('.', ' ')} {ref}".strip() + (f" by {actor}" if actor else "")


__all__ = ["SentinelAdapter", "ProductError", "OPERATOR", "CAP_DECIDE", "CAP_EXECUTE", "CAP_BAN", "CAP_UNBAN",
           "mission_row", "mission_steps", "mission_state", "attention_item", "approval_card", "action_title",
           "action_consequence", "action_ip", "case_line", "activity_text", "verification_line", "dry_run"]
