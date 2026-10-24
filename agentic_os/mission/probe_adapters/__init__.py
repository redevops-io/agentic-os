"""Provider reconciliation probes. Each module is one provider's evidence contract: what identifies an effect there, what counts as proof it happened, and what never counts as proof it did not.

The contract every adapter follows (enforced by `ProbeRegistry.judge`, not left to the adapter):
  CONFIRMED_EXECUTED      the effect (or its postcondition) is positively visible at the provider, with a reference to the record
  CONFIRMED_NOT_EXECUTED  never returned by these adapters: none of them is qualified to prove an effect absent
  INCONCLUSIVE            nothing found, several candidates, or not yet visible: a missing record is not proof the action never happened
  TRANSIENT_ERROR         the lookup itself failed (timeout, 429, 5xx); Retry-After is honoured
  UNSUPPORTED             the provider leaves no identifiable record of this action, or the lookup is not authorised: escalate
A probe is READ-ONLY. It never repeats the business action.
"""
