"""Trusted ingestion adapters for the first-party metric ledger (SR-04 round 4, defence in depth).

A ``source`` label on a metric is just a string: it is NOT evidence that the number came from a first-party
sensor. The ledger therefore also requires an adapter attestation, produced only by a registered
:class:`SourceAdapter` for that source:

* ``register_source_adapter(source, adapter_id)`` registers an adapter for one of ``METRIC_SOURCES`` (and only
  those) and returns the :class:`SourceAdapter` that holds its private key. Re-registering an id is refused, so a
  registered adapter cannot be hijacked by registering it again.
* ``adapter.make_metric(...)`` is the only supported factory: it builds the ``FirstPartyMetric`` and stamps it with
  ``attestation = HMAC(adapter key, content digest of the metric)``. The digest covers every metric field and the
  whole provenance, so the stamp cannot be moved to another metric and any later mutation invalidates it.
* ``FirstPartyLedger.record`` verifies the stamp. A metric built by hand with ``source='umami'`` has none and is
  refused.

HONEST LIMITS. Python cannot make this unforgeable against code that runs in the same process (it can read the
registry or call ``register_source_adapter`` itself). The registry makes label-only and accidental mislabelling
impossible and makes every first-party writer an explicit, auditable registration; it is defence in depth. The hard
guarantee that synthetic research output stays out of the ledger is architectural: the SSR / study packages
(``agentic_os/market/synthetic_research``) must not import this module, ``outcomes_join`` or the outcome store (a
static test enforces and extends that rule).

This module imports nothing from ``outcomes_join`` at import time (``outcomes_join`` imports it).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

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
_LOCK = threading.Lock()
_KEYS: Dict[Tuple[str, str], bytes] = {}


class IngestionRefused(ValueError):
    """A metric or an adapter registration was refused by the ingestion rules."""


def _synthetic_name(text: str) -> bool:
    low = text.lower()
    return any(tok.startswith(("synthetic", "simulat", "study")) or tok == "ssr" for tok in _TOKEN.findall(low))


def content_digest(m: Any) -> str:
    """sha256 over every field of a FirstPartyMetric and its whole provenance (so an attestation is bound to the
    metric's content). Raises if the shape is not a plain metric."""
    prov = m.prov
    payload = [m.KIND, m.source, m.metric, repr(m.value), m.segment, m.surface, bool(m.higher_is_better),
               m.period_start_ms, m.period_end_ms, prov.provider, prov.provider_ref, list(prov.evidence_refs),
               prov.observed_at, prov.known_at]
    return hashlib.sha256(json.dumps(payload, default=repr, sort_keys=True).encode("utf-8")).hexdigest()


def _mac(key: bytes, digest: str) -> str:
    return hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()


class SourceAdapter:
    """A registered producer of first-party metrics for one source. Holds the private attestation key."""

    def __init__(self, source: str, adapter_id: str, key: bytes) -> None:
        self.source = source
        self.adapter_id = adapter_id
        self.__key = key

    def __repr__(self) -> str:                            # never leaks the key
        return f"SourceAdapter(source={self.source!r}, adapter_id={self.adapter_id!r})"

    def attest(self, metric: Any) -> str:
        return f"{_SCHEME}:{self.adapter_id}:{_mac(self.__key, content_digest(metric))}"

    def make_metric(self, *, metric: str, value: float, segment: str = "", surface: str = "",
                    higher_is_better: bool = True, period_start_ms: int = 0, period_end_ms: int = 0,
                    provider_ref: str = "", evidence_refs: Tuple[str, ...] = (), known_at: int = 0,
                    observed_at: Optional[int] = None) -> Any:
        """The supported factory: builds the FirstPartyMetric for this adapter's source and stamps it."""
        from dataclasses import replace
        from ..integrations.business.contracts import Provenance, now_ms
        from .outcomes_join import FirstPartyMetric
        prov = Provenance(provider=self.adapter_id, provider_ref=provider_ref, evidence_refs=tuple(evidence_refs),
                          observed_at=now_ms() if observed_at is None else observed_at, known_at=known_at)
        m = FirstPartyMetric(prov=prov, source=self.source, metric=metric, value=value, segment=segment,
                             surface=surface, higher_is_better=higher_is_better, period_start_ms=period_start_ms,
                             period_end_ms=period_end_ms)
        return replace(m, attestation=self.attest(m))


_BUILTIN_PREFIX = "repo:"
MAX_REGISTRATIONS = 64                      # caller registrations; the built-ins do not count
_IDS: Dict[str, str] = {}                   # adapter id -> source: an id belongs to ONE source, forever (no unregister)
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
        key = secrets.token_bytes(32)
        _KEYS[(source, adapter_id)] = key
        _IDS[adapter_id] = source
        return SourceAdapter(source, adapter_id, key)


def register_source_adapter(source: str, adapter_id: str, *, owner: str = "") -> SourceAdapter:
    """Register an ingestion adapter for ``source`` (which must be one of ``METRIC_SOURCES``). An id belongs to ONE
    source and can never be registered twice (not even for another source: ``repo:umami`` cannot be reused for
    ``billing``); the ``repo:`` prefix is reserved for the built-in adapters that exist from import time; at most
    ``MAX_REGISTRATIONS`` caller adapters can exist and there is no unregister. ``owner`` is a free-text audit note.
    Adding a NEW source name is not possible here: that needs a reviewed change to ``METRIC_SOURCES``."""
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
    so there is no first-use race and no id to squat."""
    try:
        return _REPO_ADAPTERS[source]
    except (KeyError, TypeError):
        raise IngestionRefused(f"source {source!r} is not a registered first-party source") from None


def registered_adapters() -> Tuple[Tuple[str, str], ...]:
    with _LOCK:
        return tuple(sorted(_KEYS))


def verify_attestation(metric: Any) -> None:
    """Raise ``IngestionRefused`` unless ``metric.attestation`` was produced by the registered adapter named in it
    for ``metric.source`` over THIS metric's current content."""
    att = getattr(metric, "attestation", "")
    if not isinstance(att, str) or not att.startswith(_SCHEME + ":") or att.count(":") < 2:
        raise IngestionRefused("metric has no adapter attestation: build it with a registered source adapter "
                               "(register_source_adapter(...).make_metric(...)); see docs/ledger_source_inventory.md")
    adapter_id, _, mac = att[len(_SCHEME) + 1:].rpartition(":")
    parts = (_SCHEME, adapter_id, mac)
    with _LOCK:
        key = _KEYS.get((metric.source, parts[1]))
    if key is None:
        raise IngestionRefused(f"adapter {parts[1]!r} is not registered for source {metric.source!r}")
    if not hmac.compare_digest(_mac(key, content_digest(metric)), parts[2]):
        raise IngestionRefused("adapter attestation does not match this metric's content (copied or mutated)")
