"""Multimodal creative intelligence (Market-Intelligence plan §7, Phase 2).

Turn a :class:`MediaArtifact` (a competitor's image/video creative) into a :class:`MediaAnalysis` — the visual
hooks, on-screen text, and CTA structure that DOM-only collection misses. A real analyzer wraps a
vision-capable model; the model call is injectable so the parsing/guard logic is fully testable offline.

Governance the schema already encodes and this honours (plan §7, §25):
  * **Observed vs derived.** ``on_screen_text`` / ``transcript`` are lifted from the media (``observed=True``);
    ``hooks`` / ``cta_structure`` are the model's *interpretation*. The result never presents inference as
    observation — an analysis with no model / no readable media returns ``observed=False`` and empty fields.
  * **Weak evidence.** A creative analysis is one input to pattern detection, never proof; first-party
    outcomes still supersede it downstream.

Image analysis is the Phase-2 focus (creatives are mostly images). Video keyframe extraction + transcription
plug in behind the same seam later; a video artifact without those configured yields ``observed=False``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from .contracts import MarketObservations, MediaAnalysis, MediaArtifact, Provenance


@runtime_checkable
class VisionModel(Protocol):
    """A vision-capable model: given an image reference (URL or data URI) and an instruction, return text."""
    def caption(self, image_ref: str, instruction: str) -> str: ...


@dataclass
class OpenAICompatVisionModel:
    """A VisionModel over any OpenAI-compatible /v1/chat/completions endpoint that accepts image content
    (e.g. a served Qwen-VL). Dependency-free (stdlib urllib); returns "" on any error so the analyzer
    degrades to an unobserved result rather than raising."""
    base_url: str
    model: str = ""
    api_key: str = ""
    timeout: float = 60.0
    max_tokens: int = 512

    def caption(self, image_ref: str, instruction: str) -> str:
        import urllib.request
        payload = {
            "model": self.model or "default",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": image_ref}},
            ]}],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        req = urllib.request.Request(f"{self.base_url.rstrip('/')}/v1/chat/completions",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        except Exception:  # noqa: BLE001
            return ""


_INSTRUCTION = (
    "You are analysing a marketing creative image. Return ONLY a JSON object, no prose:\n"
    '{"on_screen_text": "<verbatim text visible in the image, or empty>", '
    '"hooks": ["<the persuasive hooks / value propositions>"], '
    '"cta_structure": ["<calls to action shown, e.g. Start free trial>"]}\n'
    "Use only what is actually visible. If nothing is visible, use empty string / empty arrays.")


def _json_obj(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _strs(v) -> Tuple[str, ...]:
    if isinstance(v, list):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


@dataclass
class VisionMediaAnalyzer:
    """Analyze image creatives with a vision model. Self-skips (no model, or a non-image artifact, or an
    unparseable/empty model reply) to an ``observed=False`` analysis — never fabricates observed text."""
    model: Optional[VisionModel] = None
    provider: str = "internal.vision"

    def analyze(self, media: MediaArtifact) -> MediaAnalysis:
        ref = media.prov.provider_ref
        empty = MediaAnalysis(prov=Provenance(self.provider, f"{ref}#analysis"), media_ref=ref, observed=False)
        if self.model is None or media.media_kind != "image" or not media.url:
            return empty
        data = _json_obj(self.model.caption(media.url, _INSTRUCTION))
        if not data:
            return empty
        ost = str(data.get("on_screen_text", "") or "").strip()
        hooks, ctas = _strs(data.get("hooks")), _strs(data.get("cta_structure"))
        if not (ost or hooks or ctas):
            return empty
        return MediaAnalysis(
            prov=Provenance(self.provider, f"{ref}#analysis", evidence_refs=(media.url,)),
            media_ref=ref, on_screen_text=ost, hooks=hooks, cta_structure=ctas, observed=True)


def analyze_media(observations: MarketObservations, analyzer: VisionMediaAnalyzer) -> Tuple[MediaAnalysis, ...]:
    """Analyze every media artifact in an observation bundle; results carry ``observed=False`` where the media
    could not be read, so downstream pattern detection can weight them honestly."""
    return tuple(analyzer.analyze(m) for m in observations.media)
