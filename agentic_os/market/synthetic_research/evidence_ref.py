"""StudyEvidenceRef: a plain, serialisable, explicitly-synthetic pointer from a market Opportunity/Experiment to a
synthetic buyer study (SR-04 evidence bridge).

It lets a human reviewer see "a synthetic study exists for this hypothesis" next to an experiment, labelled
``HYPOTHESIS, synthetic, unvalidated``. It is NOT evidence of market behaviour and has no path into measurement:

  * it is a reference (study id, manifest hash, construct, embedder name), never a score or a metric;
  * ``synthetic`` is always True and the label is fixed (both are validated at construction);
  * it serialises to a string with the ``synthetic-study:`` scheme so it fits the existing
    ``Experiment.evidence_refs: Tuple[str, ...]`` / ``Opportunity.evidence_refs`` fields with NO contract change,
    and ``split_refs`` can always tell it apart from first-party refs;
  * this module imports nothing from ``outcomes_join``, ``outcome_store``, the SSR scoring modules or the search
    plane (a static test enforces it), and ``outcomes_join.METRIC_SOURCES`` gains no synthetic source.

Two seams, both through existing contracts: :class:`StudyEvidenceWriter` plugs into
``plan_experiments(..., writer=...)`` and labels the proposed change text; :func:`attach_study_refs` appends the
serialised refs to an already planned ``Experiment`` (attach AFTER planning so ``explain``'s count of supporting
observations is not inflated by a synthetic ref).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence, Tuple, TypeVar

__all__ = ["StudyEvidenceRef", "StudyEvidenceWriter", "attach_study_refs", "split_refs", "is_synthetic_ref",
           "SYNTHETIC_REF_SCHEME", "SYNTHETIC_LABEL"]

SYNTHETIC_LABEL = "HYPOTHESIS, synthetic, unvalidated"
SYNTHETIC_REF_SCHEME = "synthetic-study:v1:"

T = TypeVar("T")


def is_synthetic_ref(ref: object) -> bool:
    return isinstance(ref, str) and ref.startswith(SYNTHETIC_REF_SCHEME)


@dataclass(frozen=True)
class StudyEvidenceRef:
    study_id: str
    manifest_hash: str
    construct: str
    embedder_name: str
    synthetic: bool = True
    label: str = SYNTHETIC_LABEL

    def __post_init__(self) -> None:
        if self.synthetic is not True:
            raise ValueError("StudyEvidenceRef is always synthetic")
        if self.label != SYNTHETIC_LABEL:
            raise ValueError(f"StudyEvidenceRef label is fixed: {SYNTHETIC_LABEL!r}")
        for f in ("study_id", "manifest_hash", "construct", "embedder_name"):
            v = getattr(self, f)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"StudyEvidenceRef.{f} is required")

    def to_dict(self) -> dict:
        return {"study_id": self.study_id, "manifest_hash": self.manifest_hash, "construct": self.construct,
                "embedder_name": self.embedder_name, "synthetic": True, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> "StudyEvidenceRef":
        return cls(study_id=d.get("study_id", ""), manifest_hash=d.get("manifest_hash", ""),
                   construct=d.get("construct", ""), embedder_name=d.get("embedder_name", ""),
                   synthetic=d.get("synthetic", True), label=d.get("label", SYNTHETIC_LABEL))

    def to_ref(self) -> str:
        """The string stored in ``evidence_refs``: scheme + canonical JSON."""
        return SYNTHETIC_REF_SCHEME + json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def parse(cls, ref: str) -> "StudyEvidenceRef":
        if not is_synthetic_ref(ref):
            raise ValueError("not a synthetic-study ref")
        return cls.from_dict(json.loads(ref[len(SYNTHETIC_REF_SCHEME):]))

    def note(self) -> str:
        return (f"[{self.label}: pre-test evidence from synthetic study {self.study_id} "
                f"(construct {self.construct}); not first-party evidence]")


def split_refs(evidence_refs: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[StudyEvidenceRef, ...]]:
    """(first-party/other refs unchanged, parsed synthetic refs). A malformed synthetic ref raises."""
    plain = tuple(r for r in evidence_refs if not is_synthetic_ref(r))
    synth = tuple(StudyEvidenceRef.parse(r) for r in evidence_refs if is_synthetic_ref(r))
    return plain, synth


class StudyEvidenceWriter:
    """A ``ChangeWriter`` (see ``market.experiments``) that labels the proposed change with the synthetic studies
    behind it. It delegates to ``inner`` when given (else uses the opportunity's own proposed experiment text) and
    only ADDS a labelled note; the planner keeps the opportunity's evidence and expected value regardless."""

    def __init__(self, refs: Sequence[StudyEvidenceRef], inner: Optional[Any] = None) -> None:
        self.refs = tuple(refs)
        self.inner = inner

    def write(self, opportunity: Any) -> str:
        base = self.inner.write(opportunity) if self.inner is not None else opportunity.proposed_experiment
        return base if not self.refs else base + " " + " ".join(r.note() for r in self.refs)


def attach_study_refs(obj: T, refs: Sequence[StudyEvidenceRef]) -> T:
    """Return a copy of an ``Experiment`` (or ``Opportunity``) whose ``evidence_refs`` additionally lists the
    serialised synthetic refs; for an object with an ``explain`` field a labelled sentence is appended. Existing
    refs are kept; duplicates are not added. No existing field is removed or re-typed."""
    new = [r.to_ref() for r in refs]
    existing = tuple(getattr(obj, "evidence_refs", ()) or ())
    merged = existing + tuple(r for r in dict.fromkeys(new) if r not in existing)
    changes = {"evidence_refs": merged}
    if hasattr(obj, "explain") and refs and merged != existing:
        added = [r for r in refs if r.to_ref() not in existing]
        changes["explain"] = (getattr(obj, "explain") + f" | {SYNTHETIC_LABEL}: {len(added)} synthetic study "
                              f"ref(s) attached; not counted as observations and never first-party evidence.")
    return replace(obj, **changes)  # type: ignore[type-var]
