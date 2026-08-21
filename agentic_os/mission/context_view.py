"""Reference-first context — the canonical content-addressed working-set primitive.

An :class:`ArtifactHandle` is a *governed reference* (ref + content hash + authority + approval) that
carries enough to plan without exposing the object; a :class:`ContextView` is a bounded, content-addressed
working set materialized from approved handles, reproducible as a pure function of ``(handles, plan, pins)``
— so any run's inputs are themselves a governed, replayable artifact.

These first appeared inside the Evaluation Runtime (``evaluation.py``, v10) where a benchmark's inputs had
to be pinned and replayable. They are **not** eval-specific: the same "pin an exact, approved, content-
addressed set of evidence a run consumed" need is the runtime-wide **EvidenceSnapshot / ContextEpoch**
primitive named in the v0.2.x evidence-native suggestions (E3/E12). This module is their canonical home so
the snapshot concept has ONE definition; ``evaluation.py`` re-exports them for back-compat, and Slice 2 of
the evidence-native work promotes ``ContextView`` to the Mission spine to bind a whole mission to a point-
in-time evidence view — extending this type, never reinventing it (E0 audit §3 reconciliation).

Pure data + pure construction; no I/O. ``content_hash``/``derived_from``/``pins`` are exactly the fields a
snapshot needs, and ``materialize`` is deterministic, so a replay reproduces the same ``ContextView`` id.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


def content_hash(*parts: str) -> str:
    """Content address for a working set: sha256 over the joined parts, truncated to 16 hex. Kept
    byte-stable — a run's ContextView id is a function of its members, so replay must reproduce it."""
    return sha256("|".join(parts).encode()).hexdigest()[:16]


# Back-compat alias for the historical private helper name used inside evaluation.py.
_hash = content_hash


@dataclass(frozen=True)
class ArtifactHandle:
    """A governed reference — carries enough to plan without exposing the object (v10). Only
    :attr:`approved` handles may be materialized into a :class:`ContextView`."""
    ref: str
    kind: str
    content_hash: str
    authority: str = ""
    visibility: str = "private"
    approved: bool = False                  # only approved handles may be materialized


@dataclass(frozen=True)
class ContextView:
    """A bounded, content-addressed working set materialized from approved handles (v10). Reproducible:
    a function of (handles, plan, pins) — so any run's inputs are themselves a governed artifact. This is
    the EvidenceSnapshot / ContextEpoch primitive: ``content_hash`` pins the set, ``derived_from`` records
    the handle refs it was built from (lineage), ``pins`` records the version pins it resolved against."""
    id: str
    content_hash: str
    derived_from: tuple[str, ...]           # the handle refs materialized (lineage)
    pins: tuple[str, ...]                   # dataset/protocol/program/source version pins
    budget: int = 0

    @staticmethod
    def materialize(handles: list["ArtifactHandle"], pins: list[str], budget: int = 0) -> "ContextView":
        approved = [h for h in handles if h.approved]           # reference-first: only approved refs
        ch = content_hash(*[h.content_hash for h in approved], *pins)
        return ContextView(f"cv-{ch}", ch, tuple(h.ref for h in approved), tuple(sorted(pins)), budget)
