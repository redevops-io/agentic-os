"""Multimodal creative intelligence — VisionMediaAnalyzer (Market-Intelligence plan §7, Phase 2).

Offline with a stub vision model: an image creative yields observed on-screen text + derived hooks/CTAs;
a non-image / no-model / unparseable-reply case self-skips to observed=False (never fabricates observed text);
bulk anal_media honours the mix.

Run:  uv run python -m pytest tests/test_market_media.py -q
"""
from __future__ import annotations

from agentic_os.market import MarketObservations, VisionMediaAnalyzer, VisionModel, analyze_media
from agentic_os.market.contracts import MediaArtifact, Provenance


def _img(ref="acme:ad1", kind="image", url="https://acme.example/ad.png"):
    return MediaArtifact(prov=Provenance("website", ref), company_ref="acme", url=url, media_kind=kind)


class _StubVision:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def caption(self, image_ref, instruction):
        self.calls.append(image_ref)
        return self.reply


_GOOD = ('{"on_screen_text": "Ship agents in minutes", '
         '"hooks": ["speed", "no infra"], "cta_structure": ["Start free trial"]}')


def test_protocol_conformance():
    assert isinstance(_StubVision("{}"), VisionModel)


def test_image_yields_observed_text_and_derived_hooks():
    a = VisionMediaAnalyzer(model=_StubVision(_GOOD))
    r = a.analyze(_img())
    assert r.observed is True
    assert r.on_screen_text == "Ship agents in minutes"          # observed (verbatim from the creative)
    assert r.hooks == ("speed", "no infra") and r.cta_structure == ("Start free trial",)  # derived
    assert r.media_ref == "acme:ad1" and r.prov.evidence_refs == ("https://acme.example/ad.png",)


def test_no_model_self_skips_unobserved():
    r = VisionMediaAnalyzer(model=None).analyze(_img())
    assert r.observed is False and r.on_screen_text == "" and r.hooks == ()


def test_video_artifact_not_analyzed_by_image_analyzer():
    r = VisionMediaAnalyzer(model=_StubVision(_GOOD)).analyze(_img(kind="video"))
    assert r.observed is False                                    # image analyzer skips video (seam for later)


def test_unparseable_or_empty_reply_does_not_fabricate():
    assert VisionMediaAnalyzer(model=_StubVision("sorry, I can't")).analyze(_img()).observed is False
    assert VisionMediaAnalyzer(model=_StubVision('{"on_screen_text":"","hooks":[],"cta_structure":[]}')
                               ).analyze(_img()).observed is False


def test_analyze_media_bulk_mixed():
    obs = MarketObservations(media=(_img("a"), _img("b", kind="video")))
    results = analyze_media(obs, VisionMediaAnalyzer(model=_StubVision(_GOOD)))
    assert len(results) == 2
    assert [r.observed for r in results] == [True, False]         # image analyzed, video skipped
