"""Apache Doris storage/analytics layer for the car-diagnosis service.

Doris (4.x, deployed on the proxmox k3s cluster, MySQL protocol) is the storage + analytics + vector
layer over the evidence→diagnosis→repair→outcome graph: append-only interaction/diagnosis history
(monthly-partitioned so cold months tier to an S3 datalake via a Doris storage policy), the outcome
flywheel for analytics ("what % of P0302 + rough-idle cases were coil failures"), and a vector ANN
``knowledge`` table for the NHTSA/MechanicDB evidence lake (RAG).

Dependency-light + testable: all SQL goes through one injectable ``execute(sql, params) -> rows``
callable, defaulting to a lazily-imported pymysql connection. Unit tests inject a fake executor and
assert the SQL/params; a live smoke runs only when ``CAR_DORIS_HOST`` is set.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contracts import Diagnosis, DiagnosticObservation, RepairEvidence, VehicleRef

# execute(sql, params) -> list of row-dicts (for SELECT) or None (for write)
Executor = Callable[[str, Sequence[Any]], Optional[List[Dict[str, Any]]]]

DB = "car_diagnosis"


def _vec_literal(embedding: Sequence[float]) -> str:
    """Format a float vector as a Doris array literal (params can't carry ARRAY<FLOAT>)."""
    return "[" + ",".join(f"{float(x):.6f}" for x in embedding) + "]"


class DorisCaseStore:
    def __init__(self, *, execute: Optional[Executor] = None, host: str = "", port: int = 9030,
                 user: str = "root", password: str = "", db: str = DB) -> None:
        self.host = host or os.environ.get("CAR_DORIS_HOST", "")
        self.port = int(os.environ.get("CAR_DORIS_PORT", port))
        self.user = os.environ.get("CAR_DORIS_USER", user)
        self._password = password or os.environ.get("CAR_DORIS_PASSWORD", "")
        self.db = db
        self._execute = execute or self._pymysql_execute

    # ---- writes ----

    def upsert_case(self, case_id: str, vehicle: VehicleRef, *, participant_ref: str = "",
                    status: str = "open") -> None:
        self._execute(
            f"INSERT INTO {self.db}.cases "
            "(case_id,participant_ref,vin,make,model,year,powertrain,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
            [case_id, participant_ref, vehicle.vin, vehicle.make, vehicle.model, vehicle.year,
             vehicle.powertrain.value, status])

    def append_event(self, event: Any) -> None:
        """Append a runtime_contracts InteractionEvent (conversation_id == case_id)."""
        ts = getattr(event, "timestamp", "") or ""
        dt = (ts[:10] if len(ts) >= 10 else "")  # YYYY-MM-DD partition key
        self._execute(
            f"INSERT INTO {self.db}.interaction_events "
            "(dt,case_id,interaction_id,ts,channel,modality,text,artifact_ref,transcript,provenance) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [dt or None, event.conversation_id, event.interaction_id, ts or None,
             event.channel.value, event.modality.value, event.text, event.artifact_ref,
             event.text, event.provenance])

    def record_observation(self, case_id: str, obs: DiagnosticObservation) -> None:
        self._execute(
            f"INSERT INTO {self.db}.observations "
            "(case_id,evidence_id,kind,code,value,unit,source,observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [case_id, obs.evidence_id, obs.kind, obs.code, obs.value, obs.unit, obs.source,
             obs.observed_at or None])

    def record_diagnosis(self, diag: Diagnosis) -> None:
        top = diag.top
        dt = (diag.created_at[:10] if len(diag.created_at) >= 10 else None)
        self._execute(
            f"INSERT INTO {self.db}.diagnoses "
            "(dt,case_id,diagnosis_id,top_cause,top_confidence,safety_gate_passed,"
            "needs_more_evidence,hypotheses_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [dt, diag.case_id, diag.diagnosis_id, (top.cause if top else ""),
             (float(top.confidence) if top else 0.0), diag.safety_gate_passed,
             diag.needs_more_evidence, json.dumps([h.canonical_form() for h in diag.hypotheses]),
             diag.created_at or None])

    def record_outcome(self, outcome_id: str, case_id: str, diagnosis_id: str,
                       repair: RepairEvidence) -> None:
        self._execute(
            f"INSERT INTO {self.db}.outcomes "
            "(outcome_id,case_id,diagnosis_id,diagnosis,repair_procedure,part,labor_hours,cost,"
            "fixed,provenance,verified_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
            [outcome_id, case_id, diagnosis_id, repair.diagnosis, repair.procedure, repair.part,
             (float(repair.labor_hours) if repair.labor_hours else None),
             (float(repair.cost) if repair.cost else None), repair.fixed, repair.provenance])

    # ---- reads / analytics ----

    def timeline(self, case_id: str) -> List[Dict[str, Any]]:
        return self._execute(
            f"SELECT ts,channel,modality,text,provenance FROM {self.db}.interaction_events "
            "WHERE case_id=%s ORDER BY ts", [case_id]) or []

    def knowledge_search(self, embedding: Sequence[float], *, k: int = 5,
                         dtc: str = "") -> List[Dict[str, Any]]:
        """Vector ANN search over the evidence lake (nearest TSB/complaint/repair docs)."""
        where = "WHERE dtc=%s " if dtc else ""
        params: List[Any] = [dtc] if dtc else []
        return self._execute(
            f"SELECT doc_id,source,dtc,title,body,"
            f"l2_distance_approximate(embedding,{_vec_literal(embedding)}) AS dist "
            f"FROM {self.db}.knowledge {where}ORDER BY dist LIMIT {int(k)}", params) or []

    def outcome_stats(self, *, dtc: str = "", make: str = "", model: str = "") -> List[Dict[str, Any]]:
        """The flywheel: for a DTC (optionally make/model), which repairs fixed it and how often."""
        conds, params = [], []
        if dtc:
            conds.append("o.diagnosis_id IN (SELECT diagnosis_id FROM %s.diagnoses)" % self.db)
        # join outcomes to cases for make/model filtering
        where = ["o.fixed = TRUE"]
        if make:
            where.append("c.make=%s"); params.append(make)
        if model:
            where.append("c.model=%s"); params.append(model)
        clause = " AND ".join(where)
        return self._execute(
            f"SELECT o.repair_procedure, o.part, COUNT(*) AS fixed_count, AVG(o.cost) AS avg_cost "
            f"FROM {self.db}.outcomes o LEFT JOIN {self.db}.cases c ON o.case_id=c.case_id "
            f"WHERE {clause} GROUP BY o.repair_procedure, o.part ORDER BY fixed_count DESC", params) or []

    # ---- transport (lazy pymysql) ----

    def _pymysql_execute(self, sql: str, params: Sequence[Any]) -> Optional[List[Dict[str, Any]]]:
        import pymysql  # lazy — only needed for a live connection
        conn = pymysql.connect(host=self.host, port=self.port, user=self.user,
                               password=self._password, database=self.db, autocommit=True,
                               cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:  # a SELECT
                    return list(cur.fetchall())
                return None
        finally:
            conn.close()
