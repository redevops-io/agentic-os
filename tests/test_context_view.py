"""ContextView / ArtifactHandle have a single canonical home (v0.2.x evidence-native reconciliation).

They were promoted out of the Evaluation Runtime into `mission.context_view` as the runtime-wide
EvidenceSnapshot/ContextEpoch primitive; `mission.evaluation` re-exports them, so there is exactly one
definition and the reproducible-working-set (content-addressed, replayable) invariant holds everywhere.
"""
from agentic_os.mission import context_view as cv_mod
from agentic_os.mission.context_view import ArtifactHandle, ContextView
from agentic_os.mission import evaluation as eval_mod


def test_evaluation_reexports_the_canonical_types():
    assert eval_mod.ArtifactHandle is ArtifactHandle
    assert eval_mod.ContextView is ContextView
    # evaluation's private _hash is the canonical content_hash (byte-stable cv ids across the boundary)
    assert eval_mod._hash is cv_mod.content_hash


def test_materialize_is_reference_first_and_reproducible():
    handles = [ArtifactHandle("ref/1", "dataset", "h1", approved=True),
               ArtifactHandle("ref/2", "dataset", "h2", approved=False)]
    a = ContextView.materialize(handles, ["ds@1", "proto@1"])
    b = ContextView.materialize(handles, ["ds@1", "proto@1"])   # same inputs → same identity
    # only the approved handle is in the working set (reference-first)
    assert a.derived_from == ("ref/1",)
    # deterministic → same content-addressed id (replay reproduces it)
    assert a.id == b.id and a.id.startswith("cv-")


def test_context_view_carries_snapshot_fields():
    # the fields a snapshot needs: a content hash, the lineage it derived from, and the version pins
    cv = ContextView.materialize([ArtifactHandle("r", "k", "hh", approved=True)], ["src@7#abc"])
    assert cv.content_hash and cv.derived_from == ("r",) and cv.pins == ("src@7#abc",)
