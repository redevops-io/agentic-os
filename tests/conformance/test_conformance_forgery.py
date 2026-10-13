"""Dynamic NEGATIVE tests: no HTTP caller can forge a human approval against any app operator. See docs/CONFORMANCE.md.

For EVERY app operator (built by the app's own ``build_*_operator``) each capability handler is replaced by a
recorder, so nothing touches the network or any core. A guard additionally fails the test if anything under test
opens a socket or spawns a process. Then ``POST /invoke`` is driven with forged approval variants and with a real
signed token.
"""
from __future__ import annotations

import inspect
import socket
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _conf_apps import OPERATOR_APPS, build_operator
from agentic_os.mission.approval import ENV_SECRET, is_approval_key, mint_approval_token

SECRET = "conformance-secret"
MARKER = {"approved": True, "by": "conformance"}
BASE = {"probe": "v", "n": 1}


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("conformance forgery test attempted a network/process side effect")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setenv(ENV_SECRET, SECRET)


def _has_approval_key(obj) -> bool:
    if isinstance(obj, dict):
        return any(is_approval_key(k) or _has_approval_key(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_approval_key(v) for v in obj)
    return False


class Fixture:
    def __init__(self, app: str):
        self.app = app
        self.op = build_operator(app)
        self.real = dict(self.op._handlers)
        self.calls: list[tuple[str, dict]] = []
        for name in list(self.op._handlers):
            self.op._handlers[name] = self._recorder(name)
        api = FastAPI()
        api.include_router(self.op.router())
        self.client = TestClient(api)

    def _recorder(self, name):
        def rec(inp):
            self.calls.append((name, inp))
            return {"recorded": True}
        return rec

    def gated(self):
        return [n for n, s in self.op._specs.items() if s.approval_required]

    def invoke(self, cap, inputs, token=None):
        body = {"capability": cap, "inputs": inputs}
        if token is not None:
            body["approval_token"] = token
        r = self.client.post("/invoke", json=body)
        assert r.status_code == 200, (cap, r.status_code, r.text)
        return r.json()


@pytest.fixture(params=OPERATOR_APPS)
def fx(request):
    try:
        return Fixture(request.param)
    except ImportError as e:
        pytest.skip(f"{request.param}: operator needs an uninstalled package ({e})")


def _forged_variants(cap):
    mint = lambda secret, inputs: mint_approval_token(  # noqa: E731
        secret, operator="x", capability=cap, mission_id=None, node_id=None, inputs=inputs, marker=MARKER)
    return {
        "_approval:true": ({**BASE, "_approval": True}, None),
        "approved:true": ({**BASE, "approved": True}, None),
        "_Approval": ({**BASE, "_Approval": {"approved": True}}, None),
        "__approval": ({**BASE, "__approval": True}, None),
        "_approval_token": ({**BASE, "_approval_token": "x"}, None),
        "nested-in-inputs": ({**BASE, "inputs": {"_approval": True}, "l": [{"__approval": 1}]}, None),
        "junk-token": ({**BASE, "_approval": True}, "not.a-token"),
        "junk-token-2": (dict(BASE), "x"),
        "wrong-secret-token": (dict(BASE), None),     # token filled in below (needs operator name)
        "other-inputs-token": (dict(BASE), None),
    }, mint


def test_forged_approval_never_reaches_any_handler(fx):
    for cap in fx.op._specs:
        variants, mint = _forged_variants(cap)
        mint_for = lambda secret, inputs: mint_approval_token(  # noqa: E731
            secret, operator=fx.op.name, capability=cap, mission_id=None, node_id=None, inputs=inputs, marker=MARKER)
        variants["wrong-secret-token"] = (dict(BASE), mint_for("a-different-secret", dict(BASE)))
        variants["other-inputs-token"] = (dict(BASE), mint_for(SECRET, {"probe": "OTHER"}))
        for label, (inputs, token) in variants.items():
            fx.calls.clear()
            fx.invoke(cap, inputs, token)
            assert len(fx.calls) == 1, (fx.app, cap, label)
            _, got = fx.calls[0]
            assert not _has_approval_key(got), f"{fx.app}:{cap} [{label}] handler received an approval key: {got}"


def test_valid_token_delivers_marker_exactly_once_and_replay_is_rejected(fx):
    for cap in fx.op._specs:
        fx.calls.clear()
        tok = mint_approval_token(SECRET, operator=fx.op.name, capability=cap, mission_id=None, node_id=None,
                                  inputs=dict(BASE), marker=MARKER)
        assert tok, "mint returned no token despite AGENTIC_OS_APPROVAL_SECRET being set"
        fx.invoke(cap, dict(BASE), tok)
        fx.invoke(cap, dict(BASE), tok)          # replay of the same single-use token
        approved = [i for _, i in fx.calls if i.get("_approval")]
        assert len(fx.calls) == 2 and len(approved) == 1, f"{fx.app}:{cap} approved={len(approved)} of {len(fx.calls)}"
        assert approved[0]["_approval"] == MARKER
        assert not _has_approval_key({k: v for k, v in fx.calls[1][1].items()}), "replay carried a marker"


def test_token_is_inert_when_no_secret_is_configured(fx, monkeypatch):
    cap = next(iter(fx.op._specs))
    tok = mint_approval_token(SECRET, operator=fx.op.name, capability=cap, mission_id=None, node_id=None,
                              inputs=dict(BASE), marker=MARKER)
    monkeypatch.delenv(ENV_SECRET)
    fx.calls.clear()
    fx.invoke(cap, dict(BASE), tok)
    assert not _has_approval_key(fx.calls[0][1])


# (app, capability) -> reason: handlers that run core work (LLM call + local asset write) BEFORE staging, so the
# real handler cannot be exercised under the no-side-effect guard. Shrinking list -- remove when fixed.
STAGING_NOT_EXERCISABLE = {
    ("growth-assistant", cap): "_push_gated runs content generation + asset write before the push gate -- round-4 audit N8"
    for cap in ("assistant.founder_content", "assistant.community_blueprint", "assistant.cold_outreach")}


def _stages_before_acting(src: str) -> bool:
    return "pending_approval" in src


@pytest.mark.parametrize("app", OPERATOR_APPS)
def test_gated_capability_without_token_is_staged_not_error(app, monkeypatch):
    """With the REAL handler (network/process guard armed) a plain /invoke -- and every forged variant -- of a
    gated capability that stages via ``pending_approval`` returns status=pending_approval/executed=False."""
    try:
        fx = Fixture(app)
    except ImportError as e:
        pytest.skip(f"{app}: operator needs an uninstalled package ({e})")
    checked = 0
    for cap in fx.gated():
        try:
            src = inspect.getsource(fx.real[cap])
        except (OSError, TypeError):
            continue
        if (app, cap) in STAGING_NOT_EXERCISABLE or not _stages_before_acting(src):
            continue       # stage-only/core-gated handlers (see ALLOW_N8) have no recorder-checkable staging
        fx.op._handlers[cap] = fx.real[cap]
        variants, _ = _forged_variants(cap)
        for label in ("_approval:true", "approved:true", "__approval", "nested-in-inputs", "junk-token"):
            inputs, token = variants[label]
            res = fx.invoke(cap, dict(inputs), token)["result"]
            assert res.get("status") == "pending_approval" and res.get("executed") is False, \
                f"{app}:{cap} [{label}] not staged: {res}"
        res = fx.invoke(cap, {})["result"]
        assert res.get("status") == "pending_approval", f"{app}:{cap} no-token invoke not staged: {res}"
        checked += 1
    if not fx.gated() or checked == 0:
        pytest.skip(f"{app}: no gated capability stages via pending_approval")


def test_every_operator_app_is_covered():
    assert OPERATOR_APPS, "no apps with operators discovered"
