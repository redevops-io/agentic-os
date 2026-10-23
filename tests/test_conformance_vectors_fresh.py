"""The shared conformance vectors are current and the kernel satisfies them (the reference side of port parity)."""
import json
import os
import subprocess
import sys

from agentic_os.mission.store import Event, entry_hash, verify_chain

ROOT = os.path.join(os.path.dirname(__file__), "..", "conformance")


def _load(name):
    with open(os.path.join(ROOT, "vectors", name), encoding="utf-8") as fh:
        return json.load(fh)


def test_committed_vectors_are_not_stale():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "gen_vectors.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_chain_vector_event_recomputes():
    for case in _load("chain.json")["cases"]:
        for e in case["events"]:
            assert entry_hash(e["prev"], e["seq"], e["ts"], e["type"], e["mission_id"], e["payload"]) == e["hash"], case["name"]


def test_tamper_vectors_report_what_the_kernel_reports():
    for case in _load("chain.json")["tamper"]:
        evs = [Event(type=e["type"], mission_id=e["mission_id"], payload=e["payload"], seq=e["seq"], ts=e["ts"],
                     prev=e["prev"], hash=e["hash"]) for e in case["events"]]
        r = verify_chain(evs)
        got = {"ok": r.ok, "checked": r.checked, "unchained": r.unchained, "first_bad_seq": r.first_bad_seq, "reason": r.reason}
        assert got == case["expect"], case["name"]
