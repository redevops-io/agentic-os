"""The decision flow: a person clicks Approve/Reject/Execute in Projects, and the PRODUCT decides what that means.

Reuses ``agentic_os.mission.approval`` (no second token format): the approval token is HMAC-SHA256 over operator,
capability, the sha256 of the exact inputs ``{case_id, request_id, approved, digest}``, a marker naming who clicked,
``exp`` (120 s) and a nonce, verified and single-use on the product side (durable replay guard). So:

* the approval binds ONE action digest; a changed action is a 409 ``stale_approval`` here (fresh read) and again in the
  product, and the approval is void;
* the marker is ``{"approved": bool, "by": "projects:<principal>", "via": "projects"}``; the product maps ``by`` to a
  LINKED product user (``actor_links``) and records the decision with that user as actor and ``via: projects``; an
  unlinked principal is refused by the product (403 ``actor_not_linked``);
* the product's own mandatory approval and post-action read-back remain the only way an action executes: this module
  never executes anything itself, never marks anything verified, and never decides for a service or an agent. Decide and
  execute are not Sidekick tools (``schemas.FORBIDDEN_TOOLS``) and are reachable only through the ``human_only``
  Projects routes.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from agentic_os.mission.approval import mint_approval_token

from . import qa, sentinel
from .client import ProductError
from .hub import ProductHub

TOKEN_TTL_S = 120


def _marker(principal: str, approved: bool) -> Dict[str, Any]:
    return {"approved": bool(approved), "by": f"projects:{principal}", "via": "projects"}


def _mint(hub: ProductHub, product_id: str, *, operator: str, capability: str, inputs: Mapping[str, Any],
          principal: str, approved: bool) -> str:
    secret = hub.client(product_id).approval_secret()
    if not secret:
        raise ProductError("no_approval_secret", "no approval secret is configured for this product",
                           reason="a decision from Projects needs the shared approval secret",
                           remedy=f"set auth.approval_secret_file for {product_id}, or decide in the product UI")
    tok = mint_approval_token(secret, operator=operator, capability=capability, mission_id=None, node_id=None,
                              inputs=dict(inputs), marker=_marker(principal, approved), ttl_s=TOKEN_TTL_S)
    if not tok:
        raise ProductError("no_approval_secret", "the approval token could not be minted")
    return tok


def _require_ready(hub: ProductHub, product_id: str) -> None:
    if not hub.ready(product_id):
        u = hub.unavailable_reason(product_id)
        raise ProductError("unavailable", u.get("reason", "product unavailable"), reason=u.get("reason", ""),
                           remedy=u.get("remedy", ""))


def sentinel_decision(hub: ProductHub, product_id: str, request_id: str, *, approved: bool, digest: str,
                      rationale: str, principal: str) -> Dict[str, Any]:
    _require_ready(hub, product_id)
    ad = hub.sentinel(product_id)
    a = ad.action(request_id, actor=f"projects:{principal}")           # fresh read, never the cache
    if str(a.get("state")) != "AWAITING_APPROVAL":
        raise ProductError("conflict", f"the action is {a.get('state')}, not awaiting approval", status=409)
    if not digest or digest != str(a.get("digest") or ""):
        raise ProductError("stale_approval", "the action changed since you reviewed it; review it again", status=409)
    case_id = str(a.get("case_id") or "")
    inputs = {"case_id": case_id, "request_id": request_id, "approved": bool(approved), "digest": digest}
    tok = _mint(hub, product_id, operator=sentinel.OPERATOR, capability=sentinel.CAP_DECIDE, inputs=inputs,
                principal=principal, approved=approved)
    out = ad.decide(case_id, request_id, approved=approved, digest=digest, rationale=rationale,
                    approval_token=tok, actor=f"projects:{principal}")
    hub.invalidate(product_id)
    return out


def sentinel_execute(hub: ProductHub, product_id: str, request_id: str, *, digest: str, principal: str) -> Dict[str, Any]:
    _require_ready(hub, product_id)
    ad = hub.sentinel(product_id)
    a = hub.with_mode(product_id, ad.action(request_id, actor=f"projects:{principal}"))
    if sentinel.settled_dry_run(a):
        raise ProductError("conflict", "the action already ran as a dry run; nothing was blocked. Enable blocking in the "
                                       "product to execute it for real", status=409)
    if str(a.get("state")) != "APPROVED":
        raise ProductError("conflict", f"the action is {a.get('state')}, not approved for execution", status=409)
    if not digest or digest != str(a.get("digest") or ""):
        raise ProductError("stale_approval", "the action changed since it was approved; review it again", status=409)
    case_id = str(a.get("case_id") or "")
    inputs = {"case_id": case_id, "request_id": request_id, "digest": digest}
    tok = _mint(hub, product_id, operator=sentinel.OPERATOR, capability=sentinel.CAP_EXECUTE, inputs=inputs,
                principal=principal, approved=True)
    out = ad.execute(case_id, request_id, digest=digest, approval_token=tok, actor=f"projects:{principal}")
    hub.invalidate(product_id)
    return out


def qa_approval(hub: ProductHub, product_id: str, run_id: str, *, approved: bool, estimate_digest: str, rationale: str,
                principal: str) -> Dict[str, Any]:
    _require_ready(hub, product_id)
    ad = hub.qa(product_id)
    r = ad.run(run_id, actor=f"projects:{principal}")
    if str(r.get("state")) != "awaiting_approval":
        raise ProductError("conflict", f"the run is {r.get('state')}, not awaiting approval", status=409)
    if not estimate_digest or estimate_digest != str(r.get("estimate_digest") or ""):
        raise ProductError("stale_approval", "the estimate changed since you reviewed it", status=409)
    inputs = {"run_id": run_id, "approved": bool(approved), "estimate_digest": estimate_digest}
    tok = _mint(hub, product_id, operator=qa.OPERATOR, capability=qa.CAP_APPROVE_RUN, inputs=inputs,
                principal=principal, approved=approved)
    out = ad.approve(run_id, approved=approved, estimate_digest=estimate_digest, rationale=rationale,
                     approval_token=tok, actor=f"projects:{principal}")
    hub.invalidate(product_id)
    return out


__all__ = ["sentinel_decision", "sentinel_execute", "qa_approval", "TOKEN_TTL_S"]
