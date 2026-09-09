"""Context Sources — the *evidence* side of Projects, first-class alongside Apps.

Apps answer "what can the system **do**?" (capabilities/actions, via the Integration
Plane). Sources answer "what may the system **know / use as evidence**?" — local files,
cloud drives, databases, and app-provided records fed to the Context Runtime / ReDevOps
RAG. The doc's guiding split (``redevops_projects_sidekick_enhanced_ui_apps_sources_context.md``):

    Sources define what evidence is available. Context Runtime decides how to
    retrieve and represent it.

So a :class:`ContextSource` here carries *identity, access, permissions, indexing/refresh
policy, and health* — never a retrieval strategy (BM25 vs vector vs SQL vs graph); that is
Context Runtime's per-task decision. And a **context grant** (:class:`SourceGrant`, what
evidence may be read) is deliberately separate from an app's **capability grant** (what
actions it may take): connecting HubSpot as an app does not imply permission to ingest all
of HubSpot as evidence.

Connecting a source is confirm-first, exactly like an integration:
:class:`SourceConnectionProposal` (model-authored, editable) → ``confirm()`` (the human
seal) → frozen, content-addressed :class:`ConfirmedSourceIntent` → a
:class:`SourceConnector` connects + scans it into a durable :class:`ContextSource`.

The connector is a seam. :class:`LocalFilesConnector` is real and stdlib-only — it walks a
folder and counts eligible files, so the local-file source works end-to-end today. The
actual embedding/index build is Context Runtime / redevops-rag's job, reached through the
injected :class:`Indexer` seam (a :class:`CountingIndexer` stands in until that is bound).
Database and cloud connectors are the documented plug-in points on the same seam.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from runtime_contracts.canonical import content_hash

CONTRACT_VERSION = "context-source/0.1"


# ── enums ─────────────────────────────────────────────────────────────────────
class SourceKind(str, Enum):
    FILES = "files"
    CLOUD_FILES = "cloud_files"
    DATABASE = "database"
    APP_EVIDENCE = "app_evidence"
    WEBSITE = "website"


class AccessMode(str, Enum):
    READ_ONLY = "read_only"
    ALLOW_GENERATED = "allow_generated"


class IndexingPolicy(str, Enum):
    AUTOMATIC = "automatic"
    ON_DEMAND = "on_demand"


class SourceHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    ERROR = "error"


# content-type inference for the file families the doc lists (§7)
_EXT_TYPE: Dict[str, str] = {
    ".pdf": "pdf", ".doc": "docx", ".docx": "docx", ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".rst": "text", ".csv": "csv", ".tsv": "csv",
    ".png": "images", ".jpg": "images", ".jpeg": "images", ".gif": "images", ".webp": "images",
    ".mp3": "audio_video", ".wav": "audio_video", ".mp4": "audio_video", ".mov": "audio_video",
}
_DEFAULT_CONTENT_TYPES: Tuple[str, ...] = ("pdf", "docx", "markdown", "text", "csv")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── the evidence grant (separate from an app's capability grant) ────────────────
@dataclass(frozen=True)
class SourceGrant:
    """What evidence may be read from a source — the *context* grant, distinct from an
    app's capability grant (doc §14). Scoping is per source family: paths for files,
    schemas/tables for databases; ``denied`` carves out sub-scopes (``billing.card_data``,
    ``hr.*``); ``access_mode`` is read-only unless generated files are explicitly allowed.
    """

    allowed_paths: Tuple[str, ...] = ()
    allowed_schemas: Tuple[str, ...] = ()
    allowed_tables: Tuple[str, ...] = ()
    allowed_content_types: Tuple[str, ...] = ()
    denied: Tuple[str, ...] = ()
    access_mode: AccessMode = AccessMode.READ_ONLY

    def canonical_form(self) -> Dict[str, object]:
        return {
            "allowed_paths": sorted(self.allowed_paths),
            "allowed_schemas": sorted(self.allowed_schemas),
            "allowed_tables": sorted(self.allowed_tables),
            "allowed_content_types": sorted(self.allowed_content_types),
            "denied": sorted(self.denied),
            "access_mode": self.access_mode.value,
        }

    def to_dict(self) -> Dict[str, object]:
        return self.canonical_form()

    @property
    def content_types_or_default(self) -> Tuple[str, ...]:
        return self.allowed_content_types or _DEFAULT_CONTENT_TYPES


@dataclass(frozen=True)
class SourceHealth:
    state: SourceHealthState
    detail: str = ""
    last_observed_at: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"state": self.state.value, "detail": self.detail, "last_observed_at": self.last_observed_at}


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer to one piece of evidence a source exposes — what a Mission consumes and what
    Projects shows under "Context used" (doc §11/§21). It is an addressable *reference* plus a
    human summary, never the content itself, and never a secret."""

    source_id: str
    ref: str            # e.g. "postgres:support.tickets" or "file:policy.pdf"
    kind: str           # "table" | "file" | "record" | …
    summary: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"source_id": self.source_id, "ref": self.ref, "kind": self.kind, "summary": self.summary}


