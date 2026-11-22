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

Trust model: the injected embedder is TRUSTED code. The guarantee that no synthetic buyer score becomes a
relevance value is ARCHITECTURAL (this module has no import path to the SSR scoring modules and only turns two
embedding vectors into a cosine); the source-prefix checks in ``RelevanceResult`` and ``apply_relevance`` catch
accidents and mislabelling, they do not defend against a hostile embedder (which could encode any number as an
angle between its output vectors).

The cosine → [0, 1] map is uncalibrated: see ``docs/SEARCH_RELEVANCE.md`` before relying on the 0.6 / 0.9
detector thresholds.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

if TYPE_CHECKING:  # typing only: no runtime dependency on the synthetic-research package
    from agentic_os.content.search_signals import SearchObservation
    from agentic_os.market.synthetic_research.embedder import TextEmbedder

__all__ = [
    "RelevanceResult", "PageText", "PageTextProvider", "make_relevance_fn", "sanitize_text",
    "page_text_from_html", "parse_relevance_source", "ParsedSource", "SIMILARITY_MAPS", "DEFAULT_SIMILARITY_MAP",
    "TEXT_POLICY_ID", "EMBEDDING_SOURCE_PREFIX",
]

EMBEDDING_SOURCE_PREFIX = "embedding:"
UNAVAILABLE_PREFIX = "unavailable:"
TEXT_POLICY_ID = "visible-text-v2"       # recorded in provenance: how the texts were reduced and bounded
_FORBIDDEN_SOURCE_PREFIXES = ("ssr", "synthetic")
_PROBE_TEXT = "relevance embedder fingerprint probe"
_PROBE_TEXT_2 = "a recipe for slow cooked tomato pasta with basil"   # must embed differently from _PROBE_TEXT
_META_CAP = 120
_SYNTHETIC_TOKEN = re.compile(r"[a-z0-9]+")

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
_MAX_HTML_CHARS = 200_000     # default input cap of page_text_from_html
_MAX_DEPTH = 256              # open-element stack bound: deeper documents are truncated, not processed
_MAX_TAGS = 20_000            # start tags examined; the rest of the document is ignored
_MAX_COLLECTED = 100_000      # characters of visible body text collected before parsing stops


