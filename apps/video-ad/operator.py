"""agentic-video-ad as a Mission Runtime operator (production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) so the Mission Runtime can
drive video-ad production — segment planning, reference discovery, render, review, regenerate, assemble,
voice-over, subtitles, coverage check, publish — as discoverable, idempotent capabilities.

The capability handlers are the tested, pure `dict -> dict` functions in `reel.ad_operator`
(ffmpeg-mcp-aws/ai/sidekick-reel); the heavy media IO is injected via `reel.ad_media_io.build_media_io`
(ffmpeg-mcp-wrapper render / concat / minimax TTS / burn-captions). This app is deliberately THIN: it
binds handlers to capability specs and serves them. Mission Runtime owns lifecycle / HITL / saga /
replay; these capabilities only do domain work + call the injected reel surface.

Gating matches the plan's invariants:
  * video.review_segment / video.check_coverage  → approval_required (human review gates)
  * video.publish                                → approval_required + side_effecting + undo
                                                    (the one outward action; parks before it fires)

Run standalone:
    PYTHONPATH=<agentic-os>:<agentic-os>/apps:<ffmpeg-mcp-aws>/ai/sidekick-reel \
        python -m importlib ...   # or: uvicorn, see build_app() / __main__ below
"""
from __future__ import annotations

import os

from agentic_os.mission.operator_sdk import Operator, capability

# cross-repo: the ad domain + its media IO live with the render pipeline (sidekick-reel on PYTHONPATH).
from reel import ad_operator as OP


def _default_cr_chooser():
    """The production generation-chain chooser = Context Runtime's video_ad tenant. Lazy + guarded, so
    the operator still imports (with a fixed-default strategy) if contextos isn't present."""
    try:
        from reel.ad_cr import VideoAdChainOptimizer
        return VideoAdChainOptimizer().decision_for
    except Exception:
        return None


def build_video_ad_operator(io=None, chooser="__cr__") -> Operator:
    """The video-ad capability operator. `io` is a `reel.ad_operator.MediaIO`; defaults to the live one
    (real render/assemble/VO/subtitle tools + seeded-QA/VLM witnesses). `chooser(segment_id,intent) ->
    decision_dict` picks the generation chain for `video.select_strategy`; it defaults to the Context
    Runtime video_ad tenant (`__cr__`), so production learns per shot-complexity bucket. Tests inject a
    fake IO / fixed chooser to exercise the wire offline."""
    if io is None:
        from reel.ad_media_io import build_media_io
        io = build_media_io()
    if chooser == "__cr__":
        chooser = _default_cr_chooser()

    # Concurrency model: renders are GPU/provider-bound (bounded fan-out, NOT per-segment exclusive) so
    # different shots render together up to the cap; each write to one assembled cut is exclusive; a
    # publish/retract holds its destination alone. Planning/review/coverage read; confirm is mission state.
    RENDER = "provider:render"
    CUT = "video:cut:{cut_id}"
    PUB = "video:publish:{destination}"
    return Operator("video-ad", [
        capability("video.plan_segments", lambda i: OP.plan_segments_cap(i),
                   provides=["segments_planned"],
                   outputs={"segments": "shots grouped into ordered ~20s segments"},
                   estimated_value="high", concurrency_mode="read_only"),
        capability("video.discover_references", lambda i: OP.discover_references_cap(i, io),
                   provides=["references_proposed"],
                   outputs={"references": "web-search candidates (await user confirm) or direct user refs"},
                   concurrency_mode="read_only"),
        capability("video.confirm_references", lambda i: OP.confirm_references_cap(i),
                   provides=["references_acquired"],
                   outputs={"references": "the user-confirmed reference set (i2v anchors + accept criterion)"}),
        capability("video.select_strategy", lambda i: OP.select_strategy_cap(i, chooser),
                   provides=["strategy_selected"],
                   outputs={"artifact": "Context Runtime's chosen generation chain, stamped on the artifact"},
                   estimated_value="high", concurrency_mode="read_only"),
        capability("video.render_segment", lambda i: OP.render_segment_cap(i, io),
                   provides=["segment_rendered"],
                   outputs={"lineages": "one immutable ShotAttempt appended per shot"},
                   side_effecting=True, permissions=["media:render"], usd=0.50, latency_ms=1_200_000,
                   concurrency_key=RENDER, max_parallelism=2),
        capability("video.review_segment", lambda i: OP.review_segment_cap(i),
                   provides=["segment_reviewed"], approval_required=True,
                   outputs={"regen": "shots the human flagged for regeneration"},
                   estimated_value="high", concurrency_mode="read_only"),
        capability("video.regenerate_shots", lambda i: OP.regenerate_shots_cap(i, io),
                   provides=["segment_regenerated"],
                   outputs={"lineages": "a NEW immutable attempt for each flagged shot; old takes retained"},
                   side_effecting=True, permissions=["media:render"], usd=0.25, latency_ms=600_000,
                   concurrency_key=RENDER, max_parallelism=2),
        capability("video.assemble_reel", lambda i: OP.assemble_reel_cap(i, io),
                   provides=["reel_assembled"],
                   outputs={"cut_uri": "the accepted takes concatenated in order"},
                   permissions=["media:assemble"], concurrency_mode="exclusive", concurrency_key=CUT),
        capability("video.add_voiceover", lambda i: OP.add_voiceover_cap(i, io),
                   provides=["voiceover_added"], permissions=["media:assemble"], usd=0.05,
                   concurrency_mode="exclusive", concurrency_key=CUT),
        capability("video.add_subtitles", lambda i: OP.add_subtitles_cap(i, io),
                   provides=["subtitles_burned"], permissions=["media:assemble"],
                   concurrency_mode="exclusive", concurrency_key=CUT),
        capability("video.check_coverage", lambda i: OP.check_coverage_cap(i),
                   provides=["cut_reviewed"], approval_required=True,
                   outputs={"ok": "every material brief requirement executed / evidenced / waived"},
                   concurrency_mode="read_only"),
        capability("video.publish", lambda i: OP.publish_cap(i, io),
                   provides=["ad_published"], side_effecting=True, approval_required=True,
                   undo="video.unpublish", permissions=["social:write"],
                   outputs={"published": "the approved cut posted to the destination (with a receipt post_id)"},
                   estimated_value="high", concurrency_mode="exclusive", concurrency_key=PUB),
        capability("video.unpublish", lambda i: OP.unpublish_cap(i, io),
                   provides=["ad_unpublished"], side_effecting=True, permissions=["social:write"],
                   outputs={"unpublished": "retract the post (saga/undo) where the target allows"},
                   concurrency_mode="exclusive", concurrency_key=PUB),
    ])


def build_app():
    """A FastAPI app serving this operator's `/capabilities` + `/invoke` (mount into the app's server)."""
    from fastapi import FastAPI

    app = FastAPI(title="agentic-video-ad operator")
    app.include_router(build_video_ad_operator().router())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8218")))
