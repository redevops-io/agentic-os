"""The apps' append-only writers (the Linux merge gate has apps/ on the path; the native-core lane installs no app)."""
from __future__ import annotations

import pytest

from tests.test_append_writers import K, N, _hammer


def test_the_billing_observation_store_keeps_every_observation(tmp_path):
    observe = pytest.importorskip("billing.observe")                    # an app: needs apps/ on the path (the Linux gate has it)
    from agentic_os.observation import Observation
    store = observe.FileObservationStore(str(tmp_path / "obs" / "o.jsonl"))
    _hammer(lambda i, j: store.append(Observation(f"o-{i}-{j}", "billing", "invoice.overdue", f"c{i}", 1.0, 2.0)))
    assert sorted(o.observation_id for o in store.all()) == sorted(f"o-{i}-{j}" for i in range(N) for j in range(K))
    assert b"\r" not in open(store.path, "rb").read()
