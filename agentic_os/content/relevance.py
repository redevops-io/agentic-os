"""Query-to-page relevance hook for the Search-Intelligence plane (SR-04). Optional, read-only, default OFF.

What it is: a deterministic, embedding-only estimate of how well a page's visible text (title + H1 + meta
description + a bounded body excerpt) matches a Search Console query, injected into
``collect_search_observations(..., relevance_fn=make_relevance_fn(embedder, page_text_provider))``.

What it is not: a ranking predictor, a buyer-persuasion score, or anything produced by synthetic respondents. The
value is a *search-matching* quantity only. This module deliberately imports NOTHING from the synthetic-research
scoring/study modules (a static test enforces it) and ``RelevanceResult`` refuses any source that looks like a
synthetic buyer score.

Safety properties:
  * No network, no file, no process access and no chat-model call: only ``embedder.embed`` is invoked. The page
    text comes from an injected :class:`PageTextProvider`; this module never fetches anything.
  * Page text is UNTRUSTED. It is reduced to plain visible text, length-bounded, stripped of control, zero-width,
    bidirectional and other format characters, and then only ever embedded — never executed, parsed for
    instructions or put into a prompt.
  * A failure or missing text yields ``value=None`` and ``source="unavailable:<reason>"`` — never ``0.0`` — so a
    detector can tell "unknown" from "measured low".
  * Every result records the embedder name + revision, the similarity map and the hashes of the exact texts that
    were embedded.

The cosine → [0, 1] map is uncalibrated: see ``docs/SEARCH_RELEVANCE.md`` before relying on the 0.6 / 0.9
detector thresholds.
"""
from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

if TYPE_CHECKING:  # typing only: no runtime dependency on the synthetic-research package
    from agentic_os.content.search_signals import SearchObservation
    from agentic_os.market.synthetic_research.embedder import TextEmbedder

__all__ = [
    "RelevanceResult", "PageText", "PageTextProvider", "make_relevance_fn", "sanitize_text",
    "page_text_from_html", "SIMILARITY_MAPS", "DEFAULT_SIMILARITY_MAP", "TEXT_POLICY_ID", "EMBEDDING_SOURCE_PREFIX",
]

EMBEDDING_SOURCE_PREFIX = "embedding:"
UNAVAILABLE_PREFIX = "unavailable:"
TEXT_POLICY_ID = "visible-text-v1"       # recorded in provenance: how the texts were reduced and bounded
_FORBIDDEN_SOURCE_PREFIXES = ("ssr", "synthetic")
_PROBE_TEXT = "relevance embedder fingerprint probe"

#: Documented monotone maps from cosine similarity c in [-1, 1] to a relevance in [0, 1]. All uncalibrated.
SIMILARITY_MAPS: Dict[str, Callable[[float], float]] = {
    # max(0, c): identity on the non-negative range (text-embedding cosines are almost always >= 0).
    "clamp01": lambda c: min(1.0, max(0.0, c)),
    # (1 + c) / 2: the half-cosine map (as in the SSR reference code); compresses everything into [0.5, 1] for
    # typical text embeddings, so most pairs would clear a 0.6 threshold. Provided for comparison only.
    "halfcosine": lambda c: min(1.0, max(0.0, (1.0 + c) / 2.0)),
}
DEFAULT_SIMILARITY_MAP = "clamp01"


