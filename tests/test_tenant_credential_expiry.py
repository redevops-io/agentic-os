"""A tenant credential can be given an end time that the server enforces by itself."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from agentic_os.mission import control_auth as ca

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

ADMIN, RUN, APPROVE, STEADY = "a" * 40, "r" * 40, "p" * 40, "s" * 40


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(ca.ENV_TOKEN, ADMIN)
    monkeypatch.setenv(ca.ENV_TENANT_TOKENS, f"run={RUN},steady={STEADY}")
    monkeypatch.setenv(ca.ENV_APPROVER_TOKENS, f"run={APPROVE}")

    def limits(**run):
        monkeypatch.setenv(ca.ENV_TENANT_LIMITS, json.dumps({"run": {"policy_refs": ["dashboards:qa"], **run}, "steady": {"policy_refs": []}}))
    return limits


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def refused(token):
    with pytest.raises(HTTPException) as e:
        ca.resolve_caller("Bearer " + token)
    return e.value.status_code


def test_a_credential_works_until_its_time_and_not_after(env):
    env(expires_at=stamp(timedelta(hours=1)))
    assert ca.resolve_caller("Bearer " + RUN).tenant == "run" and ca.resolve_caller("Bearer " + APPROVE).role == "approver"
    env(expires_at=stamp(timedelta(seconds=-1)))
    assert refused(RUN) == 401 and refused(APPROVE) == 401


def test_expiry_applies_without_a_restart_or_any_revocation_step(env, monkeypatch):
    until = 2_000_000_000
    env(expires_at=until)
    monkeypatch.setattr(ca.time, "time", lambda: until - 1)
    assert ca.resolve_caller("Bearer " + RUN).tenant == "run"
    monkeypatch.setattr(ca.time, "time", lambda: until)
    assert refused(RUN) == 401


def test_other_tenants_the_admin_and_entries_without_a_time_are_unaffected(env):
    env(expires_at=stamp(timedelta(days=-1)))
    assert ca.resolve_caller("Bearer " + STEADY).tenant == "steady" and ca.resolve_caller("Bearer " + ADMIN).role == "admin"
    env()
    assert ca.resolve_caller("Bearer " + RUN).tenant == "run"


@pytest.mark.parametrize("bad", ["tomorrow", "2030-01-01T00:00:00", "", None, True, [], {}, float("inf")])
def test_a_time_that_cannot_be_read_counts_as_expired(env, bad):
    env(expires_at=bad)
    assert refused(RUN) == 401


def test_expiry_never_switches_separation_of_duty_off(env):
    env(expires_at=stamp(timedelta(days=-1)))
    assert "run" in ca.approver_tokens() and "run" in ca.tenant_tokens()
