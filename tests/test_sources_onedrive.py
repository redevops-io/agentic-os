"""Microsoft OneDrive / SharePoint cloud-files connector, driven by a fake GraphFilesClient
(no live API, no token). Proves OAuth-token-backed listing, content-type scoping, folder
skipping, pagination, a freshness fingerprint, per-file EvidenceRefs, error health, and that
the token never leaks — the Microsoft mirror of test_sources_drive.py."""
from __future__ import annotations

import json

from agentic_os.sources import (
    ConfirmedSourceIntent,
    IndexingPolicy,
    ProposedSource,
    SourceConnectorRegistry,
    SourceHealthState,
    SourceKind,
)
from agentic_os.sources_drive import GoogleDriveSourceConnector
from agentic_os.sources_onedrive import (
    MicrosoftGraphFilesHttpClient,
    MicrosoftGraphSourceConnector,
    graph_evidence,
)

TOKEN = "eyJ0.fake-graph-oauth-token-DO-NOT-STORE"

# One page of Graph driveItem metadata: a pdf, a Word doc, a subfolder (skip), a png (skip by
# default content types), and a second page with a markdown file — exercises pagination.
PAGE1 = [
    {"id": "f1", "name": "Refund Policy.pdf", "file": {"mimeType": "application/pdf"},
     "size": 2048, "lastModifiedDateTime": "2026-09-01T10:00:00Z"},
    {"id": "f2", "name": "Onboarding.docx",
     "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
     "size": 4096, "lastModifiedDateTime": "2026-09-02T10:00:00Z"},
    {"id": "d1", "name": "Archive", "folder": {"childCount": 3}, "size": 0,
     "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
    {"id": "f3", "name": "logo.png", "file": {"mimeType": "image/png"},
     "size": 512, "lastModifiedDateTime": "2026-08-01T10:00:00Z"},
]
PAGE2 = [
    {"id": "f4", "name": "notes.md", "file": {"mimeType": "text/markdown"},
     "size": 128, "lastModifiedDateTime": "2026-09-03T10:00:00Z"},
]


class FakeGraphClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_files(self, folder, *, page_token=""):
        self.calls.append((folder, page_token))
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
    fake = FakeGraphClient([list(p) for p in pages])
    conn = MicrosoftGraphSourceConnector(resolver=Resolver(token), client_factory=lambda _tok: fake,
                                         clock=lambda: "2026-09-09T12:00:00Z")
    return conn, fake


def test_scans_onedrive_scoping_content_types_and_paginating():
    conn, fake = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="folder123")  # default content types
    cs = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert cs.kind is SourceKind.CLOUD_FILES and cs.provider == "microsoft_onedrive"
    assert cs.health.state is SourceHealthState.HEALTHY
    # pdf + docx + md eligible; folder skipped (not counted as a file); png skipped
    assert cs.stats["discovered"] == 4 and cs.stats["indexed"] == 3 and cs.stats["skipped"] == 1
    assert cs.source_fingerprint
    assert fake.calls[0][0] == "folder123"  # scoped to the requested folder
    assert len(fake.calls) == 2  # followed the next-page token


def test_uses_the_oauth_token_and_never_leaks_it():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root")
    cs = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert cs.credential_ref == "microsoft:token"  # addressed by ref…
    assert TOKEN not in json.dumps(cs.to_projection())  # …the token itself never lands on the source


def test_missing_token_is_pending_error_health_no_crash():
    conn, _ = _connector(token="")
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root")
    cs = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert cs.health.state is SourceHealthState.ERROR and "access token" in cs.health.detail
    assert "OneDrive" in cs.health.detail


def test_content_type_allowlist_restricts():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", allowed_content_types=["pdf"])
    cs = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert cs.stats["indexed"] == 1  # only the pdf


def test_on_demand_indexing_defers():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", indexing_policy=IndexingPolicy.ON_DEMAND)
    cs = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert cs.stats["indexed"] == 0 and cs.stats["discovered"] == 4


