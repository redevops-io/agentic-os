"""ELM327 / OBD-II ingestion (P3) — protocol + decoders, offline against recorded adapter responses."""
from __future__ import annotations

from agentic_os.automotive import (
    ELM327Client,
    DiagnosisService,
    Diagnoser,
    OBDSnapshot,
    decode_dtc,
    observations_from_snapshot,
    parse_dtcs,
    parse_pid,
    parse_vin,
)


def test_decode_dtc():
    assert decode_dtc(0x03, 0x02) == "P0302"     # cylinder-2 misfire
    assert decode_dtc(0x01, 0x71) == "P0171"     # system too lean
    assert decode_dtc(0xC1, 0x23) == "U0123"     # network


def test_parse_dtcs_mode03():
    assert parse_dtcs("43 03 02 01 71\r\r>") == ["P0302", "P0171"]
    assert parse_dtcs("SEARCHING...\r43 00 00\r>") == []      # no codes
    assert parse_dtcs("NO DATA\r>") == []


def test_parse_pids():
    assert parse_pid("41 0C 1A F8\r>", "0C") == 1726.0        # RPM = (0x1AF8)/4
    assert parse_pid("41 05 5A\r>", "05") == 50               # coolant 0x5A-40 = 50C
    assert parse_pid("41 0D 00\r>", "0D") == 0                # speed
    assert parse_pid("NO DATA\r>", "0C") is None


def test_parse_vin():
    resp = "49 02 01 31 47 31 4A 43 35 34 34 34 52 37 32 35 32 33 36 37\r>"
    assert parse_vin(resp) == "1G1JC5444R7252367"


class _FakeELM:
    """Canned ELM327 responses keyed by command."""
    def __init__(self, responses):
        self.responses = responses
        self.sent = []
    def __call__(self, command):
        self.sent.append(command)
        return self.responses.get(command, "NO DATA\r>")


def _client():
    return ELM327Client(_FakeELM({
        "03": "43 03 02\r>",
        "0902": "49 02 01 31 47 31 4A 43 35 34 34 34 52 37 32 35 32 33 36 37\r>",
        "010C": "41 0C 0B B8\r>",   # 750 rpm
        "0105": "41 05 5A\r>",      # 50 C
        "010D": "41 0D 00\r>",      # 0 km/h idle
        "0104": "41 04 40\r>", "0111": "41 11 20\r>", "012F": "41 2F 80\r>",
        "0142": "41 42 39 D0\r>",   # 14.8 V
    }))


def test_client_connect_and_snapshot():
    c = _client()
    c.connect()
    snap = c.snapshot()
    assert snap.dtcs == ["P0302"]
    assert snap.vin == "1G1JC5444R7252367"
    assert snap.pids["rpm"] == 750.0 and snap.pids["coolant_temp"] == 50
    assert snap.pids["control_module_voltage"] == 14.8


def test_observations_from_snapshot():
    snap = OBDSnapshot(dtcs=["P0302"], pids={"rpm": 750.0, "coolant_temp": 50}, vin="X")
    obs = observations_from_snapshot(snap, observed_at="2026-09-07T00:00:00Z")
    kinds = {(o.kind, o.code) for o in obs}
    assert ("dtc", "P0302") in kinds and ("pid", "rpm") in kinds
    dtc = next(o for o in obs if o.kind == "dtc")
    assert dtc.source == "obd" and dtc.evidence_id.startswith("rcv1:")


def test_service_ingest_obd_folds_into_case_and_decodes_vin():
    import json
    ch_sent = []
    class _Ch:
        def send_text(self, cid, t): ch_sent.append((cid, t)); return type("R", (), {"status": "sent"})()
        def poll(self, *, timeout=0): return []
    svc = DiagnosisService(channel=_Ch(),
                           diagnoser=Diagnoser(llm=lambda s, u: json.dumps({"hypotheses": [
                               {"cause": "cyl2 misfire", "confidence": 0.85, "system": "engine"}]})),
                           vin_decoder=lambda vin: __import__("agentic_os.automotive", fromlist=["VehicleRef"]).VehicleRef(vin=vin, make="Chevrolet"))
    case = svc.ingest_obd("tg:9", _client())
    assert any(o.code == "P0302" for o in case.observations)     # DTC folded in
    assert any(o.kind == "pid" and o.code == "rpm" for o in case.observations)
    assert case.vehicle.vin == "1G1JC5444R7252367"               # VIN from OBD decoded
    # the diagnosis now reflects the OBD evidence
    reply = svc.diagnose_case(case)
    assert "misfire" in reply.lower()