class _VisibleText(HTMLParser):
    """Linear-time visible-text extractor. A hidden-depth COUNTER (not a scan of the stack) and a per-tag open
    count make every start tag, end tag and text chunk O(1) amortised; depth, tag count and collected text are
    all bounded, so no input can make it quadratic."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: List[str] = []
        self.h1: List[str] = []
        self.meta = ""
        self.body: List[str] = []
        self._collected = 0
        self._title_chars = 0
        self._h1_chars = 0
        self._stack: List[Tuple[str, bool]] = []     # (tag, hides its subtree)
        self._open: Dict[str, int] = {}
        self._hidden_depth = 0
        self._tags = 0
        self._stopped = False
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

    def _pop_one(self) -> str:
        tag, hides = self._stack.pop()
        self._open[tag] -= 1
        if hides:
            self._hidden_depth -= 1
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True
        return tag

    def handle_starttag(self, tag, attrs):
        if self._stopped:
            return
        self._tags += 1
        if self._tags > _MAX_TAGS:
            self._stopped = True
            return
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            if d.get("name", "").lower() == "description" and not self.meta:
                self.meta = d.get("content", "")[:2000]
            return
        if tag in _VOID:
            return
        if len(self._stack) >= _MAX_DEPTH:
            self._stopped = True                      # pathological nesting: keep what was parsed, ignore the rest
            return
        hides = self._hides(tag, attrs)
        self._stack.append((tag, hides))
        self._open[tag] = self._open.get(tag, 0) + 1
        if hides:
            self._hidden_depth += 1
        if tag == "h1" and not self._h1_done and self._hidden_depth == 0:
            self._in_h1 = True

    def handle_endtag(self, tag):
        if self._stopped:
            return
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if self._open.get(tag, 0) <= 0:
            return                                    # unmatched end tag: O(1), never scans the stack
        while self._pop_one() != tag:
            pass

    def handle_data(self, data):
        if self._stopped:
            return
        if self._in_title:
            if self._title_chars < 2000:
                self.title.append(data[:2000])
                self._title_chars += len(data[:2000])
            return
        if self._hidden_depth:
            return
        if self._in_h1 and self._h1_chars < 2000:
            self.h1.append(data[:2000])
            self._h1_chars += len(data[:2000])
        self.body.append(data[:_MAX_COLLECTED])
        self._collected += len(data)
        if self._collected > _MAX_COLLECTED:
            self._stopped = True


def page_text_from_html(html: object, *, max_html_chars: int = _MAX_HTML_CHARS) -> PageText:
    """Reduce an HTML string to a :class:`PageText` of VISIBLE text only: script/style/noscript/template/svg and
    elements marked hidden (``hidden``, ``aria-hidden="true"``, inline ``display:none`` / ``visibility:hidden``)
    are dropped, as are comments. Pure parsing — nothing is executed or fetched. Work is linear in the (capped)
    input: input over ``max_html_chars`` (default 200 000), nesting over 256 levels, more than 20 000 start tags
    or more than 100 000 characters of visible text are truncated, so hostile markup cannot cost more than a
    bounded amount of time. Never raises."""
    if not isinstance(html, str):
        return PageText()
    parser = _VisibleText()
    try:
        parser.feed(html[: max(0, min(int(max_html_chars), 4 * _MAX_HTML_CHARS))])
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup yields whatever was parsed so far
        pass
    return PageText(title="".join(parser.title), h1="".join(parser.h1), meta_description=parser.meta,
                    body_text=" ".join(parser.body))


# ── result ────────────────────────────────────────────────────────────────────────────────────────────────
def _hex_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _meta(text: object, cap: int = _META_CAP) -> str:
    """Provenance strings (embedder name/revision, source tails): plain, single-line, bounded."""
    return sanitize_text(str(text) if text is not None else "", cap)


def _synthetic_looking(text: str) -> bool:
    """True when any alphanumeric token of ``text`` (NFKC, lower case) starts with 'ssr' or 'synthetic'. This
    catches accidents and mislabelling only; it is not a defence against a hostile embedder (see module doc)."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return any(tok.startswith(_FORBIDDEN_SOURCE_PREFIXES) for tok in _SYNTHETIC_TOKEN.findall(folded))


MAX_SOURCE_CHARS = 300
_RESERVED_HEADS = ("embedding", "unavailable", "unset")
_LABEL_HEAD = re.compile(r"^[A-Za-z0-9_.-]+$")
_UNAVAILABLE_REASON = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")


@dataclass(frozen=True)
class ParsedSource:
    """A relevance source after validation. ``kind`` is ``unset`` | ``unavailable`` | ``embedding`` | ``labelled``
    | ``invalid``; ``text`` is the canonical form (reserved prefix in lower case) and is ``""`` when invalid, so a
    rejected raw string is never echoed into evidence."""
    kind: str
    text: str

    @property
    def valid(self) -> bool:
        return self.kind != "invalid"


_INVALID = ParsedSource("invalid", "")


def parse_relevance_source(raw: object) -> ParsedSource:
    """THE one validator for a relevance source token: used by ``RelevanceResult``, ``apply_relevance``, every
    detector and the evidence renderer. Never raises. It is strict and fail-closed (a source it cannot vouch for is
    ``invalid`` and read as UNKNOWN downstream, never as a measured value):

    * ``None`` / empty / ``unset`` (any case, surrounding blanks trimmed) is ``unset``;
    * not a string, longer than 300 characters (never truncated into acceptance), containing any control, format
      (zero-width, bidi), separator or internal whitespace character, or any ``ssr`` / ``synthetic`` token (NFKC
      folded): invalid;
    * the text before the first ``:`` must be plain ASCII ``[A-Za-z0-9_.-]`` and the text after it non-empty, so a
      unicode lookalike of ``embedding:`` / ``unavailable:`` can never impersonate a reserved prefix; a reserved
      word without a colon (``embedding``), ``unset:x``, an empty label (``embedding:``, ``supplied:``) and a
      duplicated or nested reserved prefix (``embedding:embedding:x``, ``supplied:unavailable:x``): invalid;
    * ``unavailable:<reason>`` needs a reason of ``[A-Za-z0-9_.:@/-]{1,200}``;
    * ``embedding:<name>@<revision>`` and any other ``<label>:<text>`` (for example ``supplied:crm``) or bare ASCII
      word are accepted as typed, except that the reserved prefixes are canonicalised to lower case."""
    if raw is None:
        return ParsedSource("unset", "unset")
    if not isinstance(raw, str) or len(raw) > 4 * MAX_SOURCE_CHARS:
        return _INVALID
    s = raw.strip()
    if not s:
        return ParsedSource("unset", "unset")
    if len(s) > MAX_SOURCE_CHARS:
        return _INVALID
    if any(unicodedata.category(ch)[0] in "CZ" for ch in s) or _synthetic_looking(s):
        return _INVALID
    head, sep, rest = s.partition(":")
    low = s.lower()
    if not sep:
        if low == "unset":
            return ParsedSource("unset", "unset")
        if low in _RESERVED_HEADS or not _LABEL_HEAD.match(s):
            return _INVALID
        return ParsedSource("labelled", s)
    hl = head.lower()
    if not _LABEL_HEAD.match(head) or not rest or hl == "unset":
        return _INVALID
    nested = rest.lower().startswith(tuple(h + ":" for h in _RESERVED_HEADS))
    if nested:
        return _INVALID
    if hl == "unavailable":
        return ParsedSource("unavailable", "unavailable:" + rest) if _UNAVAILABLE_REASON.match(rest) else _INVALID
    if hl == "embedding":
        return ParsedSource("embedding", "embedding:" + rest)
    return ParsedSource("labelled", s)


