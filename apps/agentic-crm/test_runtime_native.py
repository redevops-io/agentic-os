"""agentic-crm runtime-native migration: conformant manifest, no phantom capabilities, decision spine.

Run from the repo root with the apps dir on the path (hyphenated package):
    PYTHONPATH=apps python -m pytest apps/agentic-crm/test_runtime_native.py
"""
from __future__ import annotations

import importlib

from agentic_os.app_kit import AppRegistry, BridgeOutcome, bridge_selection
from agentic_os.priority_engine import select_action

# Hyphenated package dir → importlib (a plain `import` can't spell it).
manifest_mod = importlib.import_module("agentic-crm.manifest")
producers = importlib.import_module("agentic-crm.producers")
operator_mod = importlib.import_module("agentic-crm.operator")


def _registered_caps():
    op = operator_mod.build_crm_operator()
    return {c.name for c in op.manifest.capabilities}


class _Store:
    def __init__(self):
        self.records = []

    def append(self, r):
        self.records.append(r)


class _Launcher:
    def __init__(self):
        self.requests = []

    def launch(self, request):
        self.requests.append(request)
        return "m-crm-1"


def test_manifest_registers_conformantly():
    reg = AppRegistry()
    app = manifest_mod.register(reg)        # builds the real operator + registers; raises if non-conformant
    assert app.manifest.name == "agentic-crm"
    assert "agentic-crm" in reg
    # all four capabilities are declared and backed by a verifier
    assert app.manifest.capability_names() == {"crm.score_lead", "crm.research",
                                               "crm.draft_outreach", "crm.qualify"}
    assert app.manifest.verified_capabilities() == app.manifest.capability_names()


def test_producer_emits_no_phantom_capabilities():
    registered = _registered_caps()
    for cap in producers.EMITTED_CAPABILITIES:
        assert cap in registered, f"producer emits phantom capability {cap}"
    opp = producers.crm_opportunity(producers.DealSignals(
        account="ACME", stage="engaged", buying_signal=True, open_technical_question=True))
    # every remapped candidate targets a real capability (monitor was dropped)
    assert opp.candidate_actions
    for cand in opp.candidate_actions:
        assert all(rc in registered for rc in cand.required_capabilities)
        assert "monitor" != cand.action_kind


def test_decision_spine_authors_a_mission_for_a_real_capability():
    opp = producers.crm_opportunity(producers.DealSignals(
        account="ACME", stage="engaged", buying_signal=True))
    sel = select_action(opp)
    store, launcher = _Store(), _Launcher()
    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)

    assert len(store.records) == 1                      # the decision is recorded (N1)
    if res.outcome in (BridgeOutcome.LAUNCHED, BridgeOutcome.LAUNCHED_GATED):
        assert res.request.capability in _registered_caps()   # authored against a real capability (N2)
        assert res.mission_id == "m-crm-1"
