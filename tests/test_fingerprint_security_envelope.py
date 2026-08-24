"""The plan fingerprint binds the security envelope when a mission opts into a MissionPolicy — so a
revoked grant, changed policy, or swapped model can no longer EXACT-REPLAY a stale sealed plan. A
security-free mission's fingerprint is byte-identical to before (existing sealed plans still replay)."""
from __future__ import annotations

from agentic_os.mission.context_view import plan_fingerprint
from agentic_os.mission.policy import MissionPolicy


def test_backward_compatible_without_a_policy():
    # No security envelope → identical to the historical fingerprint. Existing sealed plans still replay.
    assert plan_fingerprint("a->b") == plan_fingerprint("a->b", security="")
    assert plan_fingerprint("a->b", intent_id="i") == plan_fingerprint("a->b", "i", security="")


def test_envelope_changes_the_fingerprint():
    bare = plan_fingerprint("a->b")
    with_policy = plan_fingerprint("a->b", security="policy=rcv1:deadbeef")
    assert with_policy != bare                      # attaching a policy binds it into the seal


def test_changed_grants_change_the_policy_digest_and_fingerprint():
    p1 = MissionPolicy(id="p", grants=("read:market", "read:portfolio"))
    p2 = MissionPolicy(id="p", grants=("read:market",))            # a grant revoked
    assert p1.digest() != p2.digest()                             # the pinned authority changed
    fp1 = plan_fingerprint("a->b", security=f"policy={p1.digest()};grants=read:market,read:portfolio")
    fp2 = plan_fingerprint("a->b", security=f"policy={p2.digest()};grants=read:market")
    assert fp1 != fp2                                             # → the sealed plan cannot be reused


def test_changed_policy_version_changes_the_fingerprint():
    v1 = MissionPolicy(id="p", version="1", grants=("read:market",))
    v2 = MissionPolicy(id="p", version="2", grants=("read:market",))
    assert v1.digest() != v2.digest()
    assert plan_fingerprint("a->b", security=f"policy={v1.digest()}") != \
        plan_fingerprint("a->b", security=f"policy={v2.digest()}")
