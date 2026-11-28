"""Trusted ingestion adapters for the first-party metric ledger (SR-04, defence in depth).

A ``source`` label on a metric is just a string: it is NOT evidence that the number came from a first-party
sensor. The ledger therefore also requires an adapter attestation, produced only by a registered
:class:`SourceAdapter` for that source:

* ``register_source_adapter(source, adapter_id)`` registers an adapter for one of ``METRIC_SOURCES`` (and only
  those) and returns the :class:`SourceAdapter` that can stamp metrics. An id belongs to one source forever, the
  ``repo:`` prefix is reserved for the built-in adapters (created at import), registrations are capped, and nothing
  can be unregistered.
* ``adapter.make_metric(...)`` is the only supported factory: it builds the ``FirstPartyMetric`` and stamps it with
  ``attestation = HMAC(adapter key, source + adapter id + content digest)``. The digest covers every metric field
  and the whole provenance, so the stamp cannot be moved to a DIFFERENT metric and any later mutation invalidates
  it (an exact copy of a metric keeps a valid attestation, by design).
* ``FirstPartyLedger.record`` verifies the stamp, and so does every read from the ledger.

HONEST LIMITS. This is BYPASSABLE by code that runs in the same process (it can inspect the interpreter's objects,
call the factory from an unrelated module, or replace this module). These checks stop ACCIDENTAL and CASUAL misuse:
label-only mislabelling, copied attestations, tampering with a recorded metric, and calls that come (anywhere on the
call stack) from the synthetic-research package, which is refused outright. The signing keys live in a closure, not
in instance attributes, and adapters refuse to be pickled or copied. The structural protection is the import rule:
the SSR / study packages (``agentic_os/market/synthetic_research``) must not import this module, ``outcomes_join`` or
the outcome store, and importing them must not load these modules (subprocess test).

This module imports nothing from ``outcomes_join`` at import time (``outcomes_join`` imports it).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

METRIC_SOURCES = ("umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics")

#: The in-repo owner of each source: who WOULD register an adapter and mint metrics. Nothing in the repository
#: constructs a FirstPartyMetric today (see docs/ledger_source_inventory.md); `repo_adapter(source)` registers the
#: built-in adapter on first use, for in-repo composition and tests.
IN_REPO_SOURCE_OWNERS: Mapping[str, str] = {
    "umami": "agentic_os/integrations/umami.py (UmamiClient, page behaviour)",
    "gsc": "agentic_os/integrations/gsc.py (GscClient, search performance)",
    "marketplace": "agentic_os/market/marketplace.py (marketplace sales context)",
    "crm": "agentic_os/crm_outcome_live.py (CRM outcomes)",
    "billing": "apps/billing (billing decisions and outcomes)",
    "erp": "no in-repo producer",
    "analytics": "no in-repo producer (generic analytics import)",
}

_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")
_TOKEN = re.compile(r"[a-z0-9]+")
_SCHEME = "v1"
_BUILTIN_PREFIX = "repo:"
MAX_REGISTRATIONS = 64                      # caller registrations; the built-ins do not count
_STUDY_PACKAGE = "agentic_os.market.synthetic_research"
_LOCK = threading.Lock()
_IDS: Dict[str, str] = {}                   # adapter id -> source: an id belongs to ONE source, forever


class IngestionRefused(ValueError):
    """A metric or an adapter registration was refused by the ingestion rules."""


def _synthetic_name(text: str) -> bool:
    low = text.lower()
    return any(tok.startswith(("synthetic", "simulat", "study")) or tok == "ssr" for tok in _TOKEN.findall(low))


def refuse_study_caller(*, max_frames: int = 80) -> None:
    """Raise ``IngestionRefused`` if ANY frame on the current call stack belongs to the synthetic-research package.
    A study / SSR code path may not mint, register or record first-party metrics, directly or through an unrelated
    helper module it calls. Raises the bar; it cannot stop code that rewrites frames or modules."""
    frame = sys._getframe(1)
    for _ in range(max_frames):
        if frame is None:
            return
        name = frame.f_globals.get("__name__", "")
        if isinstance(name, str) and (name == _STUDY_PACKAGE or name.startswith(_STUDY_PACKAGE + ".")):
            raise IngestionRefused("the synthetic-research package may not mint, register or record first-party "
                                   "metrics")
        frame = frame.f_back


def content_digest(m: Any) -> str:
    """sha256 over every field of a FirstPartyMetric and its whole provenance (so an attestation is bound to the
    metric's content). Raises if the shape is not a plain metric."""
    prov = m.prov
    payload = [m.KIND, m.source, m.metric, repr(m.value), m.segment, m.surface, bool(m.higher_is_better),
               m.period_start_ms, m.period_end_ms, prov.provider, prov.provider_ref, list(prov.evidence_refs),
               prov.observed_at, prov.known_at]
    return hashlib.sha256(json.dumps(payload, default=repr, sort_keys=True).encode("utf-8")).hexdigest()


def _make_vault():
    """The signing keys live in this closure only: no module attribute, no instance attribute."""
    keys: Dict[Tuple[str, str], bytes] = {}

    def mac_input(source: str, adapter_id: str, digest: str) -> bytes:
        return f"{source}\n{adapter_id}\n{digest}".encode("utf-8")

    def create(source: str, adapter_id: str) -> Callable[[str], str]:
        key = secrets.token_bytes(32)
        keys[(source, adapter_id)] = key
        return lambda digest: hmac.new(key, mac_input(source, adapter_id, digest), hashlib.sha256).hexdigest()

    def verify(source: str, adapter_id: str, digest: str, mac: str) -> bool:
        key = keys.get((source, adapter_id))
        if key is None:
            return False
        want = hmac.new(key, mac_input(source, adapter_id, digest), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, mac)

    def registered() -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(keys))
    return create, verify, registered


