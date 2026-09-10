"""W1 — Google Workspace documents App adapter + hosted Connect, driven by a FAKE client
(no network, no token). Proves: the advertised capability surface (name/write); a governed
sheet.write reconciles found=True via observe; a write with no envelope is refused; document.create
returns a documentId; the credential token is resolved at use and never stored/leaked; and the
hosted Connect builds a `google` OAuth app targeting Google with the hosted callback redirect."""
from __future__ import annotations

import json

from agentic_os.integrations.execution import GovernedEnvelope
from agentic_os.integrations.google_app import (
    GoogleDocsSheetsHttpClient,
    GoogleWorkspaceDocsAdapter,
    google_docs_adapter,
)
from agentic_os.integrations.hosted_oauth import (
    ProviderOAuthApp,
    oauth_apps_from_env,
)

TOKEN = "ya29.fake-oauth-token-DO-NOT-STORE"


class FakeDocsSheets:
    """A fake GoogleDocsSheetsClient with an in-memory store. Records calls; no network."""

    def __init__(self):
        self.calls = []
        self.sheets = {}      # (sid, range) -> values
        self.docs = {}        # doc_id -> title
        self._doc_seq = 0

    def get_values(self, spreadsheet_id, a1_range):
        self.calls.append(("get_values", spreadsheet_id, a1_range))
        vals = self.sheets.get((spreadsheet_id, a1_range))
        # Sheets echoes the range even when empty; a wholly-unknown sheet raises (as the API 404s).
        if vals is None and spreadsheet_id not in {s for (s, _r) in self.sheets}:
            from agentic_os.integrations.google_app import GoogleAppError
            raise GoogleAppError("Requested entity was not found.")
        return {"range": a1_range, "majorDimension": "ROWS", "values": vals or []}

    def write_values(self, spreadsheet_id, a1_range, values, *, mode="update"):
        self.calls.append(("write_values", spreadsheet_id, a1_range, mode))
        self.sheets[(spreadsheet_id, a1_range)] = [list(r) for r in values]
        return {"spreadsheetId": spreadsheet_id, "updatedRange": a1_range,
                "updatedCells": sum(len(r) for r in values)}

    def create_document(self, title):
        self.calls.append(("create_document", title))
        self._doc_seq += 1
        doc_id = f"doc-{self._doc_seq}"
        self.docs[doc_id] = title
        return {"documentId": doc_id, "title": title}

    def get_document(self, document_id):
        self.calls.append(("get_document", document_id))
        if document_id not in self.docs:
            from agentic_os.integrations.google_app import GoogleAppError
            raise GoogleAppError("not found")
        return {"documentId": document_id, "title": self.docs[document_id]}


class Resolver:
    def __init__(self, token=TOKEN):
        self._token = token

    def resolve(self, ref):
        return {"access_token": self._token} if self._token else {}


def _adapter(token=TOKEN):
    fake = FakeDocsSheets()
    ad = google_docs_adapter(Resolver(token), credential_ref="google:token",
                             client_factory=lambda _tok: fake)
    return ad, fake


def _envelope(cap):
    return GovernedEnvelope(intent_hash="h" * 64, capability=cap, provider="google", tier=2)


# ── capability surface ──────────────────────────────────────────────────────────────────
def test_capabilities_shape_name_and_write():
    ad, _ = _adapter()
    assert ad.provider == "google"
    caps = {c.name: c.write for c in ad.capabilities()}
    assert caps == {"sheet.read": False, "sheet.write": True, "document.create": True}


# ── sheet.write under a governed envelope → ok + id → observe reconciles ─────────────────
def test_sheet_write_under_envelope_then_reconciles():
    ad, fake = _adapter()
    req = {"spreadsheet_id": "sheet-1", "range": "Sheet1!A1:B1", "values": [["Q3", 42]]}
    res = ad.execute("sheet.write", req, _envelope("sheet.write"))
    assert res.ok and res.error == ""
    assert res.provider_object_id == "sheet:sheet-1:Sheet1!A1:B1"
    # no action is complete until observed — re-reading the range finds it
    obs = ad.observe(res.provider_object_id)
    assert obs.found is True
    assert fake.sheets[("sheet-1", "Sheet1!A1:B1")] == [["Q3", 42]]


def test_sheet_write_refuses_without_envelope():
    ad, fake = _adapter()
    res = ad.execute("sheet.write", {"spreadsheet_id": "sheet-1", "range": "A1", "values": [["x"]]}, None)
    assert not res.ok and "envelope required" in res.error
    assert fake.calls == []   # refused before any client call


def test_sheet_read_needs_no_envelope():
    ad, fake = _adapter()
    fake.sheets[("sheet-1", "Sheet1!A1:B1")] = [["Q3", 42]]
    res = ad.execute("sheet.read", {"spreadsheet_id": "sheet-1", "range": "Sheet1!A1:B1"}, None)
    assert res.ok and res.data["values"] == [["Q3", 42]]
    assert res.provider_object_id == "sheet:sheet-1:Sheet1!A1:B1"


