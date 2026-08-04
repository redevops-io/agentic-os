"""Control-plane wire protocol — the records the Kotlin/Spark **execution profile** emits across the
driver/executor boundary, decoded by the Python/Go **control plane** (see the Kotlin
`ControlPlaneBoundary` + `redevops-runtime-kotlin` PR #4).

Canonical wire form (the external control-plane protocol): snake_case fields, a `type` discriminator,
keys sorted, compact JSON, all values strings — so encode/decode is **byte-identical across Python, Go
and Kotlin**. Conformance is asserted against the shared golden fixtures below (the same bytes Kotlin's
`ControlPlaneConformanceDemo --emit` produces).

The control plane *consumes* these facts and makes the decisions; the execution profile never does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

#: Semantic version of the public control-plane wire protocol. Additive-only within a major (new record
#: types, new optional fields); a breaking change to the canonical wire form bumps the major. The GOLDEN
#: fixtures below are the cross-language conformance vectors (byte-identical in Python, Go and Kotlin).
CONTRACT_VERSION = "control-plane/v1"


def _canonical(fields: dict[str, str]) -> str:
    # sorted keys, compact separators, UTF-8 passthrough — matches Kotlin Wire.canonicalJson / Go's encoder
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class VerificationObservation:
    node_id: str
    outcome: str
    detail: str = ""
    authorization: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"type": "verification_observation", "node_id": self.node_id, "outcome": self.outcome,
                "detail": self.detail, "authorization": self.authorization}

    def to_json(self) -> str:
        return _canonical(self.to_wire())


@dataclass
class EvidenceReference:
    ref: str
    kind: str
    content_hash: str
    authorization: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"type": "evidence_reference", "ref": self.ref, "kind": self.kind,
                "content_hash": self.content_hash, "authorization": self.authorization}

    def to_json(self) -> str:
        return _canonical(self.to_wire())


@dataclass
class PolicyInputObserved:
    key: str
    value: str
    authorization: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"type": "policy_input_observed", "key": self.key, "value": self.value,
                "authorization": self.authorization}

    def to_json(self) -> str:
        return _canonical(self.to_wire())


@dataclass
class ExecutionConstraintViolation:
    node_id: str
    constraint: str
    detail: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"type": "execution_constraint_violation", "node_id": self.node_id,
                "constraint": self.constraint, "detail": self.detail}

    def to_json(self) -> str:
        return _canonical(self.to_wire())


_DECODERS = {
    "verification_observation": lambda m: VerificationObservation(m["node_id"], m["outcome"], m.get("detail", ""), m.get("authorization", "")),
    "evidence_reference": lambda m: EvidenceReference(m["ref"], m["kind"], m["content_hash"], m.get("authorization", "")),
    "policy_input_observed": lambda m: PolicyInputObserved(m["key"], m["value"], m.get("authorization", "")),
    "execution_constraint_violation": lambda m: ExecutionConstraintViolation(m["node_id"], m["constraint"], m.get("detail", "")),
}


def decode(line: str):
    """Decode one canonical wire line into its record, dispatching on the `type` discriminator."""
    m = json.loads(line)
    t = m.get("type")
    if t not in _DECODERS:
        raise ValueError(f"unknown control-plane wire record type {t!r}")
    return _DECODERS[t](m)


# The shared golden fixtures — the exact bytes the Kotlin execution profile emits. Conformance = decoding
# each and re-encoding is byte-identical (proving Kotlin's records decode identically here).
GOLDEN = [
    '{"authorization":"prog:abc123","detail":"deterministic","node_id":"nd_1","outcome":"pass","type":"verification_observation"}',
    '{"authorization":"prog:abc123","content_hash":"h_9f","kind":"evidence","ref":"ev/incident-1","type":"evidence_reference"}',
    '{"authorization":"prog:abc123","key":"data_residency","type":"policy_input_observed","value":"eu"}',
    '{"constraint":"no side effect inside a retryable map","detail":"mapPartitions","node_id":"nd_2","type":"execution_constraint_violation"}',
]


def _demo() -> int:
    checks: list[tuple[str, bool]] = []
    for line in GOLDEN:
        rec = decode(line)
        t = json.loads(line)["type"]
        checks.append((f"{t}: decodes + re-encodes byte-identically to the Kotlin-emitted golden", rec.to_json() == line))
    # a record built here encodes to the same canonical bytes (Python is byte-compatible as an emitter too)
    built = VerificationObservation("nd_1", "pass", "deterministic", "prog:abc123").to_json()
    checks.append(("Python encodes canonically (matches the golden)", built == GOLDEN[0]))
    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())
