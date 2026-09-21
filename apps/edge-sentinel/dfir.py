"""DFIR — deterministic acquisition, chain of custody, and timeline construction (Phase 5).

The plan's DFIR acceptance: a known fixture gives a **reproducible timeline** and evidence-linked findings,
and **narrative changes never change source evidence**. So this is built entirely on the Phase 1 evidence
spine — forensic artifacts are acquired as immutable, content-addressed :class:`EvidenceArtifact`s with an
explicit chain of custody, timeline events are *derived* from evidence (each carries its evidence ref), and
the human-readable ``description`` is separate from the evidence digest: re-narrating an event yields a new
event id but touches no evidence.

Static/sandbox artifact analysis is deliberately **not** built here — :class:`SandboxProvider` is an
interface. Nothing executes untrusted artifacts in this process; a real detonation service (or none)
implements the seam later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .evidence import (
    CaseStatus,
    CaseType,
    EvidenceArtifact,
    Finding,
    FindingStatus,
    SecurityCase,
    Severity,
    _now,
    canonical_json,
    sha256_hex,
)


# ──────────────────────────── acquisition + chain of custody ────────────────────────────

def acquire(kind: str, source: str, content, *, acquired_by: str, method: str,
            source_host: str = "", observed_at: str = "") -> EvidenceArtifact:
    """Acquire a forensic artifact as immutable, content-addressed evidence with a detailed custody entry.
    Same bytes → same ``sha256``/``artifact_id`` (so an acquisition is verifiable and de-duplicated)."""
    raw = content if isinstance(content, str) else canonical_json(content)
    digest = sha256_hex(raw)
    now = _now()
    custody = (f"acquired_by:{acquired_by}", f"method:{method}",
               f"host:{source_host or 'unknown'}", f"at:{now}", f"sha256:{digest}")
    return EvidenceArtifact(kind=kind, source=source, sha256=digest,
                            content_ref=f"forensic://{source}/{digest}",
                            acquired_at=observed_at or now, known_at=now,
                            chain_of_custody=custody, parser_version="es-dfir-0.1.0")


def verify_custody(ev: EvidenceArtifact, raw) -> bool:
    """Re-hash the raw bytes and confirm they still match the artifact's declared digest — the chain of
    custody holds iff the content is unchanged. Deterministic; the basis for 'evidence never changes'."""
    raw_s = raw if isinstance(raw, str) else canonical_json(raw)
    return sha256_hex(raw_s) == ev.sha256


# ──────────────────────────── timeline ────────────────────────────

@dataclass(frozen=True)
class TimelineEvent:
    """One derived event on the forensic timeline, linked to the evidence it came from. ``description`` is
    narrative and does NOT enter the id — re-narrating produces a new event, never a changed evidence."""
    timestamp: str
    evidence_ref: str
    actor: str = ""
    action: str = ""
    target: str = ""
    description: str = ""

    @property
    def event_id(self) -> str:
        # narrative-independent identity: same (time, evidence, actor, action, target) → same id
        basis = f"{self.timestamp}|{self.evidence_ref}|{self.actor}|{self.action}|{self.target}"
        return f"tl-{sha256_hex(basis)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["event_id"] = self.event_id; return d


def build_timeline(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Order events deterministically (by timestamp, then stable event_id) so the same inputs always
    produce the same timeline — the 'reproducible timeline' guarantee."""
    return sorted(events, key=lambda e: (e.timestamp, e.event_id))


def correlate(timeline: list[TimelineEvent]) -> dict[str, list[TimelineEvent]]:
    """Group the timeline by entity (actor and target) so an analyst can pivot. Deterministic order."""
    out: dict[str, list[TimelineEvent]] = {}
    for e in build_timeline(timeline):
        for entity in (e.actor, e.target):
            if entity:
                out.setdefault(entity, []).append(e)
    return out


def dfir_case(store, timeline: list[TimelineEvent], *, title: str, severity: Severity = Severity.HIGH,
              findings: list[Finding] | None = None) -> SecurityCase:
    """Open a DFIR case linking the (reproducible) timeline's evidence + any findings. The timeline is a
    view over evidence already in the store; the case references evidence + findings, never copies them."""
    tl = build_timeline(timeline)
    evidence_refs = tuple(dict.fromkeys(e.evidence_ref for e in tl))
    finding_refs = tuple(f.finding_id for f in (findings or []))
    for f in (findings or []):
        store.put_finding(f)
    case = SecurityCase(
        case_type=CaseType.DFIR, severity=severity, created_at=_now(), known_at=_now(),
        title=title, status=CaseStatus.INVESTIGATING,
        evidence_refs=evidence_refs, finding_refs=finding_refs)
    return store.put_case(case)


# ──────────────────────────── sandbox seam (interface only) ────────────────────────────

@dataclass(frozen=True)
class SandboxVerdict:
    artifact_ref: str
    verdict: str                # malicious | suspicious | benign | not_analyzed | error
    score: float = 0.0
    signatures: tuple[str, ...] = ()
    provider: str = ""
    evidence_ref: str = ""      # a report artifact, when the provider returns one

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["signatures"] = list(self.signatures); return d


class SandboxProvider(ABC):
    """The seam for static/dynamic artifact analysis. INTENTIONALLY not implemented here — Edge Sentinel
    never executes untrusted artifacts in-process. A real detonation service (or a deliberate no-op)
    implements ``analyze``; results come back as evidence-linked verdicts, never as authority to act."""
    @abstractmethod
    def analyze(self, artifact_ref: str, content) -> SandboxVerdict:
        ...


class NullSandboxProvider(SandboxProvider):
    """Default: no analysis performed. Makes 'we did not detonate this' an explicit, honest verdict rather
    than a silent gap."""
    def analyze(self, artifact_ref: str, content) -> SandboxVerdict:
        return SandboxVerdict(artifact_ref=artifact_ref, verdict="not_analyzed", provider="null")
