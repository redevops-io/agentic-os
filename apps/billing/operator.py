"""agentic-billing as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive billing as a capability operator — the same core Lago
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  billing.summary              — read-only KPIs + overdue invoices (fact `billing_activity`)
  billing.dunning              — retry payment on overdue invoices  [approval gate: dunning]
                                 scoped by ``account_id`` / ``invoice_ids`` (ONE account, read-back verified);
                                 unscoped calls are refused ``scope_required`` unless ``all_accounts`` is set
  billing.refund               — stage a refund credit-note              [approval gate: refund]
  billing.create_subscription  — onboard a customer onto a paid plan (undo: cancel_subscription)
  billing.cancel_subscription  — terminate a subscription (saga compensation)

Money-moving capabilities carry approval_required=True (matches modules.yaml gates), so the
runtime parks them as HumanTasks before execution.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def _split_ids(v):
    if v is None:
        return None
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


def _dunning_scope(inp: dict) -> tuple[str, list | None]:
    """(account_id, invoice_ids) from the node inputs. The runtime delivers the decision's scope under
    ``intent`` (string-valued, see decision.BillingPlanner); direct callers pass them flat."""
    intent = inp.get("intent") if isinstance(inp.get("intent"), dict) else {}
    account = inp.get("account_id") or intent.get("account_id") or ""
    ids = _split_ids(inp.get("invoice_ids") if inp.get("invoice_ids") is not None else intent.get("invoice_ids"))
    return str(account), ids


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"))


def _make_dunning(on_dunning=None):
    def _dunning(inp: dict):
        """Scope is REQUIRED (``account_id`` [+ ``invoice_ids``]); an unscoped call is refused ``scope_required``
        unless ``all_accounts`` is true AND the runtime-delivered ``_approval`` is present. Every executed path
        reads each invoice back from Lago and returns the declared outputs. The gate itself lives in core."""
        approval = inp.get("_approval")           # the runtime-delivered approval marker; absent => stage only
        account, ids = _dunning_scope(inp)
        intent = inp.get("intent") if isinstance(inp.get("intent"), dict) else {}
        all_accounts = _truthy(inp.get("all_accounts")) or _truthy(intent.get("all_accounts"))
        res = core.chase_invoices(account, ids, approval=approval, all_accounts=all_accounts)
        if approval and on_dunning is not None:
            try:
                on_dunning(inp.get("_mission"), res)
            except Exception:  # noqa: BLE001 — the ledger must never change the action's outcome
                pass
        return res
    return _dunning


def build_billing_operator(on_dunning=None) -> Operator:
    return Operator("billing", [
        capability(
            "billing.summary",
            lambda inp: core.fetch_activity(),
            provides=["billing_activity"],
            outputs={"billing_activity": "KPIs + overdue invoices from the live Lago core"},
            estimated_value="low", deterministic=False, latency_ms=300,
            concurrency_mode="read_only",
        ),
        capability(
            "billing.dunning",
            _make_dunning(on_dunning),
            provides=["dunning_attempted"],
            outputs={"dunning_attempted": "retry_payment issued on the in-scope overdue invoices",
                     "dunning_verified": "bool: every chased invoice read back as settled (and >=1 chased)",
                     "settled_count": "invoices read back as paid after the retry",
                     "failed_count": "chased invoices rejected or not settled on read-back"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "dunning sends customer email + retries payments: NOT reversible"}, approval_required=True,
            permissions=["billing:write"], estimated_value="high", latency_ms=1500,
            concurrency_mode="exclusive", resource_keys=["billing:dunning"],  # one dunning run at a time
        ),
        capability(
            "billing.refund",
            lambda inp: core.stage_refund(inp),
            provides=["refund_staged"],
            outputs={"refund_staged": "refund credit-note staged for human approval"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "submitting a payment/credit note is NOT automatically reversible"}, approval_required=True,
            permissions=["billing:write"], estimated_value="medium", latency_ms=500,
            concurrency_mode="exclusive", concurrency_key="billing:invoice:{invoice}",
        ),
        capability(
            "billing.create_subscription",
            lambda inp: core.create_subscription(inp),     # approval gate lives in core (_approval)
            provides=["subscription"],
            outputs={"subscription": "paid subscription created on the live Lago core"},
            side_effecting=True,
            compensation={"supported": True, "capability": "billing.cancel_subscription", "requires_approval": True,
                          "reversibility": "REVERSIBLE_EXPLICIT", "notes": "cancel the subscription; the cancel is itself approval-gated"}, approval_required=True,
            permissions=["billing:write"], estimated_value="high", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="billing:customer:{customer}",
        ),
        capability(
            "billing.cancel_subscription",
            lambda inp: core.cancel_subscription(inp),     # approval gate lives in core (_approval)
            provides=["subscription_cancelled"],
            outputs={"subscription_cancelled": "subscription terminated (saga compensation)"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "terminating a subscription is a business decision; re-subscribing is a NEW forward action"}, approval_required=True,
            permissions=["billing:write"], estimated_value="medium", latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="billing:customer:{customer}",
        ),
    ])