# ── document.create → documentId ─────────────────────────────────────────────────────────
def test_document_create_returns_document_id_and_reconciles():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"title": "Q3 Board Memo"}, _envelope("document.create"))
    assert res.ok and res.data["documentId"] == "doc-1"
    assert res.provider_object_id == "doc:doc-1"
    obs = ad.observe(res.provider_object_id)
    assert obs.found is True


def test_document_create_refuses_without_envelope():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"title": "x"}, None)
    assert not res.ok and "envelope required" in res.error


# ── an unadvertised capability is an honest failure, not a silent no-op ──────────────────
def test_unsupported_capability_is_reported():
    ad, _ = _adapter()
    res = ad.execute("slides.create", {}, _envelope("slides.create"))
    assert not res.ok and "does not implement" in res.error


# ── observe of a missing / unknown object is not-found, never a raise ────────────────────
def test_observe_missing_is_not_found():
    ad, _ = _adapter()
    assert ad.observe("").found is False
    assert ad.observe("doc:nope").found is False
    assert ad.observe("sheet:unknown:A1").found is False


# ── the token is resolved at use and never stored on the adapter or a result ─────────────
def test_token_is_never_stored_or_leaked():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"title": "memo"}, _envelope("document.create"))
    blob = json.dumps({"adapter_ref": ad.credential_ref, "result_id": res.provider_object_id,
                       "result_data": res.data})
    assert TOKEN not in blob                    # token never lands on the adapter/result
    assert ad.credential_ref == "google:token"  # addressed by ref only


def test_missing_token_is_a_clean_failure():
    ad, _ = _adapter(token="")
    res = ad.execute("sheet.read", {"spreadsheet_id": "s", "range": "A1"}, None)
    assert not res.ok and "access token" in res.error
    assert not ad.connect({}, "google:token").connected


def test_connect_and_health_when_token_resolves():
    ad, _ = _adapter()
    assert ad.connect({}, "google:token").connected is True
    assert ad.health().healthy is True


# ── the thin HTTP client sends a Bearer token and hits the right Sheets/Docs endpoints ───
def test_http_client_sends_bearer_and_targets_the_apis():
    seen = []

    def http_request(method, url, headers, body=None):
        seen.append((method, url, headers.get("Authorization", ""), body))
        if "/values/" in url and method == "GET":
            return 200, {"range": "A1", "values": [["hi"]]}
        if "/values/" in url:  # PUT update
            return 200, {"updatedRange": "A1", "updatedCells": 1}
        if url.endswith("/documents") and method == "POST":
            return 200, {"documentId": "d1", "title": "T"}
        return 200, {}

    client = GoogleDocsSheetsHttpClient(access_token=TOKEN, http_request=http_request)
    client.get_values("s1", "A1")
    client.write_values("s1", "A1", [["hi"]])
    client.create_document("T")
    assert all(auth == f"Bearer {TOKEN}" for (_m, _u, auth, _b) in seen)
    assert any("sheets.googleapis.com/v4/spreadsheets" in u for (_m, u, _a, _b) in seen)
    assert any("docs.googleapis.com/v1/documents" in u for (_m, u, _a, _b) in seen)


def test_http_client_append_mode_uses_append_endpoint():
    seen = {}

    def http_request(method, url, headers, body=None):
        seen["method"], seen["url"] = method, url
        return 200, {"updates": {"updatedRange": "A2", "updatedCells": 1}}

    client = GoogleDocsSheetsHttpClient(access_token=TOKEN, http_request=http_request)
    client.write_values("s1", "A1", [["x"]], mode="append")
    assert seen["method"] == "POST" and ":append" in seen["url"]


# ── hosted Connect for Google ─────────────────────────────────────────────────────────────
def test_oauth_apps_from_env_builds_google_targeting_google_with_hosted_callback():
    apps = oauth_apps_from_env(
        "https://projects.example.com/",
        env={"GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsec"})
    app = apps["google"]
    assert isinstance(app, ProviderOAuthApp)
    assert app.redirect_uri == "https://projects.example.com/api/apps/connect/callback"
    # the authorize flow targets Google's real endpoints
    assert app.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert app.token_url == "https://oauth2.googleapis.com/token"
    # individual scope URLs (space-joined by the flow): docs + sheets + drive.file union
    assert set(app.scopes) == {
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
    }
    # the refresh-token authorize params are carried (see hosted_oauth note on live-flow threading)
    assert app.authorize_params == {"access_type": "offline", "prompt": "consent"}


def test_without_google_creds_google_is_absent():
    # no GOOGLE_CLIENT_ID/SECRET → google is not offered (deployment falls back to simulated)
    assert "google" not in oauth_apps_from_env("https://x/", env={})
    assert "google" not in oauth_apps_from_env(
        "https://x/", env={"SLACK_CLIENT_ID": "c", "SLACK_CLIENT_SECRET": "s"})