# ── the durable source object (doc §9) ──────────────────────────────────────────
@dataclass(frozen=True)
class ContextSource:
    """A connected evidence source. No plaintext secret is ever stored here — a
    ``credential_ref`` points at the broker. ``source_fingerprint`` is content-addressed
    over what was observed, so Discovery can notice when a source changes."""

    source_id: str
    project_id: str
    name: str
    kind: SourceKind
    location: str
    provider: str = ""
    credential_ref: str = ""
    grant: SourceGrant = field(default_factory=SourceGrant)
    indexing_policy: IndexingPolicy = IndexingPolicy.AUTOMATIC
    refresh_policy: str = "on_change"
    exposure_class: str = "internal"
    health: SourceHealth = field(default_factory=lambda: SourceHealth(SourceHealthState.HEALTHY))
    stats: Mapping[str, object] = field(default_factory=dict)  # {discovered,indexed,skipped} | {schemas,tables}
    last_observed_at: str = ""
    source_fingerprint: str = ""
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        # Identity is the source's *definition* — not its volatile health/stats/timestamps.
        return {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "provider": self.provider,
            "location": self.location,
            "grant": self.grant.canonical_form(),
            "indexing_policy": self.indexing_policy.value,
            "exposure_class": self.exposure_class,
        }

    @property
    def definition_hash(self) -> str:
        return content_hash(self.canonical_form())

    def to_projection(self) -> Dict[str, object]:
        """The UI-facing ContextSourceProjection — business language, with provenance so the
        advanced view can drill into Context Runtime without the frontend knowing internals."""
        return {
            "source_id": self.source_id,
            "name": self.name,
            "kind": self.kind.value,
            "provider": self.provider,
            "location": self.location,
            "access_mode": self.grant.access_mode.value,
            "indexing_policy": self.indexing_policy.value,
            "refresh_policy": self.refresh_policy,
            "exposure_class": self.exposure_class,
            "health": self.health.to_dict(),
            "stats": dict(self.stats),
            "allowed_paths": list(self.grant.allowed_paths),
            "allowed_schemas": list(self.grant.allowed_schemas),
            "allowed_tables": list(self.grant.allowed_tables),
            "allowed_content_types": list(self.grant.content_types_or_default),
            "denied": list(self.grant.denied),
            "last_observed_at": self.last_observed_at,
            "source_fingerprint": self.source_fingerprint,
            "source_runtime": "context",
            "source_refs": [f"source:{self.source_id}"],
            "advanced_refs": [f"fingerprint:{self.source_fingerprint}"] if self.source_fingerprint else [],
        }


# ── confirm-first connection intent (mirrors IntegrationProposal) ────────────────
@dataclass
class ProposedSource:
    """One source the wizard proposes to add — editable before confirmation."""

    kind: SourceKind
    location: str
    name: str = ""
    provider: str = ""
    access_mode: AccessMode = AccessMode.READ_ONLY
    allowed_content_types: List[str] = field(default_factory=list)
    allowed_schemas: List[str] = field(default_factory=list)
    indexing_policy: IndexingPolicy = IndexingPolicy.AUTOMATIC

    def display_name(self) -> str:
        return self.name or os.path.basename(self.location.rstrip("/")) or self.location or self.kind.value

    def canonical_form(self) -> Dict[str, object]:
        return {
            "kind": self.kind.value, "location": self.location, "provider": self.provider,
            "access_mode": self.access_mode.value,
            "allowed_content_types": sorted(self.allowed_content_types),
            "allowed_schemas": sorted(self.allowed_schemas),
            "indexing_policy": self.indexing_policy.value,
        }

    def grant(self) -> SourceGrant:
        return SourceGrant(
            allowed_content_types=tuple(self.allowed_content_types),
            allowed_schemas=tuple(self.allowed_schemas),
            access_mode=self.access_mode,
        )


@dataclass(frozen=True)
class ConfirmedSourceIntent:
    """The frozen, content-addressed handoff into source connection — the human confirm is
    the seal, identity is over the meaning (which sources, how scoped), not who/when."""

    project_id: str
    sources: Tuple[ProposedSource, ...]
    confirmed_by: str = ""
    confirmed_at: str = ""
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "sources": sorted((s.canonical_form() for s in self.sources), key=lambda d: (d["kind"], d["location"])),
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.canonical_form())


@dataclass
class SourceConnectionProposal:
    """Model-authored, editable, NOT sealed — what Sidekick thinks the user wants to add as
    context. ``assumptions`` were inferred (reversible: read-only, content types); ``questions``
    are what can't be safely guessed (which tables, whether to allow generated files)."""

    project_id: str
    sources: List[ProposedSource] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)

    def confirm(self, *, confirmed_by: str, confirmed_at: str) -> ConfirmedSourceIntent:
        if self.questions:
            raise ValueError(
                "cannot confirm while questions are open: " + "; ".join(self.questions)
                + " — answer them (or drop them) before confirming"
            )
        return ConfirmedSourceIntent(
            project_id=self.project_id, sources=tuple(self.sources),
            confirmed_by=confirmed_by, confirmed_at=confirmed_at,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "project_id": self.project_id,
            "sources": [s.canonical_form() | {"name": s.display_name()} for s in self.sources],
            "assumptions": list(self.assumptions),
            "questions": list(self.questions),
        }


