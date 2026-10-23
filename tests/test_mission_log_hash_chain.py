"""The mission event log is tamper-evident: per-mission hash chain, verified on demand (audit C5)."""
import json

from agentic_os.mission.store import EventStore, MemoryEventStore, verify_chain


def _fill(store):
    store.append("MissionCreated", "m1", {"goal": "pay"})
    store.append("MissionCreated", "m2", {"goal": "other"})
    store.append("NodeDispatched", "m1", {"node_id": "n1"})
    store.append("NodeSucceeded", "m1", {"node_id": "n1", "result": {"amount": 100}})


def test_a_clean_log_verifies_and_chains_per_mission():
    for store in (MemoryEventStore(), EventStore()):
        _fill(store)
        rep = store.verify()
        assert rep.ok and rep.checked == 4 and rep.unchained == 0
        m1 = store.for_mission("m1")
        assert m1[0].prev == "" and m1[1].prev == m1[0].hash and m1[2].prev == m1[1].hash
        assert store.for_mission("m2")[0].prev == ""                    # a separate chain per mission
        assert store.head("m1") == m1[-1].hash


def test_editing_an_event_is_detected(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(EventStore(str(path)))
    lines = path.read_text().splitlines()
    rec = json.loads(lines[3])
    rec["payload"]["result"]["amount"] = 1                                # rewrite history
    lines[3] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    rep = EventStore(str(path)).verify()
    assert not rep.ok and rep.first_bad_seq == 4 and "modified" in rep.reason


def test_deleting_or_reordering_an_event_is_detected(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(EventStore(str(path)))
    lines = path.read_text().splitlines()
    del lines[2]                                                          # drop NodeDispatched
    path.write_text("\n".join(lines) + "\n")
    rep = EventStore(str(path)).verify()
    assert not rep.ok and "missing" in rep.reason


def test_the_chain_survives_a_restart_and_keeps_extending(tmp_path):
    path = str(tmp_path / "log.jsonl")
    _fill(EventStore(path))
    again = EventStore(path)
    head = again.head("m1")
    ev = again.append("NodeVerified", "m1", {"node_id": "n1"})
    assert ev.prev == head
    assert EventStore(path).verify().ok


def test_legacy_unchained_records_are_counted_not_failed(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps({"seq": 1, "ts": 1.0, "type": "MissionCreated", "mission_id": "m1",
                                "payload": {}}) + "\n")
    store = EventStore(str(path))
    store.append("NodeDispatched", "m1", {"node_id": "n1"})
    rep = store.verify()
    assert rep.ok and rep.unchained == 1 and rep.checked == 1


def test_mission_receipt_exposes_the_head_and_chain_status():
    from tests.test_mission_inputs_delivery_and_explicit_planning import _op, _rt, _req
    from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
    from agentic_os.mission.receipt import mission_receipt
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req())
    rt.run(mid)
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve", actor="approver:acme")
    r = mission_receipt(rt, mid)
    assert r["ledger_chain"]["ok"] and r["ledger_head"] == rt.store.head(mid) != ""
    assert r["approvals"][0]["approver"] == "approver:acme"


def test_events_from_a_store_that_predates_chaining_are_unchained_not_an_error():
    """The enterprise overlay ships its own fork of mission/store.py whose Event has no hash/prev at all: receipts and
    assurance must not crash on it."""
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class OldEvent:
        type: str
        mission_id: str
        payload: dict[str, Any]
        seq: int = 0
        ts: float = 0.0

    rep = verify_chain([OldEvent("MissionCreated", "m", {}, 1), OldEvent("NodeSucceeded", "m", {}, 2)])
    assert rep.ok and rep.checked == 0 and rep.unchained == 2


def test_receipt_and_assurance_tolerate_an_old_style_store():
    from agentic_os.mission.assurance import assurance_findings
    from tests.test_mission_inputs_delivery_and_explicit_planning import _op, _rt, _req
    from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
    from agentic_os.mission.receipt import mission_receipt
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req())
    rt.run(mid)
    for e in rt.store.all():                      # simulate events from a store without chaining
        e.hash = ""
        del e.hash
        del e.prev
    r = mission_receipt(rt, mid)
    assert r["ledger_chain"]["ok"] and r["ledger_chain"]["checked"] == 0
    assert isinstance(assurance_findings(rt.store.all(), now=1e12), list)
