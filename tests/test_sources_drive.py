"""Google Drive cloud-files connector, driven by a fake DriveClient (no live API, no token).
Proves OAuth-token-backed listing, content-type scoping, folder skipping, pagination, a
freshness fingerprint, per-file EvidenceRefs, error health, and that the token never leaks."""
from __future__ import annotations

import json

from agentic_os.sources import IndexingPolicy, ProposedSource, SourceHealthState, SourceKind
from agentic_os.sources_drive import (
    GoogleDriveHttpClient,
    GoogleDriveSourceConnector,
    drive_evidence,
)

TOKEN = "ya29.fake-oauth-token-DO-NOT-STORE"

# One page of Drive file metadata: a pdf, a Google Doc, a folder (skip), a png (skip by
# default content types), and a second page with a markdown file — exercises pagination.
PAGE1 = [
    {"id": "f1", "name": "Refund Policy.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-09-01T10:00:00Z"},
    {"id": "f2", "name": "Onboarding", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-09-02T10:00:00Z"},
    {"id": "d1", "name": "Archive", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2026-01-01T00:00:00Z"},
    {"id": "f3", "name": "logo.png", "mimeType": "image/png", "modifiedTime": "2026-08-01T10:00:00Z"},
]
PAGE2 = [
    {"id": "f4", "name": "notes.md", "mimeType": "text/markdown", "modifiedTime": "2026-09-03T10:00:00Z"},
]


class FakeDriveClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_files(self, folder_id, *, page_token=""):
        self.calls.append((folder_id, page_token))
        idx = 0 if not page_token else int(page_token)
        files = self.pages[idx]
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else ""
        return files, nxt


class Resolver:
    def __init__(self, token=TOKEN):
        self._token = token

    def resolve(self, ref):
        return {"access_token": self._token} if self._token else {}


def _connector(token=TOKEN, pages=(PAGE1, PAGE2)):
    fake = FakeDriveClient(list(pages))
    conn = GoogleDriveSourceConnector(resolver=Resolver(token), client_factory=lambda _tok: fake,
                                      clock=lambda: "2026-09-09T12:00:00Z")
    return conn, fake


def test_scans_drive_scoping_content_types_and_paginating():
    conn, fake = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="folder123")  # default content types
    cs = conn.connect_and_scan(spec, project_id="p", source_id="gd1", credential_ref="gdrive:cred")
    assert cs.kind is SourceKind.CLOUD_FILES and cs.provider == "google_drive"
    assert cs.health.state is SourceHealthState.HEALTHY
    # pdf + gdoc + md eligible; folder skipped (not counted as a file); png skipped
    assert cs.stats["discovered"] == 4 and cs.stats["indexed"] == 3 and cs.stats["skipped"] == 1
    assert cs.source_fingerprint
    assert fake.calls[0][0] == "folder123"  # scoped to the requested folder
    assert len(fake.calls) == 2  # followed the next-page token


def test_uses_the_oauth_token_and_never_leaks_it():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root")
    cs = conn.connect_and_scan(spec, project_id="p", source_id="gd1", credential_ref="gdrive:cred")
    assert cs.credential_ref == "gdrive:cred"  # addressed by ref…
    assert TOKEN not in json.dumps(cs.to_projection())  # …the token itself never lands on the source


def test_missing_token_is_error_health():
    conn, _ = _connector(token="")
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root")
    cs = conn.connect_and_scan(spec, project_id="p", source_id="gd1", credential_ref="gdrive:cred")
    assert cs.health.state is SourceHealthState.ERROR and "access token" in cs.health.detail


def test_content_type_allowlist_restricts():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", allowed_content_types=["pdf"])
    cs = conn.connect_and_scan(spec, project_id="p", source_id="gd1", credential_ref="gdrive:cred")
    assert cs.stats["indexed"] == 1  # only the pdf


def test_on_demand_indexing_defers():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", indexing_policy=IndexingPolicy.ON_DEMAND)
    cs = conn.connect_and_scan(spec, project_id="p", source_id="gd1", credential_ref="gdrive:cred")
    assert cs.stats["indexed"] == 0 and cs.stats["discovered"] == 4


def test_drive_evidence_is_one_ref_per_file():
    refs = drive_evidence("gd1", PAGE1 + PAGE2)
    assert refs[0].ref == "gdrive:f1" and refs[0].kind == "file"
    assert "Refund Policy.pdf" in refs[0].summary
    assert {r.kind for r in refs} == {"file"}


def test_http_client_sends_bearer_and_parses_files():
    seen = {}

    def http_get(url, headers):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization", "")
        return 200, {"files": [{"id": "x", "name": "a.pdf", "mimeType": "application/pdf"}], "nextPageToken": ""}

    client = GoogleDriveHttpClient(access_token=TOKEN, http_get=http_get)
    files, nxt = client.list_files("root")
    assert files[0]["id"] == "x" and nxt == ""
    assert seen["auth"] == f"Bearer {TOKEN}" and "drive/v3/files" in seen["url"]