def test_fingerprint_is_stable_and_changes_on_size_or_mtime():
    conn, _ = _connector()
    spec = ProposedSource(kind=SourceKind.CLOUD_FILES, location="root")
    a = conn.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    conn2, _ = _connector()
    b = conn2.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert a.source_fingerprint == b.source_fingerprint  # stable across identical rescans

    # a changed size shifts the fingerprint…
    p1 = [dict(PAGE1[0], size=99999), *PAGE1[1:]]
    conn3, _ = _connector(pages=(p1, PAGE2))
    c = conn3.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert c.source_fingerprint != a.source_fingerprint

    # …and so does a changed mtime
    p1b = [dict(PAGE1[0], lastModifiedDateTime="2026-12-31T00:00:00Z"), *PAGE1[1:]]
    conn4, _ = _connector(pages=(p1b, PAGE2))
    d = conn4.connect_and_scan(spec, project_id="p", source_id="od1", credential_ref="microsoft:token")
    assert d.source_fingerprint != a.source_fingerprint


def test_graph_evidence_is_one_ref_per_file():
    refs = graph_evidence("od1", [PAGE1[0], PAGE1[1], PAGE2[0]])
    assert refs[0].ref == "onedrive:f1" and refs[0].kind == "file"
    assert "Refund Policy.pdf" in refs[0].summary
    assert {r.kind for r in refs} == {"file"}


def test_http_client_sends_bearer_and_parses_value_onedrive_default():
    seen = {}

    def http_get(url, headers):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization", "")
        return 200, {"value": [{"id": "x", "name": "a.pdf", "file": {"mimeType": "application/pdf"}}],
                     "@odata.nextLink": ""}

    client = MicrosoftGraphFilesHttpClient(access_token=TOKEN, http_get=http_get)
    files, nxt = client.list_files("root")
    assert files[0]["id"] == "x" and nxt == ""
    assert seen["auth"] == f"Bearer {TOKEN}" and "/me/drive/root/children" in seen["url"]
    assert seen["url"].startswith("https://graph.microsoft.com/v1.0")


def test_http_client_routes_sharepoint_location_to_site_drive():
    seen = {}

    def http_get(url, headers):
        seen["url"] = url
        return 200, {"value": [], "@odata.nextLink": ""}

    client = MicrosoftGraphFilesHttpClient(access_token=TOKEN, http_get=http_get)
    # a SharePoint site library, addressed by a folder path within the site drive
    client.list_files("sharepoint:contoso.sharepoint.com,siteguid,webguid:/Shared Documents")
    assert "/sites/" in seen["url"] and "/drive/root:" in seen["url"]

    # …and by a drive-item id
    client.list_files("sharepoint:site-1:01ITEMID")
    assert "/sites/site-1/drive/items/01ITEMID/children" in seen["url"]


def test_registry_routes_cloud_files_by_provider_to_the_right_connector():
    """Google Drive and OneDrive both register as cloud_files; the spec's provider disambiguates.
    (The registry's connect() carries no credential_ref, so each cloud connector lands on its own
    graceful no-token path — what matters here is *which* connector was reached, i.e. the provider.)"""
    reg = (SourceConnectorRegistry()
           .register(GoogleDriveSourceConnector(resolver=Resolver(), client_factory=lambda _t: _DummyClient(),
                                                clock=lambda: "2026-09-09T12:00:00Z"))
           .register(MicrosoftGraphSourceConnector(resolver=Resolver(), client_factory=lambda _t: _DummyClient(),
                                                    clock=lambda: "2026-09-09T12:00:00Z")))
    (ms,) = reg.connect(ConfirmedSourceIntent(project_id="p", sources=(
        ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", provider="microsoft_onedrive"),)))
    assert ms.provider == "microsoft_onedrive"  # routed to the Microsoft connector…
    (gd,) = reg.connect(ConfirmedSourceIntent(project_id="p", sources=(
        ProposedSource(kind=SourceKind.CLOUD_FILES, location="root", provider="google_drive"),)))
    assert gd.provider == "google_drive"  # …and the same registry routes Google Drive to its own


class _DummyClient:
    def __init__(self):
        self.calls = []

    def list_files(self, folder, *, page_token=""):
        self.calls.append((folder, page_token))
        return [], ""
