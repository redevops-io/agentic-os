"""LIVE acceptance against the disposable loopback stack `aos-dd` (Chatwoot, Twenty, ERPNext, Lago). OPT-IN.

Skipped unless ``AOSDD_LIVE_CONFIG`` names an ``aosdd.config/1`` file (mode 0600) that lives in a ``secrets`` directory, i.e. the
credentials file written by the stack tooling. The run mutates the stack (a quotation, invoice metadata, a task, a private note) and
compensates; start it from a restored baseline snapshot. Synthetic data only; never point it at anything but the loopback stack
(the configuration loader refuses a non-loopback base URL)."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import uuid

import pytest

LIVE = os.environ.get("AOSDD_LIVE_CONFIG", "")
pytestmark = pytest.mark.skipif(not LIVE or "/secrets/" not in LIVE, reason="live acceptance is opt-in: set AOSDD_LIVE_CONFIG to the stack's aosdd config under secrets/")

from agentic_os.integrations.aosdd.config import AosddConfig                       # noqa: E402
from agentic_os.integrations.aosdd.http import UrllibTransport                     # noqa: E402
from agentic_os.integrations.aosdd.oplog import OpLog                              # noqa: E402
from agentic_os.integrations.aosdd.reconstruct import COMPENSATED, REVERSIBLE, reconstruct   # noqa: E402
from agentic_os.integrations.aosdd.runtime import Runner, RunnerError             # noqa: E402
from agentic_os.integrations.aosdd.services import RedactingEventStore, Services  # noqa: E402
from agentic_os.integrations.aosdd.verify import ReadBack                         # noqa: E402
from agentic_os.mission.authority import AuthorityGuard, SqliteEnvelopeStore, SqliteLedger   # noqa: E402
from agentic_os.mission.types import MissionState                                  # noqa: E402


@pytest.fixture
def live(tmp_path):
    cfg = AosddConfig.from_file(LIVE)
    d = str(tmp_path)
    svc = Services(cfg, RedactingEventStore(d + "/events.jsonl", cfg.redactor), OpLog(d + "/ops.db"),
                   AuthorityGuard(SqliteEnvelopeStore(d + "/auth.db"), SqliteLedger(d + "/auth.db.usage")), transport=UrllibTransport())
    return cfg, Runner(svc), d


def _quotation_count(cfg) -> int:
    rb = ReadBack(cfg, UrllibTransport())
    r = rb.c["erpnext"].get("/api/resource/Quotation?fields=%5B%22name%22%5D&limit_page_length=500")
    return len((r.body or {}).get("data") or [])


def test_live_positive_verify_reconstruct_replay_compensate(live, monkeypatch):
    cfg, r, d = live
    q0 = _quotation_count(cfg)
    case = "LIVE-" + uuid.uuid4().hex[:8]
    mid = r.submit(case, "AOSDD-HFC-001")
    assert r.rt.repo.state(mid) is MissionState.WAITING_HUMAN and _quotation_count(cfg) == q0         # parked at the gate: nothing written
    with pytest.raises(RunnerError):
        r.approve_plan(mid, "pytest", "0" * 64)
    assert _quotation_count(cfg) == q0
    r.approve_plan(mid, "pytest@example", r.plan(mid)["plan_sha256"])
    assert r.rt.repo.state(mid) is MissionState.SUCCEEDED
    rec = reconstruct(d + "/events.jsonl", mid)
    assert [s["classification"] for s in rec["steps"]] == [REVERSIBLE] * 4 and all(s["verification"]["status"] == "VERIFIED" for s in rec["steps"])
    assert len(rec["context_pack"]) == 9 and rec["ledger"]["mission_chain"]["ok"]
    # an independent reader (its own clients, reader credentials) sees exactly what the receipt claims
    rb = ReadBack(cfg, UrllibTransport())
    plan = rec["plan"]
    for s, st in zip(rec["steps"], plan["steps"]):
        assert rb.verify(st, {"native_id": s["native_id"], "native_ids": s["native_ids"]}, {"quotation": rec["steps"][0]["native_id"]})["status"] == "VERIFIED"
    # reconstruction touches no connector and no socket
    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert reconstruct(d + "/events.jsonl", mid) == rec
    monkeypatch.undo()
    # the same case again is the same mission; a retry of every step sends nothing
    assert r.submit(case, "AOSDD-HFC-001") == mid
    assert _quotation_count(cfg) == q0 + 1
    # operator compensation, verified by read-back, with the residual effects on record
    r.compensate(mid, "pytest@example", "live acceptance cleanup")
    rec = reconstruct(d + "/events.jsonl", mid)
    assert rec["mission_classification"] == COMPENSATED and rec["residual_effects"] and _quotation_count(cfg) == q0
    assert r.rt._missions[mid].outcome["rolled_back"] is False                                       # residual effects remain: not a clean rollback


def test_live_revoked_grant_sends_nothing(live):
    cfg, r, d = live
    q0 = _quotation_count(cfg)
    mid = r.submit("LIVE-" + uuid.uuid4().hex[:8], "AOSDD-HFC-001")
    r.revoke(mid, "pytest", "negative test")
    r.approve_plan(mid, "pytest@example", r.plan(mid)["plan_sha256"])
    assert _quotation_count(cfg) == q0
    rec = reconstruct(d + "/events.jsonl", mid)
    assert all(s["outcome"] == "not_attempted" for s in rec["steps"]) and rec["halts"][0]["rule"] == "authority_revoked"