def _canonical_source(source: object) -> str:
    """Canonical text of a valid source, '' for an invalid one."""
    return parse_relevance_source(source).text


@dataclass(frozen=True)
class RelevanceResult:
    """One query-to-page relevance reading, or an explicit "unavailable".

    ``value`` is None exactly when ``source`` starts with ``unavailable:`` — never 0.0. A real value must come
    from an embedding (``source`` starts with ``embedding:``); a source naming a synthetic buyer score (``ssr…`` /
    ``synthetic…``) is refused at construction: synthetic buyer research is never a relevance input. Source
    prefixes are matched case-insensitively and stored in canonical lower case; name/revision/source are
    sanitised (no control, zero-width or bidi characters, bounded length). ``provenance_incomplete`` is True when
    the embedder revision could not be established."""
    value: Optional[float]
    source: str
    embedder_name: str = ""
    embedder_revision: str = ""
    page_text_hash: str = ""
    query_text_hash: str = ""
    similarity_map: str = ""
    text_policy: str = TEXT_POLICY_ID
    provenance_incomplete: bool = False

    def __post_init__(self) -> None:
        parsed = parse_relevance_source(self.source)
        object.__setattr__(self, "source", parsed.text)
        object.__setattr__(self, "embedder_name", _meta(self.embedder_name))
        object.__setattr__(self, "embedder_revision", _meta(self.embedder_revision))
        if not parsed.valid or parsed.kind in ("unset", "labelled") or _synthetic_looking(self.embedder_name):
            raise ValueError("RelevanceResult source must be a valid 'embedding:<name>@<rev>' or "
                             "'unavailable:<reason>' source; synthetic/SSR buyer scores are never a relevance value")
        if self.value is None:
            if parsed.kind != "unavailable":
                raise ValueError("a None relevance must carry source 'unavailable:<reason>'")
            return
        if parsed.kind == "unavailable":
            raise ValueError("an 'unavailable:*' source cannot carry a value (unknown is None, never 0.0)")
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
        parts = [f"embedder={self.embedder_name}", f"revision={self.embedder_revision}"]
        if self.provenance_incomplete:
            parts.append("provenance_incomplete=true")
        parts += [f"map={self.similarity_map}", f"text_policy={self.text_policy}",
                  f"page_text_sha256={self.page_text_hash}", f"query_text_sha256={self.query_text_hash}"]
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "embedder_name": self.embedder_name,
                "embedder_revision": self.embedder_revision, "page_text_hash": self.page_text_hash,
                "query_text_hash": self.query_text_hash, "similarity_map": self.similarity_map,
                "text_policy": self.text_policy, "provenance_incomplete": self.provenance_incomplete}


def _unavailable(reason: str, *, embedder_name: str = "", embedder_revision: str = "", page_hash: str = "",
                 query_hash: str = "", similarity_map: str = "", incomplete: bool = False) -> RelevanceResult:
    return RelevanceResult(None, UNAVAILABLE_PREFIX + reason, embedder_name, embedder_revision, page_hash,
                           query_hash, similarity_map, provenance_incomplete=incomplete)


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


