"""OutcomeSimulator — the safe write target for consequential actions (docx TABLE 6).

Quotes, emails, bookings, payments and other actions that should not hit external parties are written here
instead. Each write gets a stable id, is labelled SIMULATE, and is retained so verification/reconciliation
can confirm the intended state transition occurred — without any real side effect. Idempotent on the
(capability, key) pair, so a retried or replayed action does not double-write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from runtime_contracts import content_hash
from runtime_contracts.world import RealismClass


@dataclass
class SimArtifact:
    artifact_id: str
    kind: str                       # quote | opportunity | booking | email | refund | ...
    capability_id: str
    provider: str
    data: Dict[str, Any]
    realism: str = RealismClass.SIMULATED.value


class OutcomeSimulator:
    """A demo-safe write sink. ``record()`` is what the Capability Fabric calls for a write under a
    non-LIVE mode; the artifacts back verification and the outcome panel."""

    def __init__(self) -> None:
        self._artifacts: Dict[str, SimArtifact] = {}

    def record(self, capability_id: str, inputs: Dict[str, Any], *, provider: str = "") -> Dict[str, Any]:
        kind = capability_id.split(".")[-1]                      # quote / booking / send / …
        key = inputs.get("idempotency_key") or content_hash({"c": capability_id, "i": inputs}).split(":", 1)[-1][:16]
        aid = f"sim-{kind}-{key[:10]}"
        if aid not in self._artifacts:
            self._artifacts[aid] = SimArtifact(artifact_id=aid, kind=kind, capability_id=capability_id,
                                               provider=provider, data=dict(inputs))
        a = self._artifacts[aid]
        return {"artifact_id": a.artifact_id, "kind": a.kind, "simulated": True,
                "realism": a.realism, "provider": a.provider}

    def get(self, artifact_id: str) -> "SimArtifact | None":
        return self._artifacts.get(artifact_id)

    def of_kind(self, kind: str) -> List[SimArtifact]:
        return [a for a in self._artifacts.values() if a.kind == kind]

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._artifacts
