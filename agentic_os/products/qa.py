"""redevops-qa adapter (the ``quality`` domain): the QA service's own ``/v1`` API as typed calls plus the pure mapping to
Projects rows. Same rules as ``sentinel.py``: no storage, product ids preserved (``r…`` run ids, ``tgt-…`` targets), the
level and outcome rendered exactly as the product recorded them.

The whole module is behind the owner's gate (``gates.QA_EXTERNAL_TARGET_ACCEPTANCE_MET``); ``tools.py`` and
``projections.py`` never call it while the gate is closed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .client import ProductClient
from .registry import ProductSpec

OPERATOR = "redevops-qa"
CAP_APPROVE_RUN = "qa.approve_run"
TERMINAL = ("finished", "failed", "cancelled", "timed_out", "interrupted", "refused", "failed_to_start", "rejected")
GOOD_OUTCOMES = ("VERIFIED", "CORROBORATED")


class QaAdapter:
    def __init__(self, client: ProductClient) -> None:
        self.client = client
        self.spec: ProductSpec = client.spec
        self.p = client.spec.api_prefix        # "/v1"

    def info(self, actor: str = "") -> Dict[str, Any]:
        return self.client.get(self.spec.info_path, actor=actor)

    def targets(self, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/targets", actor=actor, source=source)

    def profiles(self, target_id: str = "", actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/profiles", params={"target_id": target_id} if target_id else None,
                               actor=actor, source=source)

    def estimate(self, target_id: str, profile: str, actor: str = "", source: str = "sidekick") -> Dict[str, Any]:
        return self.client.post(f"{self.p}/estimates", {"target_id": target_id, "profile": profile}, actor=actor,
                                source=source)

    def start_run(self, body: Mapping[str, Any], actor: str = "", source: str = "sidekick") -> Dict[str, Any]:
        return self.client.post(f"{self.p}/runs", dict(body), actor=actor, source=source)

    def runs(self, *, state: str = "", limit: int = 25, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        params = {k: v for k, v in (("state", state), ("limit", limit)) if v not in ("", None)}
        return self.client.get(f"{self.p}/runs", params=params, actor=actor, source=source)

    def run(self, run_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/runs/{run_id}", actor=actor, source=source)

    def events(self, run_id: str, after: int = 0, limit: int = 100, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/runs/{run_id}/events", params={"after": after, "limit": limit},
                               actor=actor, source=source)

    def cancel(self, run_id: str, actor: str = "", source: str = "sidekick") -> Dict[str, Any]:
        return self.client.post(f"{self.p}/runs/{run_id}/cancel", {}, actor=actor, source=source)

    def evidence(self, run_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/runs/{run_id}/evidence", actor=actor, source=source)

    def verify(self, run_id: str, actor: str = "", source: str = "projects") -> Dict[str, Any]:
        return self.client.get(f"{self.p}/runs/{run_id}/verify", actor=actor, source=source)

    def approve(self, run_id: str, *, approved: bool, estimate_digest: str, rationale: str, approval_token: str,
                actor: str) -> Dict[str, Any]:
        body = {"approved": approved, "estimate_digest": estimate_digest}
        if rationale:
            body["rationale"] = rationale
        return self.client.post(f"{self.p}/runs/{run_id}/approval", body, auth="decision", actor=actor,
                                source="projects", headers={"X-Approval-Token": approval_token})


def mission_state(r: Mapping[str, Any]) -> Tuple[str, str]:
    st = str(r.get("state") or "")
    outcome = str(r.get("outcome") or "")
    prog = r.get("progress") if isinstance(r.get("progress"), Mapping) else None
    if st == "awaiting_approval":
        return "needs", "awaiting approval"
    if st == "queued":
        return "running", "queued"
    if st == "running":
        if prog and prog.get("total"):
            return "running", f"{prog.get('step', 0)}/{prog.get('total')}"
        return "running", (str(prog.get("lane")) + ", step count unavailable") if prog and prog.get("lane") else "running"
    if st == "finished":
        if outcome in GOOD_OUTCOMES:
            return "completed", str(r.get("level") or outcome).title() if r.get("level") else outcome.title()
        return "failed", outcome.replace("_", " ").lower() or "finished"
    if st == "cancelled":
        return "failed", "Cancelled"
    if st in ("rejected", "refused"):
        return "failed", st.title()
    if st in TERMINAL:
        return "failed", st.replace("_", " ")
    return "scheduled", st or "unknown"


def mission_row(spec: ProductSpec, r: Mapping[str, Any]) -> Dict[str, Any]:
    state, progress = mission_state(r)
    rid = str(r.get("id", ""))
    prof = str(r.get("profile") or r.get("plan") or "run")
    return {"id": f"{spec.id}:{rid}", "title": f"QA run {rid}", "workflow": f"QA run {prof}", "state": state,
            "progress": progress, "context_used": [x for x in (f"target {r.get('target_id')}" if r.get("target_id") else "",
                                                               f"profile {prof}") if x],
            "source_runtime": "quality", "source_refs": [f"{spec.id}:{rid}"]}


def mission_steps(events: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, e in enumerate(events, 1):
        t = str(e.get("type") or "")
        status = "done" if t.endswith(".finished") or t in ("run.finished", "run.approved", "run.started", "run.queued") \
            else "failed" if t in ("run.cancelled", "run.interrupted", "run.rejected") else "waiting" if t.endswith("requested") else "done"
        out.append({"n": i, "capability": t or "event", "provider": str(e.get("engine") or "redevops-qa"), "tier": 0,
                    "status": status, "why": str(e.get("label") or t)})
    return out


def attention_item(spec: ProductSpec, r: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    rid = str(r.get("id", ""))
    st = str(r.get("state") or "")
    outcome = str(r.get("outcome") or "")
    refs = [f"{spec.id}:{rid}"]
    if st == "awaiting_approval":
        return {"id": f"{spec.id}:{rid}", "kind": "approval", "title": f"Approve QA run {rid}",
                "reason": f"profile {r.get('profile')} on target {r.get('target_id')}",
                "consequence": "Runs the frozen profile against the registered target through the egress gateway; "
                               "an external-target result is at most CORROBORATED.",
                "available_actions": ["Review", "Approve", "Reject"], "priority": 80, "source_runtime": "quality",
                "source_refs": refs}
    if st == "finished" and outcome and outcome not in GOOD_OUTCOMES:
        return {"id": f"{spec.id}:{rid}:outcome", "kind": "approval", "title": f"QA run {rid}: {outcome.replace('_', ' ').lower()}",
                "reason": "The run did not produce an accepted result", "consequence": "Review the evidence before relying on this run.",
                "available_actions": ["Review"], "priority": 75, "source_runtime": "quality", "source_refs": refs}
    return None


def approval_card(spec: ProductSpec, r: Mapping[str, Any]) -> Dict[str, Any]:
    rid = str(r.get("id", ""))
    return {"kind": "approval", "product": spec.id, "record": {"run_id": rid, "target_id": r.get("target_id")},
            "title": f"Approve QA run {rid}", "consequence": "Starts the run once approved; the digest binds the estimate you saw.",
            "read_back": "The run's evidence bundle is re-verified in a separate process after it finishes.",
            "digest": str(r.get("estimate_digest") or ""), "state": str(r.get("state") or ""),
            "open": {"label": f"Open in {spec.display_name}", "app": spec.id, "kind": "run",
                     "url": spec.ui_link(f"/ui/runs/{rid}"), "params": {"run_id": rid}},
            "decide": {"route": f"POST /api/products/{spec.id}/runs/{rid}/approval",
                       "actor_required": "product-linked user"}}


__all__ = ["QaAdapter", "OPERATOR", "CAP_APPROVE_RUN", "mission_row", "mission_steps", "mission_state",
           "attention_item", "approval_card", "TERMINAL"]
