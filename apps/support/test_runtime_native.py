"""support runtime-native: conformant manifest, no phantom capabilities, decision spine authors a mission.

Run: PYTHONPATH=apps python -m pytest apps/support/test_runtime_native.py
"""
from __future__ import annotations

import importlib

from agentic_os.app_kit import AppRegistry, BridgeOutcome, bridge_selection
from agentic_os.priority_engine import select_action

_manifest = importlib.import_module("support.manifest")
_producers = importlib.import_module("support.producers")
_operator = importlib.import_module("support.operator")


def _registered_caps():
    op = _operator.build_support_operator()
    return {c.name for c in op.manifest.capabilities}


class _Store:
    def __init__(self): self.records = []
    def append(self, r): self.records.append(r)


class _Launcher:
    def __init__(self): self.requests = []
    def launch(self, request): self.requests.append(request); return "m-support-1"


def test_registers_conformantly():
    reg = AppRegistry()
    app = _manifest.register(reg)
    assert app.manifest.name == "support"
    assert app.manifest.producers                              # a decision producer is declared
    assert app.manifest.verified_capabilities() <= app.manifest.capability_names()


def test_producer_emits_no_phantom_capabilities():
    registered = _registered_caps()
    for cap in _producers.EMITTED_CAPABILITIES:
        assert cap in registered, f"phantom capability {cap}"
    opp = _producers.support_opportunity(_producers.SupportSignals(conversation='t-1', sla_breached=True))
    for cand in opp.candidate_actions:
        assert all(rc in registered for rc in cand.required_capabilities)


def test_decision_spine_authors_a_mission():
    opp = _producers.support_opportunity(_producers.SupportSignals(conversation='t-1', sla_breached=True))
    sel = select_action(opp)
    store, launcher = _Store(), _Launcher()
    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)
    assert len(store.records) == 1
    if res.outcome in (BridgeOutcome.LAUNCHED, BridgeOutcome.LAUNCHED_GATED):
        assert res.request.capability in _registered_caps()
