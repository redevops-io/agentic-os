"""Microsoft OneDrive / SharePoint **cloud-files** context source — the Microsoft 365
SOURCE side, the mirror of :mod:`agentic_os.sources_drive` (Google Drive).

This is the SOURCE-role counterpart to the W2 APP adapter
(:class:`~agentic_os.integrations.microsoft_app.MicrosoftWorkbookDocsAdapter`): it consumes
exactly the ``microsoft:token`` :class:`CredentialRef` the Microsoft hosted-Connect stores —
so the OAuth-work (a user authorizes once; a token is *fetched*, never pasted) and the
Sources-work (identify → authorize → scope → observe → evidence) are demonstrably one system.

Decoupled from Microsoft Graph via a :class:`GraphFilesClient` seam (a ``list_files`` call):
tests drive a fake client with canned Graph listings; production uses
:class:`MicrosoftGraphFilesHttpClient`, a thin stdlib-``urllib`` client that reads Graph v1.0
with the OAuth access token. The token is resolved from the ``CredentialRef`` at the moment
of use and never stored on the :class:`~agentic_os.sources.ContextSource` or an
:class:`~agentic_os.sources.EvidenceRef`.

**OneDrive vs SharePoint** is expressed by the ``location``:

* OneDrive (the default) — a drive-item id or ``"root"``: children of ``/me/drive/root`` or
  ``/me/drive/items/{id}``.
* SharePoint — a ``sharepoint:{siteId}:{tail}`` location: the site's default document library
  (``/sites/{siteId}/drive/...``). A ``tail`` starting with ``/`` is a folder *path*
  (``/drive/root:{path}:/children``); otherwise it is a drive-item *id*; empty is the library
  root.

This connector does **source connectivity** only — list, scope by content type, observe a
freshness fingerprint, and expose one :class:`EvidenceRef` per file. Extracting/indexing the
file *contents* is **context materialization**, Context Runtime's choice, reached through the
``Indexer`` seam. Read-only.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Protocol, Sequence, Tuple

from runtime_contracts.canonical import content_hash

from .sources import (
    AccessMode,
    ContextSource,
    CountingIndexer,
    EvidenceRef,
    Indexer,
    IndexingPolicy,
    ProposedSource,
    SourceGrant,
    SourceHealth,
    SourceHealthState,
    SourceKind,
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Graph driveItem file.mimeType → our content-type families (doc §7).
_MIME_TYPE: Dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "text/markdown": "markdown",
    "text/plain": "text",
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "csv",
    "application/vnd.ms-excel": "csv",
    "image/png": "images", "image/jpeg": "images",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class GraphError(Exception):
    """Microsoft Graph rejected a request (surfaced as ERROR health, never raised through)."""


class GraphFilesClient(Protocol):
    """The seam. Returns one page of driveItem metadata dicts (id, name, file/folder facet,
    size, lastModifiedDateTime) plus a next-page token ('' when done)."""

    def list_files(self, folder: str, *, page_token: str = "") -> Tuple[List[dict], str]: ...


def _urllib_get(url: str, headers: Dict[str, str]) -> Tuple[int, dict]:  # pragma: no cover - live HTTP
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            return fh.status, _json.loads(fh.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _children_url(base: str, folder: str) -> str:
    """Map a ``location`` to a Graph ``children`` collection URL. OneDrive (``/me/drive``) is
    the default; a ``sharepoint:{siteId}:{tail}`` location targets a site document library."""
    import urllib.parse
    if folder.startswith("sharepoint:"):
        rest = folder[len("sharepoint:"):]
        site, _sep, tail = rest.partition(":")
        drive = f"{base}/sites/{urllib.parse.quote(site)}/drive"
    else:
        drive = f"{base}/me/drive"
        tail = folder
    if not tail or tail == "root":
        return f"{drive}/root/children"
    if tail.startswith("/"):  # a folder path within the drive
        path = urllib.parse.quote(tail)
        return f"{drive}/root:{path}:/children"
    return f"{drive}/items/{urllib.parse.quote(tail)}/children"  # a drive-item id


@dataclass
class MicrosoftGraphFilesHttpClient:
    """Live Graph v1.0 files client (stdlib urllib). The access token is used at call time and
    never stored on the resulting source. ``http_get`` is injectable so it's testable offline.
    Paginates via Graph's ``@odata.nextLink`` (a full URL carried back as the page token)."""

    access_token: str
    http_get: Callable[[str, Dict[str, str]], Tuple[int, dict]] = _urllib_get
    base: str = _GRAPH_BASE

    def list_files(self, folder: str, *, page_token: str = "") -> Tuple[List[dict], str]:
        url = page_token or _children_url(self.base, folder or "root")
        status, data = self.http_get(url, {"Authorization": f"Bearer {self.access_token}",
                                           "Accept": "application/json"})
        if status >= 400:
            raise GraphError(f"graph list failed: {status} "
                             f"{(data.get('error') or {}).get('message', '') if isinstance(data, dict) else ''}")
        return list(data.get("value", [])), data.get("@odata.nextLink", "") or ""