# ── untrusted-text handling ───────────────────────────────────────────────────────────────────────────────
def sanitize_text(text: object, max_chars: int) -> str:
    """Plain, bounded, single-line text: NFC-normalised; control (Cc), format (Cf: zero-width, bidi controls,
    soft hyphen, BOM, tag characters), surrogate, private-use and unassigned code points removed; every
    whitespace/line/paragraph separator collapsed to one space; truncated to ``max_chars``. Returns "" for
    non-strings. It never raises."""
    if not isinstance(text, str) or max_chars <= 0:
        return ""
    text = unicodedata.normalize("NFC", text[: max_chars * 4])      # bound the work before scanning
    out: List[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\t\n\r\x0b\x0c" or cat in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split())[:max_chars].rstrip()


@dataclass(frozen=True)
class PageText:
    """Plain visible text of one page, as supplied by a :class:`PageTextProvider` (untrusted)."""
    title: str = ""
    h1: str = ""
    meta_description: str = ""
    body_text: str = ""


@runtime_checkable
class PageTextProvider(Protocol):
    """Supplies page text; injected. The hook never fetches. A real provider must be an approved read-only
    snapshotter (public pages, robots honoured, ``safe_url``) — none ships in this slice."""

    def page_text(self, page_url: str) -> Optional[PageText]:
        ...


_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_NEVER_VISIBLE = frozenset(("script", "style", "noscript", "template", "svg", "head", "iframe", "object"))


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: List[str] = []
        self.h1: List[str] = []
        self.meta = ""
        self.body: List[str] = []
        self._stack: List[Tuple[str, bool]] = []     # (tag, hides its subtree)
        self._in_title = False
        self._in_h1 = False
        self._h1_done = False

    @staticmethod
    def _hides(tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> bool:
        if tag in _NEVER_VISIBLE:
            return True
        d = {k.lower(): (v or "") for k, v in attrs}
        style = d.get("style", "").replace(" ", "").lower()
        return ("hidden" in d or d.get("aria-hidden", "").lower() == "true"
                or "display:none" in style or "visibility:hidden" in style)

    def _hidden(self) -> bool:
        return any(h for _, h in self._stack)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            if d.get("name", "").lower() == "description" and not self.meta:
                self.meta = d.get("content", "")
            return
        if tag in _VOID:
            return
        self._stack.append((tag, self._hides(tag, attrs)))
        if tag == "h1" and not self._h1_done and not self._hidden():
            self._in_h1 = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if self._hidden():
            return
        if self._in_h1:
            self.h1.append(data)
        self.body.append(data)


def page_text_from_html(html: object, *, max_html_chars: int = 1_000_000) -> PageText:
    """Reduce an HTML string to a :class:`PageText` of VISIBLE text only: script/style/noscript/template/svg and
    elements marked hidden (``hidden``, ``aria-hidden="true"``, inline ``display:none`` / ``visibility:hidden``)
    are dropped, as are comments. Pure parsing — nothing is executed or fetched. Never raises."""
    if not isinstance(html, str):
        return PageText()
    parser = _VisibleText()
    try:
        parser.feed(html[:max_html_chars])
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup yields whatever was parsed so far
        pass
    return PageText(title="".join(parser.title), h1="".join(parser.h1), meta_description=parser.meta,
                    body_text=" ".join(parser.body))


# ── result ────────────────────────────────────────────────────────────────────────────────────────────────
def _hex_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RelevanceResult:
    """One query-to-page relevance reading, or an explicit "unavailable".

    ``value`` is None exactly when ``source`` starts with ``unavailable:`` — never 0.0. A real value must come
    from an embedding (``source`` starts with ``embedding:``); a source naming a synthetic buyer score (``ssr…`` /
    ``synthetic…``) is refused at construction: synthetic buyer research is never a relevance input."""
    value: Optional[float]
    source: str
    embedder_name: str = ""
    embedder_revision: str = ""
    page_text_hash: str = ""
    query_text_hash: str = ""
    similarity_map: str = ""
    text_policy: str = TEXT_POLICY_ID

    def __post_init__(self) -> None:
        src = (self.source or "").strip().lower()
        if not src or src.startswith(_FORBIDDEN_SOURCE_PREFIXES):
            raise ValueError("RelevanceResult source must be a measurement source; synthetic/SSR buyer scores are "
                             "never a relevance value")
        if self.value is None:
            if not src.startswith(UNAVAILABLE_PREFIX) or src == UNAVAILABLE_PREFIX:
                raise ValueError("a None relevance must carry source 'unavailable:<reason>'")
            return
        if src.startswith(UNAVAILABLE_PREFIX):
            raise ValueError("an 'unavailable:*' source cannot carry a value (unknown is None, never 0.0)")
        if not src.startswith(EMBEDDING_SOURCE_PREFIX):
            raise ValueError("a relevance value must come from an embedding similarity ('embedding:<name>@<rev>')")
        if (isinstance(self.value, bool) or not isinstance(self.value, (int, float))
                or not math.isfinite(self.value) or not 0.0 <= self.value <= 1.0):
            raise ValueError("relevance value must be a finite float in [0, 1]")
        if not self.embedder_name or not self.embedder_revision:
            raise ValueError("a relevance value must record the embedder name and revision")

    @property
    def available(self) -> bool:
        return self.value is not None

    def provenance(self) -> str:
        """Deterministic one-line provenance copied onto the observation and into signal evidence (hashes only,
        never the page text)."""
        return (f"embedder={self.embedder_name}; revision={self.embedder_revision}; map={self.similarity_map}; "
                f"text_policy={self.text_policy}; page_text_sha256={self.page_text_hash}; "
                f"query_text_sha256={self.query_text_hash}")

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "embedder_name": self.embedder_name,
                "embedder_revision": self.embedder_revision, "page_text_hash": self.page_text_hash,
                "query_text_hash": self.query_text_hash, "similarity_map": self.similarity_map,
                "text_policy": self.text_policy}


def _unavailable(reason: str, *, embedder_name: str = "", embedder_revision: str = "", page_hash: str = "",
                 query_hash: str = "", similarity_map: str = "") -> RelevanceResult:
    return RelevanceResult(None, UNAVAILABLE_PREFIX + reason, embedder_name, embedder_revision, page_hash,
                           query_hash, similarity_map)


def _safe_token(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum() or ch in "_-")[:40] or "unknown"


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) != len(b) or not a:
        return None
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        x = float(x)
        y = float(y)
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return None
    return max(-1.0, min(1.0, dot / math.sqrt(na * nb)))