_vault_create, _vault_verify, _vault_registered = _make_vault()


class SourceAdapter:
    """A registered producer of first-party metrics for one source. It can stamp metrics; it exposes no key, has no
    ``__dict__`` and refuses to be pickled or copied."""
    __slots__ = ("source", "adapter_id", "_sign")

    def __init__(self, source: str, adapter_id: str, sign: Callable[[str], str]) -> None:
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "_sign", sign)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SourceAdapter is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("SourceAdapter is immutable")

    def __repr__(self) -> str:                            # never leaks anything secret
        return f"SourceAdapter(source={self.source!r}, adapter_id={self.adapter_id!r})"

    def __reduce_ex__(self, protocol: int):
        raise TypeError("SourceAdapter cannot be pickled or copied")

    def __reduce__(self):
        raise TypeError("SourceAdapter cannot be pickled or copied")

    def __getstate__(self):
        raise TypeError("SourceAdapter has no serialisable state")

    def __copy__(self):
        raise TypeError("SourceAdapter cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("SourceAdapter cannot be copied")

    def attest(self, metric: Any) -> str:
        refuse_study_caller()
        return f"{_SCHEME}:{self.adapter_id}:{self._sign(content_digest(metric))}"

    def make_metric(self, *, metric: str, value: float, segment: str = "", surface: str = "",
                    higher_is_better: bool = True, period_start_ms: int = 0, period_end_ms: int = 0,
                    provider_ref: str = "", evidence_refs: Tuple[str, ...] = (), known_at: int = 0,
                    observed_at: Optional[int] = None) -> Any:
        """The supported factory: builds the FirstPartyMetric for this adapter's source and stamps it."""
        refuse_study_caller()
        from dataclasses import replace
        from ..integrations.business.contracts import Provenance, now_ms
        from .outcomes_join import FirstPartyMetric
        prov = Provenance(provider=self.adapter_id, provider_ref=provider_ref, evidence_refs=tuple(evidence_refs),
                          observed_at=now_ms() if observed_at is None else observed_at, known_at=known_at)
        m = FirstPartyMetric(prov=prov, source=self.source, metric=metric, value=value, segment=segment,
                             surface=surface, higher_is_better=higher_is_better, period_start_ms=period_start_ms,
                             period_end_ms=period_end_ms)
        return replace(m, attestation=self.attest(m))


_REPO_ADAPTERS: Dict[str, SourceAdapter] = {}


def _register(source: str, adapter_id: str, *, builtin: bool = False) -> SourceAdapter:
    """The single place a key is created; everything happens under ONE lock hold (check and insert are atomic)."""
    with _LOCK:
        if adapter_id in _IDS:
            raise IngestionRefused(f"adapter {adapter_id!r} is already registered"
                                   + (f" for source {_IDS[adapter_id]!r}" if _IDS[adapter_id] != source else
                                      f" for source {source!r}"))
        if not builtin and len(_IDS) - len(_REPO_ADAPTERS) >= MAX_REGISTRATIONS:
            raise IngestionRefused(f"adapter registration limit ({MAX_REGISTRATIONS}) reached")
        sign = _vault_create(source, adapter_id)
        _IDS[adapter_id] = source
        return SourceAdapter(source, adapter_id, sign)


def register_source_adapter(source: str, adapter_id: str, *, owner: str = "") -> SourceAdapter:
    """Register an ingestion adapter for ``source`` (which must be one of ``METRIC_SOURCES``). An id belongs to ONE
    source and can never be registered twice (not even for another source: ``repo:umami`` cannot be reused for
    ``billing``); the ``repo:`` prefix is reserved for the built-in adapters that exist from import time; at most
    ``MAX_REGISTRATIONS`` caller adapters can exist and there is no unregister. ``owner`` is a free-text audit note.
    Adding a NEW source name is not possible here: that needs a reviewed change to ``METRIC_SOURCES``. Refused when
    called from the synthetic-research package."""
    refuse_study_caller()
    if source not in METRIC_SOURCES:
        raise IngestionRefused(f"source {source!r} is not a registered first-party source; "
                               f"allowed: {', '.join(METRIC_SOURCES)}")
    if not isinstance(adapter_id, str) or not _ADAPTER_ID.match(adapter_id) or _synthetic_name(adapter_id):
        raise IngestionRefused("adapter_id must match [a-z][a-z0-9_.:-]{2,63} and must not name a synthetic / "
                               "simulated / study producer")
    if adapter_id.startswith(_BUILTIN_PREFIX):
        raise IngestionRefused("the 'repo:' adapter id prefix is reserved for the built-in adapters")
    return _register(source, adapter_id)


for _src in METRIC_SOURCES:                 # built-ins: one per source, created atomically at import
    _REPO_ADAPTERS[_src] = _register(_src, f"{_BUILTIN_PREFIX}{_src}", builtin=True)


def repo_adapter(source: str) -> SourceAdapter:
    """The built-in adapter (id ``repo:<source>``) for an in-repo producer of ``source``. They are created at import,
    so there is no first-use race and no id to squat. Refused when called from the synthetic-research package."""
    refuse_study_caller()
    try:
        return _REPO_ADAPTERS[source]
    except (KeyError, TypeError):
        raise IngestionRefused(f"source {source!r} is not a registered first-party source") from None


def registered_adapters() -> Tuple[Tuple[str, str], ...]:
    return _vault_registered()


def verify_attestation(metric: Any) -> None:
    """Raise ``IngestionRefused`` unless ``metric.attestation`` was produced by the registered adapter named in it
    for ``metric.source`` over THIS metric's current content."""
    att = getattr(metric, "attestation", "")
    if not isinstance(att, str) or not att.startswith(_SCHEME + ":") or att.count(":") < 2:
        raise IngestionRefused("metric has no adapter attestation: build it with a registered source adapter "
                               "(register_source_adapter(...).make_metric(...)); see docs/ledger_source_inventory.md")
    adapter_id, _, mac = att[len(_SCHEME) + 1:].rpartition(":")
    if _IDS.get(adapter_id) != metric.source:
        raise IngestionRefused(f"adapter {adapter_id!r} is not registered for source {metric.source!r}")
    if not _vault_verify(metric.source, adapter_id, content_digest(metric), mac):
        raise IngestionRefused("adapter attestation does not match this metric's content (copied or mutated)")
