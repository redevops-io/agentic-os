"""social-autopilot runtime-native: conformant manifest, no phantom capabilities, decision spine.

Run: PYTHONPATH=apps python -m pytest apps/social-autopilot/test_runtime_native.py
"""
from __future__ import annotations

import importlib

from agentic_os.app_kit import AppRegistry, BridgeOutcome, bridge_selection
from agentic_os.priority_engine import select_action

_manifest = importlib.import_module("social-autopilot.manifest")
_producers = importlib.import_module("social-autopilot.producers")
_operator = importlib.import_module("social-autopilot.operator")


def _registered_caps():
    return {c.name for c in _operator.build_social_operator().manifest.capabilities}


class _Store:
    def __init__(self): self.records = []
    def append(self, r): self.records.append(r)


class _Launcher:
    def launch(self, request): return "m-1"


def test_registers_conformantly():
    reg = AppRegistry()
    app = _manifest.register(reg)
    assert app.manifest.name == "social-autopilot"
    assert app.manifest.producers
    assert app.manifest.verified_capabilities() <= app.manifest.capability_names()


def test_producer_emits_no_phantom_capabilities():
    registered = _registered_caps()
    for cap in _producers.EMITTED_CAPABILITIES:
        assert cap in registered
    opp = _producers.social_opportunity(_producers.SocialSignals(channel="x", slot_due=True, draft_ready=True))
    for cand in opp.candidate_actions:
        assert all(rc in registered for rc in cand.required_capabilities)


def test_decision_spine_authors_a_mission():
    opp = _producers.social_opportunity(_producers.SocialSignals(channel="x", draft_ready=True))
    sel = select_action(opp)
    store = _Store()
    res = bridge_selection(sel, store=store, policy_version="v", launcher=_Launcher())
    assert len(store.records) == 1
    if res.outcome in (BridgeOutcome.LAUNCHED, BridgeOutcome.LAUNCHED_GATED):
        assert res.request.capability in _registered_caps()
