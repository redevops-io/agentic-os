"""Exports. Two, and neither can say more than the data does.

  restricted_pack   what an auditor-facing reviewer receives: controls, the latest assessment of each with its evidence references (id and digest, not the bytes),
                    open exceptions, and the integrity state of the vault and the ledger. It carries a banner that it is not an attestation.
  benchmark_summary the redacted counts the central benchmark may ingest: results by control, nothing about content, tenants or evidence.

Neither contains an overall score, a "compliant" flag or a certification claim; `assert_no_claim` refuses an export that does.
"""
from __future__ import annotations

import json
from typing import Any

BANNER = "Readiness evidence for a reviewer. Not an attestation, not an auditor's opinion, and no statement that any criterion is satisfied."
FORBIDDEN = ("soc2_certified", "certified", "compliance_score", "overall_score", "overall_result", "compliant", "attested")


def assert_no_claim(obj: Any) -> None:
    text = json.dumps(obj, sort_keys=True).lower()
    keys = set()

    def walk(o):
        if isinstance(o, dict):
            keys.update(str(k).lower() for k in o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    bad = [f for f in FORBIDDEN if f in keys]
    if bad or "soc2_certified" in text:
        raise ValueError(f"an export may not carry a certification or an overall compliance claim: {bad}")


def restricted_pack(ledger, vault, catalog, tenant: str, scope: str, period_from: float, period_to: float) -> dict:
    controls = []
    for c in catalog:
        latest = ledger.latest(tenant, c.control_id, scope, period_from, period_to)
        entry: dict = {"control_id": c.control_id, "title": c.title, "criterion_refs": list(c.criterion_refs), "owner_role": c.owner_role,
                       "manual_dependency": c.manual_dependency, "assessed": latest is not None}
        if latest is not None:
            ref, a, _ = latest
            st = ledger.status(tenant, ref)
            by_id = {s.artifact_id: s for s in vault.find(tenant, scope=scope, since=period_from, until=period_to)}
            entry.update({"assessment": ref, "result": a["result"], "reason": a["reason"], "coverage": a["coverage"], "population": a["population"],
                          "tested": a["tested"], "failures": a["failures"], "reviewed": st["reviewed"], "superseded_by": st["superseded_by"],
                          "exceptions": [{k: e[k] for k in ("ref", "state", "expires_at", "remediation")} for e in st["exceptions"]],
                          "evidence": [{"artifact_id": i, "sha256": by_id[i].sha256, "kind": by_id[i].kind, "producer": by_id[i].producer,
                                        "late_by_s": by_id[i].late_by_s} for i in a["evidence_ids"] if i in by_id]})
        controls.append(entry)
    pack = {"banner": BANNER, "tenant": tenant, "scope": scope, "period": [period_from, period_to], "controls": controls,
            "integrity": {"vault": vault.verify_all(), "ledger": ledger.verify()}}
    assert_no_claim(pack)
    return pack


def benchmark_summary(ledger, tenant: str, scope: str, period_from: float, period_to: float, catalog) -> dict:
    """Counts only: how many controls ended in each result. No evidence, no tenant, no criterion text."""
    counts: dict = {}
    by_control: dict = {}
    for c in catalog:
        latest = ledger.latest(tenant, c.control_id, scope, period_from, period_to)
        r = latest[1]["result"] if latest else "NOT_ASSESSED"
        counts[r] = counts.get(r, 0) + 1
        by_control[c.control_id] = r
    out = {"banner": BANNER, "controls": len(catalog), "by_result": counts, "by_control": by_control}
    assert_no_claim(out)
    return out