# ── the connector seam ───────────────────────────────────────────────────────
class Indexer(Protocol):
    """Turns a set of eligible files into a retrievable index. The real implementation is
    Context Runtime / redevops-rag; it returns how many items it indexed."""

    def index(self, project_id: str, source_id: str, paths: Sequence[str]) -> int: ...


@dataclass
class CountingIndexer:
    """A stand-in Indexer: counts eligible files (no embeddings). Bind a real Context
    Runtime / redevops-rag indexer in production for actual retrieval."""

    def index(self, project_id: str, source_id: str, paths: Sequence[str]) -> int:
        return len(list(paths))


class SourceConnector(Protocol):
    kind: SourceKind

    def connect_and_scan(self, spec: ProposedSource, *, project_id: str, source_id: str,
                         credential_ref: str = "") -> ContextSource: ...


@dataclass
class LocalFilesConnector:
    """Real, stdlib-only files connector: walks the folder, counts files whose extension
    maps to an allowed content type, and indexes the eligible ones through the Indexer seam.
    Read-only by default; symlinks are not followed. The fingerprint is content-addressed
    over the observed (relpath, size) set so a later rescan can detect drift."""

    kind: SourceKind = SourceKind.FILES
    indexer: Indexer = field(default_factory=CountingIndexer)
    clock: Callable[[], str] = _now
    max_files: int = 100_000

    def connect_and_scan(self, spec: ProposedSource, *, project_id: str, source_id: str,
                         credential_ref: str = "") -> ContextSource:
        grant = spec.grant()
        allowed = set(grant.content_types_or_default)
        root = os.path.expanduser(spec.location)
        now = self.clock()
        if not os.path.isdir(root):
            return ContextSource(
                source_id=source_id, project_id=project_id, name=spec.display_name(),
                kind=SourceKind.FILES, location=spec.location, grant=grant,
                indexing_policy=spec.indexing_policy,
                health=SourceHealth(SourceHealthState.ERROR, f"not a folder: {spec.location}", now),
                stats={"discovered": 0, "indexed": 0, "skipped": 0}, last_observed_at=now,
            )

        discovered = 0
        eligible: List[str] = []
        skipped = 0
        seen: List[Tuple[str, int]] = []
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                discovered += 1
                if discovered > self.max_files:
                    break
                full = os.path.join(dirpath, name)
                ctype = _EXT_TYPE.get(os.path.splitext(name)[1].lower())
                if ctype and ctype in allowed:
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        skipped += 1
                        continue
                    eligible.append(full)
                    seen.append((os.path.relpath(full, root), size))
                else:
                    skipped += 1
            if discovered > self.max_files:
                break

        indexed = self.indexer.index(project_id, source_id, eligible) if spec.indexing_policy == IndexingPolicy.AUTOMATIC else 0
        fingerprint = content_hash({"files": sorted(seen)})
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name(),
            kind=SourceKind.FILES, location=spec.location, credential_ref=credential_ref,
            grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.HEALTHY, f"{len(eligible)} eligible files", now),
            stats={"discovered": discovered, "indexed": indexed, "skipped": skipped},
            last_observed_at=now, source_fingerprint=fingerprint,
        )


@dataclass
class SourceConnectorRegistry:
    """kind → connector. Only ``files`` is real today; database/cloud bind here."""

    connectors: Dict[SourceKind, SourceConnector] = field(default_factory=dict)
    id_prefix: str = "src"
    _n: int = 0

    def register(self, connector: SourceConnector) -> "SourceConnectorRegistry":
        self.connectors[connector.kind] = connector
        return self

    @classmethod
    def default(cls) -> "SourceConnectorRegistry":
        return cls().register(LocalFilesConnector())

    def connect(self, intent: ConfirmedSourceIntent) -> List[ContextSource]:
        out: List[ContextSource] = []
        for spec in intent.sources:
            conn = self.connectors.get(spec.kind)
            self._n += 1
            sid = f"{self.id_prefix}-{self._n}"
            if conn is None:
                out.append(ContextSource(
                    source_id=sid, project_id=intent.project_id, name=spec.display_name(),
                    kind=spec.kind, location=spec.location, provider=spec.provider, grant=spec.grant(),
                    health=SourceHealth(SourceHealthState.DEGRADED,
                                        f"no connector for {spec.kind.value} yet — connect pending", _now()),
                    last_observed_at=_now(),
                ))
                continue
            out.append(conn.connect_and_scan(spec, project_id=intent.project_id, source_id=sid))
        return out
