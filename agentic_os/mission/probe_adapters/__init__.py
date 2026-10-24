"""Provider reconciliation probes. Each module is one provider's evidence contract: what identifies an effect there, what counts as proof it happened, and what never counts as proof it did not.

The contract every adapter follows (enforced by `ProbeRegistry.judge`, not left to the adapter):
  CONFIRMED_EXECUTED      the effect (or its postcondition) is positively visible at the provider, with a reference to the record
  CONFIRMED_NOT_EXECUTED  never returned by these adapters: none of them is qualified to prove an effect absent
  INCONCLUSIVE            nothing found, several candidates, or not yet visible: a missing record is not proof the action never happened
  TRANSIENT_ERROR         the lookup itself failed (timeout, 429, 5xx); Retry-After is honoured
  UNSUPPORTED             the provider leaves no identifiable record of this action, or the lookup is not authorised: escalate
A probe is READ-ONLY. It never repeats the business action. Adapters: `lago` (billing), `gmail` (email.message.send), `postiz` (content.publish), `apollo` (outreach.enroll); shared pieces in `_common`.

Two ways an adapter can know an effect is ours. IDENTIFIED: the provider's own record is keyed by what we sent (a Lago subscription's external_id, an Apollo contact-in-sequence, a Gmail message whose Message-ID
we set and verified on the stored message). CANDIDATE: the record merely has the same fields (a refund on the invoice for the amount, a message to the recipient with the subject, a post with the text).
An unrelated action looks identical to a candidate, so by default a candidate is INCONCLUSIVE with the record named for a person to confirm; `accept_unique_match=True` is an explicit, per-adapter opt-in.

What to record at dispatch: `ProbeRegistry.register(..., ref_keys=..., ref_derive=...)` names the input fields (or derives scalar ones: a digest, a joined id list) that go into the reservation as `effect_ref`.
Bodies and credentials are never recorded.
"""