def collapse_repeats(text: str) -> str:
    """Cheap anti-stuffing step for the page representation: drop every token that completes a word 3-gram
    already seen earlier in the same text (so a sentence, list or phrase repeated verbatim, or a word repeated in
    a run, counts once). Deterministic, linear. It does NOT stop a page that genuinely contains the query text
    once (that is relevant by this measure); see docs/SEARCH_RELEVANCE.md for the residual."""
    toks = text.split()
    out: List[str] = []
    seen = set()
    for i, tok in enumerate(toks):
        if i >= 2:
            gram = (toks[i - 2].lower(), toks[i - 1].lower(), tok.lower())   # window over the ORIGINAL sequence
            if gram in seen:
                continue
            seen.add(gram)
        out.append(tok)
    return " ".join(out)


# ── single flight and bounded caches ──────────────────────────────────────────────────────────────────────
MAX_RETRY_SECONDS = 300.0            # hard cap for failure TTLs and call timeouts
DEFAULT_RETRY_SECONDS = 30.0
DEFAULT_CALL_TIMEOUT_SECONDS = 120.0


def _cap_seconds(value: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, 0.0), MAX_RETRY_SECONDS) if math.isfinite(v) else default


class _Flight:
    __slots__ = ("done", "outcome")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.outcome: Optional[tuple] = None


