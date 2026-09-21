"""S6/S7 — governed social publishing + engagement (plan §11 Phase S6/S7, §27, §28).

Publishing/engagement use the SAME ActionRequest → Governance → Operator → Receipt → Verification
invariant as every other governed action. This is a real ``operator_sdk`` operator whose capabilities are
``social.*`` (side-effecting + approval-required), so the Mission Runtime only invokes them after a
Decision. Enforced here:

  * **content-digest approval binding** (plan §27): the handler refuses if the draft's digest differs
    from the digest Governance approved — an edited draft invalidates the approval;
  * **duplicate-publish prevention** (plan §S6): the same content is never published twice;
  * **individual outreach fails closed** (plan §28): ``contact_individual`` / ``send_dm`` run only if the
    provider VERIFIED/POLICY_SCOPED-supports them (the fixture marks them PROHIBITED);
  * **verification after publish** (plan §S6): a receipt is SUCCEEDED only when a real post id is
    observed — a missing/duplicate post id yields HELD, not SUCCEEDED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Set

from agentic_os.mission.operator_sdk import Capability, Operator, capability

from .contracts import ActionClass, SocialActionReceipt, SocialActionRequest

_OUTBOUND_ACTIONS = (
    ActionClass.PUBLISH_OWNED_CHANNEL,
    ActionClass.REPLY_PUBLIC,
    ActionClass.REPLY_TO_EXISTING_THREAD,
    ActionClass.CONTACT_INDIVIDUAL,      # registered but gated: PROHIBITED in the fixture → fails closed
    ActionClass.SEND_DM,
)


@dataclass
class SocialOperator:
    """Wraps a SocialProvider as a governed Operator. Tracks published content digests for
    duplicate prevention across invokes."""

    provider: Any                        # a SocialProvider (capabilities/publish)
    name: str = "social"
    operator: Operator = field(init=False)
    _published: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        caps = []
        for action in _OUTBOUND_ACTIONS:
            caps.append(self._build(action))
        self.operator = Operator(self.name, caps)

    def _build(self, action: ActionClass) -> Capability:
        def handler(inputs: Mapping[str, Any]) -> dict:
            return self._run(action, dict(inputs))
        return capability(action.value, handler, operator=self.name, side_effecting=True,
                          approval_required=True, deterministic=False,
                          data_classifications=["social"], isolation_class="social-provider")

    def _run(self, action: ActionClass, inputs: Dict[str, Any]) -> dict:
        decision_id = str(inputs.pop("decision_id", ""))
        approved_digest = str(inputs.pop("approved_intent_digest", ""))
        request = SocialActionRequest(
            action_class=action, provider=str(inputs.get("provider", getattr(self.provider, "provider", ""))),
            content_digest=str(inputs.get("content_digest", "")), target=str(inputs.get("target", "")),
            account=str(inputs.get("account", "")),
            evidence_refs=tuple(inputs.get("evidence_refs", ()) or ()))

        # Content-digest approval binding: a draft edited after approval has a different intent digest.
        if approved_digest and approved_digest != request.intent_digest():
            return self._held(request, decision_id, "content changed after approval (digest mismatch)")

        # Fail closed on capabilities the provider does not VERIFIED/POLICY_SCOPED-support.
        if not self.provider.capabilities().supports(action.value):
            return self._held(request, decision_id, f"provider does not permit {action.value}")

        # Duplicate-publish prevention.
        if request.content_digest in self._published:
            return self._held(request, decision_id, "duplicate publish prevented")

        result = self.provider.publish(request)
        post_id = result.get("post_id", "")
        if not post_id or result.get("duplicate"):
            return self._held(request, decision_id, "no verifiable post id" if not post_id
                              else "provider reported duplicate")

        self._published.add(request.content_digest)
        receipt = SocialActionReceipt(
            provider=request.provider, account=request.account, action_class=action.value,
            content_digest=request.content_digest, target_surface=request.target, status="SUCCEEDED",
            provider_post_id=post_id, provider_post_url=result.get("url", ""),
            provider_response="ok", verification_state="verified", decision_id=decision_id)
        return {"receipt": receipt.to_dict(), "verified": True}

    def _held(self, request: SocialActionRequest, decision_id: str, reason: str) -> dict:
        receipt = SocialActionReceipt(
            provider=request.provider, account=request.account, action_class=request.action_class.value,
            content_digest=request.content_digest, target_surface=request.target, status="HELD",
            verification_state="unverified", decision_id=decision_id, error=reason)
        return {"receipt": receipt.to_dict(), "verified": False, "reason": reason}
