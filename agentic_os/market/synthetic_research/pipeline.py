"""Glue: embed responses + anchors with an injected :class:`TextEmbedder` and score them with the pure SSR core."""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .anchors import AnchorPack
from .embedder import TextEmbedder
from .ssr import SSRResult, VARIANT_PAPER, ssr_score

_ANCHOR_CACHE: Dict[Tuple[str, str], List[List[List[float]]]] = {}


def embed_anchor_pack(pack: AnchorPack, embedder: TextEmbedder) -> List[List[List[float]]]:
    """Embeddings for every anchor of ``pack`` (sets x 5 x dim), cached per (embedder.name, pack content hash)."""
    key = (embedder.name, pack.content_hash)
    if key not in _ANCHOR_CACHE:
        flat = embedder.embed(pack.flat_texts())
        if len(flat) != len(pack.flat_texts()):
            raise ValueError("embedder returned the wrong number of anchor vectors")
        _ANCHOR_CACHE[key] = [flat[i * 5:(i + 1) * 5] for i in range(len(pack.sets))]
    return _ANCHOR_CACHE[key]


def score_responses(responses: Sequence[str], pack: AnchorPack, embedder: TextEmbedder, *,
                    variant: str = VARIANT_PAPER, epsilon: float = 0.0,
                    temperature: float = 1.0) -> List[SSRResult]:
    """Score free-text responses. Deterministic given the embedder; an empty/blank response is an error, not a 3."""
    for k, r in enumerate(responses):
        if not isinstance(r, str) or not r.strip():
            raise ValueError(f"response {k} is empty: refuse to score")
    if not responses:
        return []
    anchors = embed_anchor_pack(pack, embedder)
    vecs = embedder.embed(list(responses))
    if len(vecs) != len(responses):
        raise ValueError("embedder returned the wrong number of response vectors")
    return [ssr_score(v, anchors, variant=variant, epsilon=epsilon, temperature=temperature) for v in vecs]


__all__ = ["embed_anchor_pack", "score_responses"]
