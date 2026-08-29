"""Safe-concurrency guard for the video-ad operator (parallel-execution migration, step 4).

Cross-repo: needs the `reel` package (ffmpeg-mcp-aws/ai/sidekick-reel) importable; we add it to the path
the same way test_operator.py does, and build the operator with a dummy IO (we only read the declared
concurrency metadata — no handler runs). Assertions go through the real TopoScheduler.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

# monorepo-of-repos: make the sibling reel package importable when not already on the path.
for rel in ("../../../ffmpeg-mcp-aws/ai/sidekick-reel",):
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), rel))
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("reel.ad_operator")

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy   # noqa: E402
from agentic_os.mission.types import ExecutionGraph, Node                # noqa: E402

_op = importlib.import_module("video-ad.operator").build_video_ad_operator(io=object(), chooser=None)
_SPECS = {c.name: c for c in _op.manifest.capabilities}
_S, _W = TopoScheduler(), SchedulePolicy(max_concurrency=8)


def _node(nid, cap, **inp):
    s = _SPECS[cap]
    n = Node(capability=s.name, operator=s.operator, side_effecting=s.side_effecting,
             concurrency_mode=s.concurrency_mode, concurrency_key=s.concurrency_key,
             resource_keys=list(s.resource_keys), max_parallelism=s.max_parallelism, inputs=dict(inp))
    n.id = nid
    return n


def _released(*ns):
    return {n.id for n in _S.ready(ExecutionGraph(nodes=list(ns)), set(), set(), _W)}


def _reason(*ns):
    g = ExecutionGraph(nodes=list(ns))
    ser = [r for r in _S.explain(g, set(), set(), _W) if r["decision"] == "serialized"]
    return len(_S.ready(g, set(), set(), _W)), (ser[0]["reason"] if ser else "")


def test_renders_are_bounded_by_the_render_provider():
    """Different shots render concurrently, but the GPU/provider cap holds the wave to 2 — render and
    regenerate share the same provider lane."""
    mixed = [_node("r1", "video.render_segment"), _node("r2", "video.render_segment"),
             _node("g1", "video.regenerate_shots")]
    assert len(_released(*mixed)) == 2   # provider:render max_parallelism=2


def test_writes_to_the_same_cut_serialize():
    n, reason = _reason(_node("a", "video.assemble_reel", cut_id="cutA"),
                        _node("b", "video.add_voiceover", cut_id="cutA"))
    assert n == 1 and "video:cut:cutA" in reason


def test_writes_to_different_cuts_parallelize():
    assert _released(_node("a", "video.add_subtitles", cut_id="cutA"),
                     _node("b", "video.add_subtitles", cut_id="cutB")) == {"a", "b"}


def test_publish_and_unpublish_one_destination_serialize():
    n, reason = _reason(_node("a", "video.publish", destination="tiktok"),
                        _node("b", "video.unpublish", destination="tiktok"))
    assert n == 1 and "video:publish:tiktok" in reason


def test_publishing_to_different_destinations_parallelizes():
    assert _released(_node("a", "video.publish", destination="tiktok"),
                     _node("b", "video.publish", destination="youtube")) == {"a", "b"}


def test_planning_review_coverage_are_read_only():
    for cap in ("video.plan_segments", "video.discover_references", "video.select_strategy",
                "video.review_segment", "video.check_coverage"):
        assert _SPECS[cap].concurrency_mode == "read_only", cap
    ros = [_node(f"n{i}", c) for i, c in enumerate(
        ["video.plan_segments", "video.review_segment", "video.check_coverage"])]
    assert len(_released(*ros)) == 3