# ── the hook ──────────────────────────────────────────────────────────────────────────────────────────────
def make_relevance_fn(embedder: "TextEmbedder", page_text_provider: PageTextProvider, *,
                      similarity_map: str = DEFAULT_SIMILARITY_MAP, embedder_revision: Optional[str] = None,
                      max_query_chars: int = 300, max_title_chars: int = 300, max_h1_chars: int = 300,
                      max_meta_chars: int = 500, max_body_chars: int = 2000,
                      cache_size: int = 256) -> Callable[["SearchObservation"], RelevanceResult]:
    """Build ``relevance_fn(observation) -> RelevanceResult`` for ``collect_search_observations``.

    The embedder revision recorded in every result is, in order: the ``embedder_revision`` argument, the
    embedder's own ``revision`` attribute, else a behavioural fingerprint ``probe:<sha256[:12]>`` of the
    embedding of a fixed probe string (computed once). The function is deterministic for a deterministic
    embedder and page provider."""
    if similarity_map not in SIMILARITY_MAPS:
        raise ValueError(f"unknown similarity_map {similarity_map!r}; known: {sorted(SIMILARITY_MAPS)}")
    to_unit = SIMILARITY_MAPS[similarity_map]
    name = str(getattr(embedder, "name", "") or "unnamed-embedder")
    state: Dict[str, object] = {"revision": embedder_revision or getattr(embedder, "revision", None) or None}
    cache: Dict[str, List[float]] = {}

    def revision() -> str:
        if not state["revision"]:
            try:
                vec = embedder.embed([_PROBE_TEXT])[0]
                digest = _hex_sha256(",".join(f"{float(x):.4f}" for x in vec))[:12]
                state["revision"] = f"probe:{digest}"
            except Exception:  # noqa: BLE001
                state["revision"] = "unrecorded"
        return str(state["revision"])

    def page_representation(page: PageText) -> str:
        parts: List[str] = []
        for text, bound in ((page.title, max_title_chars), (page.h1, max_h1_chars),
                            (page.meta_description, max_meta_chars), (page.body_text, max_body_chars)):
            clean = sanitize_text(text, bound)
            if clean and clean.lower() not in {p.lower() for p in parts}:
                parts.append(clean)
        return "\n".join(parts)

    def relevance_fn(observation: "SearchObservation") -> RelevanceResult:
        query = sanitize_text(getattr(observation, "query", ""), max_query_chars)
        q_hash = _hex_sha256(query) if query else ""
        common = {"embedder_name": name, "similarity_map": similarity_map}
        if not query:
            return _unavailable("empty_query", **common)
        try:
            page = page_text_provider.page_text(str(getattr(observation, "page_url", "") or ""))
        except Exception as exc:  # noqa: BLE001 — never propagate provider failures as a number
            return _unavailable(f"page_text_provider_error:{_safe_token(type(exc).__name__)}",
                                query_hash=q_hash, **common)
        if not isinstance(page, PageText):
            return _unavailable("no_page_text", query_hash=q_hash, **common)
        page_text = page_representation(page)
        if not page_text:
            return _unavailable("no_page_text", query_hash=q_hash, **common)
        p_hash = _hex_sha256(page_text)
        try:
            need = [t for t in (query, page_text) if _hex_sha256(t) not in cache]
            if need:
                vecs = embedder.embed(need)
                if len(vecs) != len(need):
                    return _unavailable("embedder_error:bad_shape", query_hash=q_hash, page_hash=p_hash, **common)
                if len(cache) + len(need) > cache_size:
                    cache.clear()
                for t, v in zip(need, vecs):
                    cache[_hex_sha256(t)] = [float(x) for x in v]
            qv, pv = cache[_hex_sha256(query)], cache[_hex_sha256(page_text)]
        except Exception as exc:  # noqa: BLE001
            return _unavailable(f"embedder_error:{_safe_token(type(exc).__name__)}",
                                query_hash=q_hash, page_hash=p_hash, **common)
        cos = _cosine(qv, pv)
        rev = revision()
        if cos is None:
            return _unavailable("degenerate_embedding", embedder_revision=rev, query_hash=q_hash,
                                page_hash=p_hash, **common)
        return RelevanceResult(to_unit(cos), f"{EMBEDDING_SOURCE_PREFIX}{name}@{rev}", name, rev, p_hash, q_hash,
                               similarity_map)

    return relevance_fn
