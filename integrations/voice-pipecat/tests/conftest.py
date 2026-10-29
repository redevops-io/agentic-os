"""Shared fixtures. Every test runs with the approval secret unset and in a throwaway directory."""
from __future__ import annotations

import importlib.util
import os
import sys
import time

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
for var in ("APPROVAL_SECRET", "AGENTIC_OS_APPROVAL_SECRET", "AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY"):
    os.environ.pop(var, None)
os.environ["AGENTIC_OS_EVENT_FSYNC"] = os.environ.get("AGENTIC_OS_EVENT_FSYNC", "1")


def _load_fork_support():
    """The repository's own deterministic crash-injection store (tests/_fork_support.py), loaded by path so this suite does
    not depend on how `tests` resolves as a package."""
    path = os.path.join(REPO, "tests", "_fork_support.py")
    spec = importlib.util.spec_from_file_location("_fork_support_shared", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fork_support_shared"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def fork_support():
    return _load_fork_support()


class Clock:
    def __init__(self, t: float = 2_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fixture_call():
    from agentic_os_voice.demo.call_simulator import load_fixture
    return load_fixture("inbound_lead_call.json")


@pytest.fixture
def env(tmp_path, clock):
    from agentic_os_voice.demo.run import build_env
    return build_env(str(tmp_path), clock=clock)


@pytest.fixture
def make_session():
    """open a session on an env: make_session(env, sid, identity=..., accounts=(...), tenant=None, caller_id='')"""
    from agentic_os_voice.contracts import IdentityLevel, SessionBinding
    from agentic_os_voice.demo.run import SUBJECT

    def make(env, sid="call-1", identity=IdentityLevel.VERIFIED, accounts=("lead_dana01",), tenant=None, caller_id="+15550100", ttl=600.0):
        return env.bridge.open_session(SessionBinding(
            session_id=sid, tenant=tenant or env.tenant, expires_at=env.clock() + ttl, identity=identity,
            verified_accounts=tuple(accounts) if identity >= IdentityLevel.VERIFIED else (), caller_id=caller_id, subject=SUBJECT))
    return make


UPDATE_ARGS = {"lead_id": "lead_dana01", "status": "qualified", "budget_band": "10k_50k", "timeline": "this_quarter"}
SEND_ARGS = {"contact": "lead_dana01", "body": "Hi Dana, here is the link we discussed."}
