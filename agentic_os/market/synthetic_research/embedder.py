"""Text embedders for SSR. agentic_os has no embedding client, so this defines the seam and two implementations.

* :class:`TextEmbedder` — a Protocol injected like ``app.transports.Transport`` (tests use a deterministic fake).
* :class:`SentenceTransformerEmbedder` — local, offline after the first model download; heavy imports are lazy.
* :class:`HttpEmbedder` — an OpenAI-compatible ``/v1/embeddings`` client that REFUSES any host that is not
  actually inside the trust boundary (``app.llm._is_in_boundary_host``): synthetic personas and page text derived
  from internal data must never reach an external embedding API (the paper's OpenAI model is external and paid,
  so it is NOT a default here). The endpoint must serve a DEDICATED embedding model: a chat model's
  ``/v1/embeddings`` output was measured to be non-discriminative for SSR (cosines 0.93–0.95 against all five
  anchors for both a positive and a negative response).
"""
from __future__ import annotations

import json
from typing import List, Protocol, Sequence, runtime_checkable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@runtime_checkable
class TextEmbedder(Protocol):
    name: str          # identifies the model in provenance; part of every study's reproducibility manifest
    # optional ``revision: str`` (model commit hash / version): recorded in every StudyManifest, "unrecorded" if absent

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        ...


class EmbedderRefused(RuntimeError):
    """The embedder cannot be used under the trust-boundary rules (fail closed)."""


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", *, device: str | None = None, revision: str = ""):
        self.model_name = model_name
        self.revision = revision          # the caller records the model commit hash it pinned (not resolved here)
        self.name = f"sentence-transformers:{model_name}"
        self._device = device
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer     # lazy: optional dependency
            except ImportError as exc:                                      # pragma: no cover - env dependent
                raise EmbedderRefused("sentence-transformers is not installed (optional 'rag' extra)") from exc
            self._model = SentenceTransformer(self.model_name, device=self._device)
        return self._model

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        out = self._load().encode(list(texts), convert_to_numpy=True, normalize_embeddings=False)
        return [[float(x) for x in row] for row in out]


class HttpEmbedder:
    """OpenAI-compatible embeddings over HTTP to an IN-BOUNDARY host only."""

    def __init__(self, base_url: str, model: str, *, timeout: float = 120.0, _opener=urlopen, revision: str = ""):
        from agentic_os.app.llm import _is_in_boundary_host
        if urlparse(base_url).scheme not in ("http", "https") or not _is_in_boundary_host(base_url):
            raise EmbedderRefused("embedding endpoint is not inside the trust boundary; refusing (fail closed)")
        self._url = base_url.rstrip("/") + "/v1/embeddings"
        self.model = model
        self.name = f"http-embeddings:{model}"
        self.revision = revision
        self._timeout = timeout
        self._open = _opener

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = Request(self._url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with self._open(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        if len(rows) != len(texts):
            raise EmbedderRefused("embedding endpoint returned a different number of vectors than inputs")
        return [[float(x) for x in r["embedding"]] for r in rows]


__all__ = ["TextEmbedder", "EmbedderRefused", "SentenceTransformerEmbedder", "HttpEmbedder"]
