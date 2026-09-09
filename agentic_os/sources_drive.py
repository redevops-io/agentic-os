"""Google Drive **cloud-files** context source — the convergence connector.

This is where the two governed acquisition paths meet in one flow:

* the **OAuth** machinery proved by the Connect acceptance path (a user authorizes once;
  a token is *fetched*, never pasted, and stored behind a ``CredentialRef``), and
* the **Sources** machinery (identify → authorize → scope → observe → evidence).

The connector consumes exactly the ``CredentialRef`` that ``LoopbackConnect`` would fetch
for Google — so OAuth-work and Sources-work are demonstrably one system, not two.

Decoupled from the Drive API via a :class:`DriveClient` seam (a ``list_files`` call): tests
drive a fake client with canned file listings; production uses :class:`GoogleDriveHttpClient`,
a thin stdlib-``urllib`` client that reads Drive v3 with the OAuth access token. The token is
resolved from the ``CredentialRef`` at the moment of use and never stored on the
:class:`~agentic_os.sources.ContextSource` or an :class:`~agentic_os.sources.EvidenceRef`.

This connector does **source connectivity** only — list, scope by content type, observe a
freshness fingerprint, and expose one :class:`EvidenceRef` per file. Extracting/indexing the
file *contents* (into BM25 / vector / graph) is **context materialization**, Context Runtime's
physical-plan choice, reached through the ``Indexer`` seam.
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

_FOLDER_MIME = "application/vnd.google-apps.folder"
# Drive mimeType → our content-type families (doc §7).
_MIME_TYPE: Dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.document": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "text/markdown": "markdown",
    "text/plain": "text",
    "text/csv": "csv",
    "application/vnd.google-apps.spreadsheet": "csv",
    "image/png": "images", "image/jpeg": "images",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DriveError(Exception):
    """The Drive API rejected a request (surfaced as ERROR health, never raised through)."""


class DriveClient(Protocol):
    """The seam. Returns one page of file metadata dicts (id, name, mimeType, modifiedTime)
    plus a next-page token ('' when done)."""

    def list_files(self, folder_id: str, *, page_token: str = "") -> Tuple[List[dict], str]: ...


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


@dataclass
class GoogleDriveHttpClient:
    """Live Drive v3 client (stdlib urllib). The access token is used at call time and never
    stored on the resulting source. ``http_get`` is injectable so it's testable offline."""

    access_token: str
    http_get: Callable[[str, Dict[str, str]], Tuple[int, dict]] = _urllib_get
    base: str = "https://www.googleapis.com/drive/v3"

    def list_files(self, folder_id: str, *, page_token: str = "") -> Tuple[List[dict], str]:
        import urllib.parse
        q = "trashed=false"
        if folder_id and folder_id != "root":
            q = f"'{folder_id}' in parents and trashed=false"
        params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
                  "pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token
        url = f"{self.base}/files?" + urllib.parse.urlencode(params)
        status, data = self.http_get(url, {"Authorization": f"Bearer {self.access_token}",
                                           "Accept": "application/json"})
        if status >= 400:
            raise DriveError(f"drive list failed: {status} {(data.get('error') or {}).get('message', '')}")
        return list(data.get("files", [])), data.get("nextPageToken", "") or ""


def drive_evidence(source_id: str, files: Sequence[dict]) -> List[EvidenceRef]:
    """One EvidenceRef per Drive file (``kind="file"``) — the evidence a Mission consumes; no
    content, no secret. Content extraction/indexing is a separate (materialization) concern."""
    return [
        EvidenceRef(source_id=source_id, ref=f"gdrive:{f.get('id', '')}", kind="file",
                    summary=f"{f.get('name', '(unnamed)')} · {_MIME_TYPE.get(f.get('mimeType', ''), 'file')}")
        for f in files
    ]


@dataclass
class GoogleDriveSourceConnector:
    """A read-only Google Drive SourceConnector. ``resolver`` resolves the credential ref to
    ``{"access_token": …}`` (the token a one-click Connect fetched); ``client_factory`` builds
    a :class:`DriveClient` from that token (defaults to the live HTTP client)."""

    resolver: Any
    client_factory: Callable[[str], DriveClient] = lambda token: GoogleDriveHttpClient(token)
    indexer: Indexer = field(default_factory=CountingIndexer)
    clock: Callable[[], str] = _now
    kind: SourceKind = SourceKind.CLOUD_FILES
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
                               "no access token — connect Google Drive first (OAuth)")

        client = self.client_factory(token)
        folder = spec.location or "root"
        discovered = 0
        eligible: List[str] = []
        skipped = 0
        seen: List[Tuple[str, str]] = []
        page = ""
        try:
            while True:
                files, page = client.list_files(folder, page_token=page)
                for f in files:
                    if f.get("mimeType") == _FOLDER_MIME:
                        continue  # flat scan — subfolders are not files
                    discovered += 1
                    ctype = _MIME_TYPE.get(f.get("mimeType", ""))
                    if ctype and ctype in allowed:
                        eligible.append(f.get("id", ""))
                        seen.append((f.get("name", ""), f.get("modifiedTime", "")))
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
            source_id=source_id, project_id=project_id, name=spec.display_name() or "Google Drive",
            kind=SourceKind.CLOUD_FILES, location=folder, provider="google_drive",
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.HEALTHY, f"{len(eligible)} eligible files", now),
            stats={"discovered": discovered, "indexed": indexed, "skipped": skipped},
            last_observed_at=now, source_fingerprint=fingerprint,
        )

    def _error(self, spec, project_id, source_id, credential_ref, grant, now, detail) -> ContextSource:
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name() or "Google Drive",
            kind=SourceKind.CLOUD_FILES, location=spec.location or "root", provider="google_drive",
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.ERROR, detail, now),
            stats={"discovered": 0, "indexed": 0, "skipped": 0}, last_observed_at=now,
        )
