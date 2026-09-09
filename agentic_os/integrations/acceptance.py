"""Cross-system acceptance harness — the P0 gate for the Agentic Apps remake.

The user-level test, in one run:

    "Use the customer records in my database and the policies in my Drive to handle refund
    requests from WhatsApp, ask me on Slack before issuing a refund, and use Polar for
    billing."

It composes everything built: **actions** (Apps, via the same ``AdapterPort`` duck-type as
``execution.py``) + **evidence** (Sources) + a **governed approval gate** + **verification**
+ a **Projects projection**. It is a harness, not a hard-coded demo — every external leg is
injected, so it runs deterministically with fakes *and* live by injecting real
adapters/connectors.

Two invariants make it the freeze gate:

* **The money leg cannot move without approval.** ``billing.refund.execute`` runs only when a
  human ``approved`` decision is True *and* ``execute_writes`` is on (a sandbox); otherwise it
  is WITHHELD or halts AWAITING_APPROVAL — never executed. Safe by default.
* **A missing provider/source ABSTAINS, it does not fail.** So the harness is green today on
  the connected legs (Slack action, Postgres evidence) and becomes fully live as Google Drive
  and WhatsApp are provisioned, with no change to the harness.

``run_acceptance`` returns an :class:`AcceptanceReport` whose ``to_projection`` renders the
Mission the way Projects does (steps + evidence + verdict), closing the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple


class LegStatus(str, Enum):
    EXECUTED = "executed"          # an action ran and returned ok
    VERIFIED = "verified"          # a write was re-observed successfully
    RETRIEVED = "retrieved"        # evidence was gathered from a source
    AWAITING_APPROVAL = "awaiting_approval"   # halted at the human gate
    WITHHELD = "withheld"          # approved, but not executed (safe mode — would move money)
    REJECTED = "rejected"          # a human declined (a correct outcome, not a failure)
    ABSTAINED = "abstained"        # provider/source not connected — skipped cleanly
    REFUSED = "refused"            # an unexpected failure (the only status that fails the gate)


@dataclass(frozen=True)
class LegResult:
    name: str
    kind: str              # "action" | "evidence"
    status: LegStatus
    provider: str = ""
    capability: str = ""
    tier: int = 0
    detail: str = ""
    why: str = ""
    object_id: str = ""
    refs: Tuple[dict, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "status": self.status.value,
                "provider": self.provider, "capability": self.capability, "tier": self.tier,
                "detail": self.detail, "why": self.why, "object_id": self.object_id,
                "refs": list(self.refs)}


@dataclass(frozen=True)
class AcceptanceReport:
    legs: Tuple[LegResult, ...]
    evidence: Tuple[dict, ...]
    goal: str = ""
    approved: Optional[bool] = None

    # ── verdict: the P0 predicate ────────────────────────────────────────────────
    @property
    def refused(self) -> Tuple[LegResult, ...]:
        return tuple(l for l in self.legs if l.status is LegStatus.REFUSED)

    @property
    def gate_enforced(self) -> bool:
        """The money leg never executed without an explicit human approval."""
        refund = next((l for l in self.legs if l.capability == "billing.refund.execute"), None)
        if refund is None:
            return True
        return refund.status is not LegStatus.EXECUTED or self.approved is True

    @property
    def passed(self) -> bool:
        # The composition holds: nothing refused unexpectedly, and the governed gate held.
        return not self.refused and self.gate_enforced

    def to_projection(self) -> Dict[str, Any]:
        """Render like the Projects Mission detail (steps + context_used + verdict)."""
        return {
            "goal": self.goal,
            "steps": [l.to_dict() for l in self.legs if l.kind == "action"],
            "context_used": [l.to_dict() for l in self.legs if l.kind == "evidence"],
            "evidence": list(self.evidence),
            "verdict": {"passed": self.passed, "gate_enforced": self.gate_enforced,
                        "refused": [l.name for l in self.refused],
                        "abstained": [l.name for l in self.legs if l.status is LegStatus.ABSTAINED],
                        "evidence_count": sum(int(r.get("record_count", len(r.get("refs", []))) or 0)
                                              for r in self.evidence)},
        }


def _ok(res: Any) -> bool:
    return bool(getattr(res, "ok", False))


def run_acceptance(
    *,
    actions: Mapping[str, Any],
    evidence: Optional[Mapping[str, Any]] = None,
    approved: Optional[bool] = None,
    envelope: Optional[object] = None,
    execute_writes: bool = False,
    requests: Optional[Mapping[str, Mapping[str, Any]]] = None,
    goal: str = ("Use DB customer records + Drive policies to handle WhatsApp refund requests, "
                 "ask on Slack before refunding, use Polar for billing."),
) -> AcceptanceReport:
    """Run the refund Mission across whatever is connected.

    - ``actions``: provider id → an ``AdapterPort`` (``.execute``/``.observe``). Absent = not
      connected → that leg ABSTAINS.
    - ``evidence``: source name → an object exposing ``.refs`` (and optionally ``.record_count``
      / ``.observed_at`` / ``.label``). Absent = not connected → that evidence leg ABSTAINS.
    - ``approved``: the human decision at the Slack gate (None halts; False rejects; True lets
      the refund proceed).
    - ``execute_writes``: safety switch — the refund is WITHHELD unless this is True (a sandbox).
    - ``envelope``: the GovernedEnvelope every write runs under.
    """
    ev = dict(evidence or {})
    req = dict(requests or {})
    legs: List[LegResult] = []
    evidence_out: List[dict] = []

    def request_for(cap: str, default: Dict[str, Any]) -> Dict[str, Any]:
        return dict(req.get(cap, default))

    def action_leg(name: str, provider: str, capability: str, tier: int, write: bool,
                   why: str, request: Dict[str, Any]) -> LegResult:
        adapter = actions.get(provider)
        if adapter is None:
            return LegResult(name, "action", LegStatus.ABSTAINED, provider, capability, tier,
                             f"{provider} not connected — abstained", why)
        try:
            res = adapter.execute(capability, request, envelope if write else None)
        except Exception as e:  # a live adapter blew up — a real failure
            return LegResult(name, "action", LegStatus.REFUSED, provider, capability, tier,
                             f"error: {type(e).__name__}: {e}", why)
        if _ok(res):
            return LegResult(name, "action", LegStatus.EXECUTED, provider, capability, tier,
                             "ok", why, object_id=str(getattr(res, "provider_object_id", "") or ""))
        return LegResult(name, "action", LegStatus.REFUSED, provider, capability, tier,
                         f"not ok: {getattr(res, 'error', '') or 'unknown'}", why)

    # 1. intake — read the request on the channel it arrived
    legs.append(action_leg("Intake WhatsApp message", "whatsapp_business", "chat.message.read", 2,
                           False, "read the customer's message on the channel it arrived",
                           request_for("chat.message.read", {"limit": 1})))
    # 2. identify — record/find the customer in the CRM
    legs.append(action_leg("Identify customer", "hubspot", "crm.contact.upsert", 2, True,
                           "identify and record the customer in the CRM",
                           request_for("crm.contact.upsert", {"email": "sarah@example.com"})))
    # 3. find the order to refund (read-only)
    legs.append(action_leg("Find order", "polar", "billing.order.find", 1, False,
                           "find the order that will be refunded",
                           request_for("billing.order.find", {})))

    # 4. evidence — the Sources half (DB customer records + Drive policy)
    for name, src_key, why in (("Customer records (DB)", "db",
                                "scoped SQL against the live source — query in place, no ingest"),
                               ("Refund policy (Drive)", "policy",
                                "vector retrieval over indexed policy files")):
        src = ev.get(src_key)
        if src is None:
            legs.append(LegResult(name, "evidence", LegStatus.ABSTAINED, detail=f"{src_key} source not connected", why=why))
            continue
        refs = tuple(dict(r) for r in getattr(src, "refs", ()) or ())
        rec = {"source": src_key, "provider": getattr(src, "provider", ""), "refs": [r for r in refs]}
        if getattr(src, "record_count", None) is not None:
            rec["record_count"] = src.record_count
        if getattr(src, "observed_at", None):
            rec["observed_at"] = src.observed_at
        evidence_out.append(rec)
        legs.append(LegResult(name, "evidence", LegStatus.RETRIEVED, provider=getattr(src, "provider", ""),
                              detail=(f"{src.record_count} records retrieved" if getattr(src, "record_count", None) is not None
                                      else f"{len(refs)} items"), why=why, refs=refs))

    # 5. approval — the governed human gate
    approval_req = request_for("approval.request", {"channel": "#first-project",
                                                    "text": "Approve refund for Sarah Chen ($129)?"})
    legs.append(action_leg("Request approval", "slack", "approval.request", 3, True,
                           "policy requires a human to approve before money moves", approval_req))

    # 6. refund — the money move, gated on approval and safe by default
    refund_leg, refund_ref = _refund_leg(actions, approved, execute_writes, envelope,
                                         request_for("billing.refund.execute", {"order_id": "ord_1"}))
    legs.append(refund_leg)

    # 7. verify — re-observe the refund if it actually executed
    if refund_leg.status is LegStatus.EXECUTED and refund_ref:
        adapter = actions.get("polar")
        try:
            obs = adapter.observe(refund_ref) if adapter else None
            verified = bool(getattr(obs, "found", False))
        except Exception:
            verified = False
        legs.append(LegResult("Verify refund", "action",
                              LegStatus.VERIFIED if verified else LegStatus.REFUSED, "polar",
                              "billing.refund.observe", 1,
                              "re-read the refund to confirm it landed" if verified else "could not verify",
                              why="verification — a write is not done until re-observed", object_id=refund_ref))

    # 8. reply — tell the customer
    legs.append(action_leg("Reply to customer", "whatsapp_business", "chat.message.send", 3, True,
                           "close the loop with the customer",
                           request_for("chat.message.send", {"channel": "sarah", "text": "Your refund is on its way."})))

    return AcceptanceReport(legs=tuple(legs), evidence=tuple(evidence_out), goal=goal, approved=approved)


def _refund_leg(actions: Mapping[str, Any], approved: Optional[bool], execute_writes: bool,
                envelope: Optional[object], request: Dict[str, Any]) -> Tuple[LegResult, str]:
    name, provider, cap, tier, why = ("Issue refund", "polar", "billing.refund.execute", 4,
                                      "move money — runs only after approval, then verified")
    if provider not in actions:
        return LegResult(name, "action", LegStatus.ABSTAINED, provider, cap, tier,
                         "polar not connected — abstained", why), ""
    if approved is None:
        return LegResult(name, "action", LegStatus.AWAITING_APPROVAL, provider, cap, tier,
                         "halted — waiting for human approval in Slack", why), ""
    if approved is False:
        return LegResult(name, "action", LegStatus.REJECTED, provider, cap, tier,
                         "a human declined — no money moved", why), ""
    if not execute_writes:
        return LegResult(name, "action", LegStatus.WITHHELD, provider, cap, tier,
                         "approved, but withheld — real money; set execute_writes (sandbox) to run", why), ""
    adapter = actions[provider]
    try:
        res = adapter.execute(cap, request, envelope)
    except Exception as e:
        return LegResult(name, "action", LegStatus.REFUSED, provider, cap, tier,
                         f"error: {type(e).__name__}: {e}", why), ""
    if _ok(res):
        ref = str(getattr(res, "provider_object_id", "") or "")
        return LegResult(name, "action", LegStatus.EXECUTED, provider, cap, tier, "refund executed", why,
                         object_id=ref), ref
    return LegResult(name, "action", LegStatus.REFUSED, provider, cap, tier,
                     f"refund failed: {getattr(res, 'error', '') or 'unknown'}", why), ""
