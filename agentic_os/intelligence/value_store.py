"""Evidence-value accounting store (intelligence plan §8 / WP8).

Every gated lookup appends an EvidenceValueRecord (why requested, provider, cost, decision before/after, action,
and later the verified outcome). This is the ledger that lets the Runtime learn whether a provider/capability is
actually worth its cost for a class of decision — cost per acquired evidence, per changed decision, per verified
beneficial changed decision (§9). Append-only JSONL; deliberately dependency-light.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Optional

from agentic_os._platform.files import append_line as _append_line, fsync_enabled, locked_rewrite
from runtime_contracts.protocol import Capability, EvidenceValueRecord


class EvidenceValueStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def append(self, record: EvidenceValueRecord) -> None:
        _append_line(self.path, json.dumps(record.canonical_form()), fsync=fsync_enabled())      # a cost ledger: forced to disk

    @staticmethod
    def _parse(text: str) -> list[EvidenceValueRecord]:
        out: list[EvidenceValueRecord] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(EvidenceValueRecord(
                decision_case_id=d["decision_case_id"], capability=Capability(d["capability"]),
                provider=d.get("provider", ""), evidence_requested=d["evidence_requested"],
                evidence_received=d["evidence_received"], cost=d.get("cost", 0.0),
                decision_before=d.get("decision_before", ""), decision_after=d.get("decision_after", ""),
                action=d.get("action", ""), verified_outcome=d.get("verified_outcome")))
        return out

    def records(self) -> list[EvidenceValueRecord]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return self._parse(fh.read())

    def resolve_outcome(self, decision_case_id: str, verified_outcome: str) -> int:
        """Backfill the verified outcome for a decision's records (called after the outcome is observed)."""
        changed = [0]

        def rewrite(old: bytes):
            recs = self._parse(old.decode("utf-8"))
            for i, r in enumerate(recs):
                if r.decision_case_id == decision_case_id and r.verified_outcome is None:
                    recs[i] = replace(r, verified_outcome=verified_outcome)
                    changed[0] += 1
            if not changed[0]:
                return None
            return "".join(json.dumps(r.canonical_form()) + "\n" for r in recs).encode("utf-8")
        # In place, under the lock appends take: a rewrite cannot destroy a record appended meanwhile, and an append cannot land in the middle of it.
        locked_rewrite(self.path, rewrite, fsync=fsync_enabled())
        return changed[0]

    def summary(self) -> dict[tuple[str, str], dict]:
        """Per (provider, capability): lookups, spend, changed-decision count, verified-beneficial count."""
        agg: dict[tuple[str, str], dict] = {}
        for r in self.records():
            key = (r.provider, r.capability.value)
            a = agg.setdefault(key, {"lookups": 0, "cost": 0.0, "changed": 0, "verified_beneficial": 0})
            a["lookups"] += 1
            a["cost"] += r.cost
            if r.changed_decision():
                a["changed"] += 1
                if (r.verified_outcome or "").lower() in ("beneficial", "good", "success", "improved"):
                    a["verified_beneficial"] += 1
        return agg
