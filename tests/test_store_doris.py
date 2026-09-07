"""DorisCaseStore — writes/reads/vector-search over the car_diagnosis Doris schema.

All SQL goes through an injectable executor, so these run offline with no Doris. A live smoke against
the real cluster runs only when CAR_DORIS_HOST is set.
"""
from __future__ import annotations

import os

import pytest

from runtime_contracts import Channel, InteractionEvent, Modality
from agentic_os.automotive import (
    Diagnosis,
    DiagnosticObservation,
    DorisCaseStore,
    Hypothesis,
    Powertrain,
    RepairEvidence,
    VehicleRef,
)


class _FakeExec:
    def __init__(self, ret=None):
        self.calls = []      # (sql, params)
        self._ret = ret or []
    def __call__(self, sql, params):
        self.calls.append((sql, list(params)))
        return self._ret
    def last(self):
        return self.calls[-1]


def _store(ret=None):
    fx = _FakeExec(ret)
    return DorisCaseStore(execute=fx), fx


def test_upsert_case_targets_cases_with_vehicle_fields():
    s, fx = _store()
    s.upsert_case("c1", VehicleRef(vin="VIN1", make="Honda", model="Accord", year="2019",
                                   powertrain=Powertrain.ICE), participant_ref="wa:+15551234")
    sql, params = fx.last()
    assert "INSERT INTO car_diagnosis.cases" in sql
    assert params[:8] == ["c1", "wa:+15551234", "VIN1", "Honda", "Accord", "2019", "ice", "open"]


def test_append_event_derives_partition_date_and_maps_fields():
    s, fx = _store()
    ev = InteractionEvent(interaction_id="i1", conversation_id="c1", channel=Channel.WHATSAPP,
                          modality=Modality.AUDIO, text="it shakes at idle", provenance="grok-asr",
                          timestamp="2026-09-06T10:11:12Z")
    s.append_event(ev)
    sql, params = fx.last()
    assert "INSERT INTO car_diagnosis.interaction_events" in sql
    assert params[0] == "2026-09-06"                       # dt partition key from timestamp
    assert params[1] == "c1" and params[4] == "whatsapp" and params[5] == "audio"
    assert params[6] == "it shakes at idle"


def test_record_observation_uses_evidence_id():
    s, fx = _store()
    obs = DiagnosticObservation(kind="dtc", code="P0302", source="obd",
                                observed_at="2026-09-06T00:00:00Z")
    s.record_observation("c1", obs)
    sql, params = fx.last()
    assert "INSERT INTO car_diagnosis.observations" in sql
    assert params[0] == "c1" and params[1] == obs.evidence_id and params[3] == "P0302"


def test_record_diagnosis_flattens_top_and_serializes_hypotheses():
    s, fx = _store()
    d = Diagnosis(case_id="c1", created_at="2026-09-06T10:00:00Z", safety_gate_passed=True,
                  needs_more_evidence=False,
                  hypotheses=(Hypothesis(cause="cyl2 misfire", confidence="0.78", system="engine"),
                              Hypothesis(cause="injector", confidence="0.14")))
    s.record_diagnosis(d)
    sql, params = fx.last()
    assert "INSERT INTO car_diagnosis.diagnoses" in sql
    assert params[0] == "2026-09-06"                       # dt
    assert params[3] == "cyl2 misfire" and abs(params[4] - 0.78) < 1e-9   # top cause+confidence
    assert params[5] is True and params[6] is False
    assert "cyl2 misfire" in params[7] and "injector" in params[7]        # hypotheses_json


def test_record_outcome_numeric_coercion():
    s, fx = _store()
    s.record_outcome("o1", "c1", "d1", RepairEvidence(diagnosis="misfire", procedure="replace coil",
                                                      part="ignition coil", cost="180", labor_hours="0.8",
                                                      fixed=True, provenance="shop"))
    sql, params = fx.last()
    assert "INSERT INTO car_diagnosis.outcomes" in sql
    assert params[4] == "replace coil" and abs(params[7] - 180.0) < 1e-9 and params[8] is True


def test_knowledge_search_builds_ann_query_with_vector_literal():
    rows = [{"doc_id": "tsb-1", "title": "Misfire TSB", "dist": 0.12}]
    s, fx = _store(rows)
    out = s.knowledge_search([0.1, 0.2, 0.3], k=3, dtc="P0302")
    sql, params = fx.last()
    assert "l2_distance_approximate(embedding,[0.100000,0.200000,0.300000])" in sql
    assert "ORDER BY dist LIMIT 3" in sql and "WHERE dtc=%s" in sql and params == ["P0302"]
    assert out == rows


def test_timeline_orders_by_ts():
    s, fx = _store([{"ts": "t", "text": "hi"}])
    s.timeline("c1")
    sql, params = fx.last()
    assert "FROM car_diagnosis.interaction_events" in sql and "ORDER BY ts" in sql and params == ["c1"]


def test_outcome_stats_groups_fixed_repairs():
    s, fx = _store([{"repair_procedure": "replace coil", "fixed_count": 63, "avg_cost": 175.0}])
    out = s.outcome_stats(make="Honda", model="Accord")
    sql, params = fx.last()
    assert "o.fixed = TRUE" in sql and "GROUP BY o.repair_procedure" in sql
    assert params == ["Honda", "Accord"] and out[0]["fixed_count"] == 63


@pytest.mark.skipif(not os.environ.get("CAR_DORIS_HOST"), reason="needs live Doris (CAR_DORIS_HOST)")
def test_live_doris_roundtrip():
    s = DorisCaseStore()
    s.upsert_case("smoke-1", VehicleRef(vin="SMOKEVIN", make="Test", model="Smoke", year="2026"))
    assert isinstance(s.timeline("smoke-1"), list)