def graph_evidence(source_id: str, files: Sequence[dict]) -> List[EvidenceRef]:
    """One EvidenceRef per Graph file (``kind="file"``) — the evidence a Mission consumes; no
    content, no secret. Content extraction/indexing is a separate (materialization) concern."""
    return [
        EvidenceRef(source_id=source_id, ref=f"onedrive:{f.get('id', '')}", kind="file",
                    summary=f"{f.get('name', '(unnamed)')} · "
                            f"{_MIME_TYPE.get((f.get('file') or {}).get('mimeType', ''), 'file')}")
        for f in files
    ]


@dataclass
class MicrosoftGraphSourceConnector:
    """A read-only OneDrive / SharePoint SourceConnector. ``resolver`` resolves the credential
    ref to ``{"access_token": …}`` (the token a one-click Microsoft Connect fetched);
    ``client_factory`` builds a :class:`GraphFilesClient` from that token (defaults to the live
    HTTP client). Mirrors :class:`~agentic_os.sources_drive.GoogleDriveSourceConnector`."""

    resolver: Any
    client_factory: Callable[[str], GraphFilesClient] = lambda token: MicrosoftGraphFilesHttpClient(token)
    indexer: Indexer = field(default_factory=CountingIndexer)
    clock: Callable[[], str] = _now
    kind: SourceKind = SourceKind.CLOUD_FILES
    provider: str = "microsoft_onedrive"
    max_files: int = 5000

    def connect_and_scan(self, spec: ProposedSource, *, project_id: str, source_id: str,
                         credential_ref: str = "") -> ContextSource:
        grant = SourceGrant(allowed_content_types=tuple(spec.allowed_content_types),
                            access_mode=AccessMode.READ_ONLY)
        allowed = set(grant.content_types_or_default)
        now = self.clock()
        token = (dict(self.resolver.resolve(credential_ref)) if credential_ref else {}).get("access_token", "")
        if not token:
            return self._error(spec, project_id, source_id, credential_ref, grant, now,
                               "no access token — connect OneDrive first (OAuth)")

        client = self.client_factory(token)
        folder = spec.location or "root"
        discovered = 0
        eligible: List[str] = []
        skipped = 0
        seen: List[Tuple[str, str, int]] = []
        page = ""
        try:
            while True:
                files, page = client.list_files(folder, page_token=page)
                for f in files:
                    if f.get("folder") is not None:
                        continue  # flat scan — subfolders are not files
                    discovered += 1
                    ctype = _MIME_TYPE.get((f.get("file") or {}).get("mimeType", ""))
                    if ctype and ctype in allowed:
                        eligible.append(f.get("id", ""))
                        seen.append((f.get("name", ""), f.get("lastModifiedDateTime", ""),
                                     int(f.get("size", 0) or 0)))
                    else:
                        skipped += 1
                    if discovered >= self.max_files:
                        break
                if not page or discovered >= self.max_files:
                    break
        except Exception as e:
            return self._error(spec, project_id, source_id, credential_ref, grant, now,
                               f"list failed: {type(e).__name__}: {e}")

        indexed = self.indexer.index(project_id, source_id, eligible) \
            if spec.indexing_policy == IndexingPolicy.AUTOMATIC else 0
        fingerprint = content_hash({"files": sorted(seen)})
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name() or "OneDrive",
            kind=SourceKind.CLOUD_FILES, location=folder, provider=self.provider,
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.HEALTHY, f"{len(eligible)} eligible files", now),
            stats={"discovered": discovered, "indexed": indexed, "skipped": skipped},
            last_observed_at=now, source_fingerprint=fingerprint,
        )

    def _error(self, spec, project_id, source_id, credential_ref, grant, now, detail) -> ContextSource:
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name() or "OneDrive",
            kind=SourceKind.CLOUD_FILES, location=spec.location or "root", provider=self.provider,
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.ERROR, detail, now),
            stats={"discovered": 0, "indexed": 0, "skipped": 0}, last_observed_at=now,
        )
