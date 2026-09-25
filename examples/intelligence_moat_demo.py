"""End-to-end demo of the external-intelligence moat loop (moat plan §4.1, §7, §8, §9, WP7).

Offline, no keys. Shows the four seams working together:
  1. an app asks for a CAPABILITY (never a provider) via `request_for`;
  2. historical customer experience is imported and preserved (WP7);
  3. gated paid lookups are recorded in the evidence-value ledger (§8/WP8);
  4. the evaluation harness (§9) turns the ledger into per-provider retain/review/drop verdicts.

Run:  PYTHONPATH=/mnt/backup/projects/runtime-contracts python examples/intelligence_moat_demo.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from runtime_contracts.protocol import Capability, EvidenceValueRecord

from agentic_os.intelligence import (
    EvidenceValueStore, evaluate, from_zendesk, report, request_for, seed_value_store,
)


def main() -> None:
    # 1. Apps ask for capabilities, tagged with purpose + sensitivity, refused if outside their remit.
    req = request_for("twenty", Capability.PERSON_ENRICHMENT,
                      decision_case_id="dc-101", subject_refs=("jane@acme.com",), tenant="summit")
    print("1) capability request:", req.capability.value,
          f"purpose={req.purpose!r} sensitivity={req.sensitivity.value}")

    tmp = Path(tempfile.mkdtemp())
    store = EvidenceValueStore(str(tmp / "evidence_value.jsonl"))

    # 2. Preserve prior Experience when the customer leaves a legacy tool (WP7 / M2).
    imported = from_zendesk([
        {"id": 11, "status": "solved", "satisfaction_rating": {"score": "good"}, "tags": ["billing"]},
        {"id": 12, "status": "closed", "satisfaction_rating": {"score": "bad"}},
    ], tenant="summit")
    print(f"2) imported {seed_value_store(store, imported)} historical outcomes (provider=import:zendesk, cost 0)")

    # 3. Simulate a few gated paid lookups landing in the ledger (in production the bridge writes these).
    for i in range(6):
        store.append(EvidenceValueRecord(
            decision_case_id=f"dc-2{i}", capability=Capability.PERSON_ENRICHMENT, provider="apollo",
            evidence_requested=True, evidence_received=True, cost=0.40,
            decision_before="hold", decision_after="reach-out", action="sequence",
            verified_outcome=("beneficial" if i < 5 else "neutral")))
    for i in range(6):
        store.append(EvidenceValueRecord(
            decision_case_id=f"dc-3{i}", capability=Capability.COMPANY_ENRICHMENT, provider="pricey_co",
            evidence_requested=True, evidence_received=True, cost=3.0,
            decision_before="A", decision_after="B", action="act",
            verified_outcome="neutral"))
    print("3) recorded 12 paid lookups across 2 providers")

    # 4. Evaluate: the paid-evidence rule decides which provider earns its place (§9).
    print("\n4) provider evaluation (§9):\n")
    print(report(store))
    verdicts = {(e.provider, e.capability): e.verdict() for e in evaluate(store)}
    print("\n→ apollo/person_enrichment keeps its place; pricey_co/company_enrichment does not:")
    print("  ", verdicts)


if __name__ == "__main__":
    main()
