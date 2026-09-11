"""Tests for durable outcome storage (agentic_os.outcome_store) and the provider persisting the
learning loop across a restart — so learned selection survives, replayably and auditably."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentic_os.priority_engine import Action, OutcomeEvent
from agentic_os.outcome_store import (
    FileOutcomeStore, InMemoryOutcomeStore, event_from_dict, event_to_dict, load_outcome_log,
    open_outcome_store)
from agentic_os.projects_api import SampleProjectionProvider, create_app


def _ev(kind="send", reward=1.0, action=Action.ACT):
    return OutcomeEvent(candidate_id=f"crm:{kind}", source_app="crm", action=action, action_kind=kind,
                        observed_reward=reward, reward_dimensions={"reply": 1.0}, delay=12.0,
                        attribution_confidence=0.9)


# ── serialisation ─────────────────────────────────────────────────────────────────────
def test_event_roundtrips_through_dict_including_none_action():
    for ev in (_ev(), _ev(action=None)):
        back = event_from_dict(event_to_dict(ev))
        assert back == ev


# ── file store ────────────────────────────────────────────────────────────────────────
def test_file_store_appends_and_reloads(tmp_path):
    store = FileOutcomeStore(str(tmp_path / "sub" / "outcomes.jsonl"))   # nested dir created on demand
    store.append(_ev("send", 1.0))
    store.append(_ev("wait", -0.2))
    reloaded = FileOutcomeStore(str(tmp_path / "sub" / "outcomes.jsonl")).load()   # a fresh handle
    assert [e.action_kind for e in reloaded] == ["send", "wait"]
    assert reloaded[0].reward_dimensions == {"reply": 1.0} and reloaded[1].observed_reward == -0.2


def test_file_store_skips_a_corrupt_tail_line(tmp_path):
    p = tmp_path / "o.jsonl"
    store = FileOutcomeStore(str(p))
    store.append(_ev("send", 1.0))
    with open(p, "a", encoding="utf-8") as f:
        f.write("{ this is a partial/corrupt line\n")     # a crash mid-append
    loaded = store.load()
    assert len(loaded) == 1 and loaded[0].action_kind == "send"   # good line survives, bad one skipped


def test_open_outcome_store_selects_by_path(tmp_path, monkeypatch):
    assert isinstance(open_outcome_store(str(tmp_path / "x.jsonl")), FileOutcomeStore)
    monkeypatch.delenv("OUTCOME_STORE_PATH", raising=False)
    assert isinstance(open_outcome_store(), InMemoryOutcomeStore)


def test_load_outcome_log_materialises_from_a_store():
    mem = InMemoryOutcomeStore()
    mem.append(_ev("send", 1.0))
    log = load_outcome_log(mem)
    assert len(log.events) == 1 and log.events[0].action_kind == "send"


# ── the provider persists the loop across a restart ─────────────────────────────────
def test_recorded_outcomes_persist_and_learned_selection_survives_restart(tmp_path):
    path = str(tmp_path / "outcomes.jsonl")

    def crm_choice(client):
        s = client.get("/api/projects/customer-ops/priorities").json()
        for it in s["surfaced"] + s["deferred"]:
            if it["source_app"] == "crm" and it["candidate_id"].startswith("crm:Acme:"):
                return it["action_kind"]
        return None

    # session 1: record outcomes that should shift CRM from its prior (send_proposal) to schedule_call
    c1 = TestClient(create_app(SampleProjectionProvider(persist_path=path)))
    assert crm_choice(c1) == "send_proposal"
    for _ in range(30):
        r = c1.post("/api/outcomes", json={"source_app": "crm", "action_kind": "send_proposal",
                                           "observed_reward": -0.6}).json()
        assert r["durable"] is True
        c1.post("/api/outcomes", json={"source_app": "crm", "action_kind": "schedule_call",
                                       "observed_reward": 0.9})
    assert crm_choice(c1) == "schedule_call"

    # a durable file now holds every outcome
    assert sum(1 for _ in open(path)) == 60

    # session 2: a BRAND NEW app/provider on the same path — learned selection is already there,
    # no re-teaching needed. This is persistence closing the loop across a restart.
    c2 = TestClient(create_app(SampleProjectionProvider(persist_path=path)))
    s2 = c2.get("/api/projects/customer-ops/priorities").json()
    assert s2["learning"]["outcomes_recorded"] == 60 and s2["learning"]["selection_adjusted"] is True
    assert crm_choice(c2) == "schedule_call"          # survived the restart


def test_in_memory_provider_is_not_durable(tmp_path):
    # no persist_path ⇒ a fresh provider starts empty (the original, non-durable behaviour)
    prov = SampleProjectionProvider()
    prov.record_outcome_event(source_app="crm", action_kind="send_proposal", observed_reward=-0.6)
    assert len(prov.outcome_events) == 1
    assert len(SampleProjectionProvider().outcome_events) == 0
