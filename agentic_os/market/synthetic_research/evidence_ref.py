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
serialised refs to an already planned ``Experiment`` and to nothing else (an ``Opportunity`` or a
``CommercialHypothesis`` feeds counts and sealed evidence, so attaching there is refused). Anything that counts
or seals ``evidence_refs`` ignores or refuses refs starting with ``synthetic-study:``.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence, Tuple

from ..contracts import Experiment
from ..synthetic_refs import is_synthetic_ref as _shared_is_synthetic_ref

__all__ = ["StudyEvidenceRef", "StudyEvidenceWriter", "attach_study_refs", "split_refs", "is_synthetic_ref",
           "SYNTHETIC_REF_SCHEME", "SYNTHETIC_LABEL"]

SYNTHETIC_LABEL = "HYPOTHESIS, synthetic, unvalidated"
SYNTHETIC_REF_SCHEME = "synthetic-study:v1:"


def is_synthetic_ref(ref: object) -> bool:
    """True for anything that looks like a synthetic-study ref once normalised (any version; any case or blanks;
    fullwidth forms, zero-width / bidi / control characters and common look-alike letters folded; see
    ``market/synthetic_refs.py``, the single shared test also used by the ledger guard, ``freeze_experiment`` and the
    explain counting): it is then never treated as a first-party reference, even when it is malformed."""
    return _shared_is_synthetic_ref(ref)


def _clean(text: object, cap: int = 80) -> str:
    """Plain single-line text for rendering into a proposed-change note: no control, zero-width or bidi
    characters, no newlines, brackets neutralised (they delimit the label), bounded length."""
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        if cat[0] == "Z" or ch in "\t\n\r\x0b\x0c":
            out.append(" ")
        elif cat[0] == "C":
            continue
        else:
            out.append({"[": "(", "]": ")"}.get(ch, ch))
    return " ".join("".join(out).split())[:cap]


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
        """Parse a serialised ref. Raises exactly one documented exception type, ``ValueError``, for ANY malformed
        input (wrong scheme/version, bad JSON, wrong types, missing fields, synthetic != True, wrong label)."""
        try:
            if not isinstance(ref, str) or not ref.startswith(SYNTHETIC_REF_SCHEME):
                raise ValueError("not a synthetic-study v1 ref")
            data = json.loads(ref[len(SYNTHETIC_REF_SCHEME):])
            if not isinstance(data, dict):
                raise ValueError("synthetic-study ref payload is not an object")
            return cls.from_dict(data)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — TypeError, RecursionError, ...: one exception type for callers
            raise ValueError(f"malformed synthetic-study ref ({type(exc).__name__})") from None

    def note(self) -> str:
        return (f"[{self.label}: pre-test evidence from synthetic study {_clean(self.study_id)} "
                f"(construct {_clean(self.construct)}); not first-party evidence]")


def split_refs(evidence_refs: Sequence[str]
               ) -> Tuple[Tuple[str, ...], Tuple[StudyEvidenceRef, ...], Tuple[str, ...]]:
    """(first-party/other refs unchanged, parsed synthetic refs, malformed synthetic refs). One bad ref never
    breaks the list: anything that looks like a synthetic ref but does not parse is returned in the third element
    (never in the first), so it can be flagged without being mistaken for first-party evidence."""
    plain, synth, malformed = [], [], []
    for r in evidence_refs:
        if not is_synthetic_ref(r):
            plain.append(r)
            continue
        try:
            synth.append(StudyEvidenceRef.parse(r))
        except ValueError:
            malformed.append(r)
    return tuple(plain), tuple(synth), tuple(malformed)


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


def attach_study_refs(experiment: Experiment, refs: Sequence[StudyEvidenceRef]) -> Experiment:
    """Return a copy of a PLANNED ``Experiment`` whose ``evidence_refs`` additionally lists the serialised
    synthetic refs, with a labelled sentence appended to ``explain``. Existing refs are kept; duplicates are not
    added; no existing field is removed or re-typed. Anything that is not an ``Experiment`` (an ``Opportunity``,
    whose evidence is counted, or a ``CommercialHypothesis``, whose evidence is sealed) raises ``TypeError``."""
    if not isinstance(experiment, Experiment):
        raise TypeError("synthetic study refs can only be attached to a planned Experiment")
    new = [r.to_ref() for r in refs]
    existing = tuple(experiment.evidence_refs or ())
    merged = existing + tuple(r for r in dict.fromkeys(new) if r not in existing)
    if merged == existing:
        return experiment
    added = [r for r in refs if r.to_ref() not in existing]
    explain = (experiment.explain + f" | {SYNTHETIC_LABEL}: {len(set(r.to_ref() for r in added))} synthetic study "
               f"ref(s) attached; not counted as observations and never first-party evidence.")
    return replace(experiment, evidence_refs=merged, explain=explain)
