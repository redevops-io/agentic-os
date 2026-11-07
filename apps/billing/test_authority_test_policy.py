"""The apps' shared test policy (apps/conftest.py) is a NAMED, recorded declaration, not an ambient allowance. The billing closed loop's
tests approve a parked mission without an authenticated principal; that is accepted only because the policy declares it, and the
approval record says so. Without the policy the same approval is refused and nothing reaches the billing system."""
from __future__ import annotations

import pytest

from agentic_os.mission.principal import PrincipalRequired
from agentic_os.mission.types import MissionState

from billing import decision
from billing.test_closed_loop import _decide, lago, loop  # noqa: F401  (fixtures)


def test_the_apps_default_policy_is_named_and_the_approval_it_waives_is_recorded_as_such(loop, lago):
    assert loop.runtime.authority_posture()["test_policy"]["name"] == "pytest-apps-default"
    cyc = _decide(loop)
    out = decision.approve_mission(loop, cyc.mission_id)
    assert out["state"] == "succeeded" and sorted(lago.retries) == ["a1", "a2"]
    granted = [e for e in loop.runtime.store.for_mission(cyc.mission_id) if e.type == "ApprovalGranted"]
    assert granted and all(e.payload.get("basis") == "test_policy:pytest-apps-default" and e.payload.get("authenticated") is False
                           for e in granted)


def test_without_the_test_policy_an_anonymous_approval_is_refused_and_nothing_is_settled(no_test_policy, loop, lago):
    assert loop.runtime.authority_posture()["test_policy"] is None
    cyc = _decide(loop)
    with pytest.raises(PrincipalRequired):
        decision.approve_mission(loop, cyc.mission_id)
    assert lago.retries == [], "no authenticated principal and no named test policy: nothing may reach the billing system"
    assert loop.runtime.repo.state(cyc.mission_id) is MissionState.WAITING_HUMAN
