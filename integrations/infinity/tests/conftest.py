"""Shared fixtures. Every test runs with the approval secret unset, in a throwaway directory, on loopback only, with synthetic data."""
from __future__ import annotations

import importlib.util
import os
import sys
import time

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
for var in ("APPROVAL_SECRET", "AGENTIC_OS_APPROVAL_SECRET", "AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY"):
    os.environ.pop(var, None)
os.environ.setdefault("AGENTIC_OS_EVENT_FSYNC", "1")


@pytest.fixture(scope="session")
def fork_support():
    """The repository's deterministic crash-injection store (tests/_fork_support.py), loaded by path."""
    path = os.path.join(REPO, "tests", "_fork_support.py")
    spec = importlib.util.spec_from_file_location("_fork_support_shared", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fork_support_shared"] = mod
    spec.loader.exec_module(mod)
    return mod


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
def runtime_factory(tmp_path, clock):
    """build(env_kwargs) -> (env, runtime): a bridge over files in tmp_path, an HTTP server on loopback, and an emulated runtime peer."""
    from redevops_infinity import RapHttpServer
    from redevops_infinity.demo.env import AUDIENCE, KEY_ID, PRINCIPAL, SECRET, TENANT, build_env, loopback_origin
    from redevops_infinity.demo.peer import EmulatedRuntime
    made = []

    def build(workdir=None, **kw):
        wd = str(workdir or tmp_path)
        env = build_env(wd, clock=clock, **kw)
        srv = RapHttpServer(env.bridge).start()
        rt = EmulatedRuntime(srv.base_url, KEY_ID, SECRET, AUDIENCE, TENANT, clock=clock)
        env.peer.callback_origins = (loopback_origin(rt.cb_port),)
        made.append((srv, rt, env))
        return env, srv, rt

    yield build
    for srv, rt, env in made:
        srv.stop()
        rt.close()
        env.bridge._pool.shutdown(wait=False)


def drive(env, n=40, inline=True):
    """Run scheduler passes until nothing is due (deterministic; no background thread)."""
    for _ in range(n):
        if not env.bridge.tick(inline=inline):
            break


def payload(msg):
    """The JSON payload of a tool_result message."""
    text = msg["text"]
    return __import__("json").loads(text[len("Error: "):] if text.startswith("Error: ") else text)


def settle(env, rt, gid, cid, timeout=5.0):
    """Drive the bridge until the runtime has the result of (gid, cid); return its payload."""
    drive(env)
    msg = rt.await_result(gid, cid, timeout)
    assert msg is not None, "no result delivered"
    return payload(msg)


UPDATE = {"lead_id": "lead_dana01", "status": "qualified"}


class Boom(BaseException):
    """A simulated hard kill of the process: not an Exception, so nothing in the code under test may swallow it."""


def restart(env, rt, clock, **kw):
    """A new process over the same files and the same external systems (the CRM and the runtime survive a bridge restart)."""
    from redevops_infinity.demo.env import build_env, loopback_origin
    env.bridge._pool.shutdown(wait=False)
    env2 = build_env(env.workdir, crm=env.crm, clock=clock, callback_origins=(loopback_origin(rt.cb_port),), **kw)
    return env2


def key_of(env, gid, cid):
    return __import__("json").dumps([env.tenant, gid, cid], separators=(",", ":"))
