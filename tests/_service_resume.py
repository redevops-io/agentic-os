"""Restart-and-continue tests: the restarted process is a SERVICE with an explicit resume authority.

Since the replay boundary a rehydrated mission is read-only until a resume authorization exists, and unattended resume is never a
default (``MissionRuntime(service_resume=None)``). A test that simulates "the process died, a new one starts over the same durable
log and carries on" therefore says so explicitly with this authority; what it then asserts (no duplicate effect, the park holds,
the approval is honoured exactly once, ...) is unchanged. The ceiling is a plain in-code StaticCeiling: tests only.
"""
from __future__ import annotations

from agentic_os.evidence_query.authz import StaticCeiling
from agentic_os.mission.runtime import ServiceResume
from agentic_os.overlays import Principal

from tests._principals import authn  # noqa: E402

TEST_SERVICE = ServiceResume(authn(Principal("test-service", "service", ())), StaticCeiling({"evidence"}))


def authorize_service(rt):
    """Give a restarted runtime the explicit service resume authority and return it."""
    rt.service_resume = TEST_SERVICE
    return rt


def rehydrate_authorized(rt, mission_id, *args, **kw):
    """``rt.rehydrate(...)`` for a restarted service process that goes on to run the mission."""
    authorize_service(rt)
    return rt.rehydrate_for_service(mission_id, *args, **kw)
