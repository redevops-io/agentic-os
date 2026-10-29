"""Bound the cost of the ACTION path (governance), measured apart from any speech path. Loose ceilings: a regression guard,
not a performance claim. The speech path (audio, speech-to-text, model, text-to-speech, network) is not measured here."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
from measure_action_path import measure  # noqa: E402


def test_in_memory_governance_overhead_is_bounded():
    r = measure(60, durable=False)
    for kind, s in r.items():
        assert s["p95_ms"] < 100.0 and s["max_ms"] < 1000.0, (kind, s)


def test_durable_governance_overhead_is_bounded_and_replays_and_refusals_are_cheaper_than_writes():
    r = measure(40, durable=True)
    assert r["write (new intent)"]["p95_ms"] < 1000.0, r
    assert r["refusal (not allowlisted)"]["p50_ms"] < r["write (new intent)"]["p50_ms"]
    assert r["replay (completed write asked again)"]["p50_ms"] < r["write (new intent)"]["p50_ms"]