class _SingleFlight:
    """Concurrent identical requests share ONE in-flight call. The first caller (the leader) runs ``fn`` in a daemon
    worker thread and waits at most ``timeout`` seconds; every other caller with the same key waits on the leader's
    outcome (also at most ``timeout``) and receives the SAME outcome, including the same failure. A hung call
    therefore blocks nobody forever: the leader and the waiters get ``("error", "timeout")``, the stale flight is
    dropped so the next request starts a new one, and a late result of the abandoned worker is discarded. Distinct
    keys never block each other (the lock only guards the table, never a call)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: Dict[object, _Flight] = {}

    def run(self, key: object, fn: Callable[[], tuple], timeout: float) -> tuple:
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
        if not leader:
            return flight.outcome if flight.done.wait(timeout) and flight.outcome is not None else ("error", "timeout")

        def work() -> None:
            try:
                out = fn()
            except BaseException as exc:  # noqa: BLE001 — fn should return outcomes; never lose a failure
                out = ("error", "exception:" + _safe_token(type(exc).__name__))
            with self._lock:
                if flight.outcome is None:           # an abandoned (timed-out) flight keeps its timeout outcome
                    flight.outcome = out

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout)
        with self._lock:
            if flight.outcome is None:
                flight.outcome = ("error", "timeout")
            if self._flights.get(key) is flight:
                del self._flights[key]
            out = flight.outcome
        flight.done.set()
        return out


# ── the hook ──────────────────────────────────────────────────────────────────────────────────────────────
def make_relevance_fn(embedder: "TextEmbedder", page_text_provider: PageTextProvider, *,
                      similarity_map: str = DEFAULT_SIMILARITY_MAP, embedder_revision: Optional[str] = None,
                      max_query_chars: int = 300, max_title_chars: int = 300, max_h1_chars: int = 300,
                      max_meta_chars: int = 500, max_body_chars: int = 2000,
                      cache_size: int = 256, probe_retry_seconds: float = DEFAULT_RETRY_SECONDS,
                      failure_ttl_seconds: float = DEFAULT_RETRY_SECONDS,
                      call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
                      _clock: Callable[[], float] = time.monotonic) -> Callable[["SearchObservation"], RelevanceResult]:
    """Build ``relevance_fn(observation) -> RelevanceResult`` for ``collect_search_observations``.

    The embedder revision recorded in every result is, in order: the ``embedder_revision`` argument, the
    embedder's own ``revision`` attribute, else a behavioural fingerprint ``probe:<sha256[:12]>`` of the
    embedding of a fixed probe string. The same probe embeds a second, unrelated text: an embedder that returns the
    same vector for both is reported as ``unavailable:degenerate_embedder`` (a constant embedder would otherwise
    score every pair 1.0). The embedder is trusted code; its name/revision are sanitised and a name containing an
    'ssr'/'synthetic' token is refused.

    SINGLE FLIGHT. Concurrent identical requests share one in-flight call: one provider call per page and one embed
    call per distinct text (key: embedder name + configured revision + normalised text hash), whatever the number of
    threads; waiters receive the leader's result or the same failure. A call that does not finish within
    ``call_timeout_seconds`` (default 120, capped at 300) yields ``unavailable:..._timeout`` for the leader and the
    waiters and never blocks others; distinct keys never block each other.

    BOUNDED FAILURE CACHE. A failed probe, provider call, embed call or timeout is remembered for
    ``probe_retry_seconds`` / ``failure_ttl_seconds`` (default 30 s, capped at 300 s, injectable clock), so a
    permanently failing dependency costs one call per interval, not one per row; a success afterwards repairs the
    state. Positive and negative caches hold at most ``cache_size`` entries each, so memory cannot grow with
    repeated failures. A failed probe leaves results ``provenance_incomplete``. Build a new function for each
    collection run so page text is never stale. Deterministic for a deterministic embedder and page provider."""
    if similarity_map not in SIMILARITY_MAPS:
        raise ValueError(f"unknown similarity_map {similarity_map!r}; known: {sorted(SIMILARITY_MAPS)}")
    to_unit = SIMILARITY_MAPS[similarity_map]
    name = _meta(getattr(embedder, "name", "")) or "unnamed-embedder"
    if _synthetic_looking(name):
        raise ValueError("embedder name looks like a synthetic/SSR scorer; relevance embedders are measurement "
                         "models only")
    given_rev = _meta(embedder_revision if embedder_revision else getattr(embedder, "revision", None) or "")
    name_token = re.sub(r"\s+", "_", name)
    probe_ttl = _cap_seconds(probe_retry_seconds, DEFAULT_RETRY_SECONDS)
    fail_ttl = _cap_seconds(failure_ttl_seconds, DEFAULT_RETRY_SECONDS)
    call_timeout = max(0.001, _cap_seconds(call_timeout_seconds, DEFAULT_CALL_TIMEOUT_SECONDS))
    flights = _SingleFlight()
    lock = threading.Lock()
    state: Dict[str, object] = {"probed": False, "degenerate": False, "probe_rev": ""}
    pos: Dict[object, object] = {}                       # successful results (page text / vectors)
    neg: Dict[object, Tuple[float, str]] = {}            # key -> (expires_at, failure reason)
    key_prefix = f"{name}|{given_rev or 'fingerprint'}|"

    def remember(key: object, outcome: tuple, ttl: float) -> None:
        with lock:
            if outcome[0] == "error" or outcome[0] == "none":
                if len(neg) >= cache_size:
                    now = _clock()
                    for k in [k for k, (exp, _) in neg.items() if exp <= now]:
                        del neg[k]
                    if len(neg) >= cache_size:
                        neg.clear()
                neg[key] = (_clock() + ttl, outcome[1] if outcome[0] == "error" else "none")
            else:
                if len(pos) >= cache_size:
                    pos.clear()
                pos[key] = outcome[1]
                neg.pop(key, None)

    def lookup(key: object):
        """('hit', value) | ('fail', reason) | None (miss or expired failure)."""
        with lock:
            if key in pos:
                return ("hit", pos[key])
            entry = neg.get(key)
            if entry is not None:
                if entry[0] > _clock():
                    return ("fail", entry[1])
                del neg[key]
        return None

    def fetch(key: object, fn: Callable[[], tuple], ttl: float) -> tuple:
        cached = lookup(key)
        if cached is not None:
            if cached[0] == "hit":
                return ("ok", cached[1])
            return ("none",) if cached[1] == "none" else ("error", cached[1])
        outcome = flights.run(key, fn, call_timeout)
        remember(key, outcome, ttl)
        return outcome

    def probe() -> None:
        """Establish the fingerprint revision and the degenerate-embedder verdict (one flight, bounded retries)."""
        with lock:
            if state["probed"]:
                return

        def run_probe() -> tuple:
            try:
                vecs = embedder.embed([_PROBE_TEXT, _PROBE_TEXT_2])
                if len(vecs) != 2:
                    return ("error", "bad_probe_shape")
                va, vb = [float(x) for x in vecs[0]], [float(x) for x in vecs[1]]
                cos = _cosine(va, vb)
                if cos is None:
                    return ("error", "degenerate_probe")
                digest = _hex_sha256(",".join(f"{x:.4f}" for x in va))[:12]
                return ("ok", (f"probe:{digest}", va == vb or cos >= 1.0 - 1e-12))
            except Exception as exc:  # noqa: BLE001
                return ("error", "probe_error:" + _safe_token(type(exc).__name__))
        outcome = fetch("probe", run_probe, probe_ttl)
        if outcome[0] == "ok":
            with lock:
                state["probe_rev"], state["degenerate"] = outcome[1]
                state["probed"] = True

    def revision() -> Tuple[str, bool]:
        if given_rev:
            return given_rev, False
        with lock:
            if state["probed"]:
                return str(state["probe_rev"]), False
        return "unrecorded", True

    def page_representation(page: PageText) -> str:
        parts: List[str] = []
        for text, bound in ((page.title, max_title_chars), (page.h1, max_h1_chars),
                            (page.meta_description, max_meta_chars), (page.body_text, max_body_chars)):
            clean = collapse_repeats(sanitize_text(text, bound))
            if clean and clean.lower() not in {p.lower() for p in parts}:
                parts.append(clean)
        return "\n".join(parts)

    def vector(text: str) -> tuple:
        def run_embed() -> tuple:
            try:
                vecs = embedder.embed([text])
                if len(vecs) != 1:
                    return ("error", "embedder_error:bad_shape")
                return ("ok", [float(x) for x in vecs[0]])
            except Exception as exc:  # noqa: BLE001
                return ("error", "embedder_error:" + _safe_token(type(exc).__name__))
        out = fetch(("vec", key_prefix + _hex_sha256(text)), run_embed, fail_ttl)
        return ("error", "embedder_timeout") if out == ("error", "timeout") else out

    def relevance_fn(observation: "SearchObservation") -> RelevanceResult:
        query = sanitize_text(getattr(observation, "query", ""), max_query_chars)
        q_hash = _hex_sha256(query) if query else ""
        common = {"embedder_name": name, "similarity_map": similarity_map}
        if not query:
            return _unavailable("empty_query", **common)
        url = str(getattr(observation, "page_url", "") or "")

        def run_page() -> tuple:
            try:
                pg = page_text_provider.page_text(url)
            except Exception as exc:  # noqa: BLE001 — never propagate provider failures as a number
                return ("error", "page_text_provider_error:" + _safe_token(type(exc).__name__))
            return ("ok", pg) if isinstance(pg, PageText) else ("none",)
        got = fetch(("page", url), run_page, fail_ttl)
        if got[0] == "error":
            reason = "page_text_provider_timeout" if got[1] == "timeout" else got[1]
            return _unavailable(reason, query_hash=q_hash, **common)
        if got[0] != "ok":
            return _unavailable("no_page_text", query_hash=q_hash, **common)
        page_text = page_representation(got[1])
        if not page_text:
            return _unavailable("no_page_text", query_hash=q_hash, **common)
        p_hash = _hex_sha256(page_text)
        probe()
        rev, incomplete = revision()
        common = {**common, "embedder_revision": rev if not incomplete else "unrecorded", "incomplete": incomplete}
        with lock:
            degenerate = bool(state["degenerate"])
        if degenerate:
            return _unavailable("degenerate_embedder", query_hash=q_hash, page_hash=p_hash, **common)
        qv = vector(query)
        if qv[0] != "ok":
            return _unavailable(qv[1], query_hash=q_hash, page_hash=p_hash, **common)
        pv = vector(page_text)
        if pv[0] != "ok":
            return _unavailable(pv[1], query_hash=q_hash, page_hash=p_hash, **common)
        cos = _cosine(qv[1], pv[1])
        if cos is None:
            return _unavailable("degenerate_embedding", query_hash=q_hash, page_hash=p_hash, **common)
        rev_token = re.sub(r"\s+", "_", rev)
        return RelevanceResult(to_unit(cos), f"{EMBEDDING_SOURCE_PREFIX}{name_token}@{rev_token}", name, rev, p_hash,
                               q_hash, similarity_map, provenance_incomplete=incomplete)

    def cache_info() -> Dict[str, int]:
        with lock:
            return {"cached": len(pos), "cached_failures": len(neg), "in_flight": len(flights._flights)}

    relevance_fn.cache_info = cache_info                  # type: ignore[attr-defined]
    return relevance_fn
