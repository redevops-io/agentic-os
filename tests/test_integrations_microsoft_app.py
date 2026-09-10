"""W2 — Microsoft 365 (Graph) documents App adapter + hosted Connect, driven by a FAKE Graph
client (no network, no token). Proves: the advertised capability surface (name/write); a governed
sheet.write reconciles found=True via observe; a write with no envelope is refused; document.create
uploads a file and returns an item id; the credential token is resolved at use and never stored/
leaked; and the hosted Connect builds a `microsoft` OAuth app targeting Graph with the hosted
callback redirect and the offline_access refresh-token scope."""
from __future__ import annotations

import json

from agentic_os.integrations.execution import GovernedEnvelope
from agentic_os.integrations.hosted_oauth import (
    ProviderOAuthApp,
    oauth_apps_from_env,
)
from agentic_os.integrations.microsoft_app import (
    MicrosoftGraphHttpClient,
    MicrosoftWorkbookDocsAdapter,
    microsoft_docs_adapter,
)

TOKEN = "eyJ0.fake-graph-token-DO-NOT-STORE"


class FakeGraph:
    """A fake MicrosoftGraphClient with an in-memory store. Records calls; no network."""

    def __init__(self):
        self.calls = []
        self.ranges = {}      # (item, worksheet, address) -> values
        self.items = {}       # item_id -> name
        self._item_seq = 0

    def get_range(self, item_id, worksheet, address):
        self.calls.append(("get_range", item_id, worksheet, address))
        vals = self.ranges.get((item_id, worksheet, address))
        if vals is None and item_id not in {i for (i, _w, _a) in self.ranges}:
            from agentic_os.integrations.microsoft_app import MicrosoftAppError
            raise MicrosoftAppError("The resource could not be found.")
        return {"address": f"{worksheet}!{address}", "values": vals or []}

    def update_range(self, item_id, worksheet, address, values):
        self.calls.append(("update_range", item_id, worksheet, address))
        self.ranges[(item_id, worksheet, address)] = [list(r) for r in values]
        return {"address": f"{worksheet}!{address}", "values": [list(r) for r in values]}

    def upload_file(self, name, content):
        self.calls.append(("upload_file", name, content))
        self._item_seq += 1
        item_id = f"item-{self._item_seq}"
        self.items[item_id] = name
        return {"id": item_id, "name": name}

    def get_item(self, item_id):
        self.calls.append(("get_item", item_id))
        if item_id not in self.items:
            from agentic_os.integrations.microsoft_app import MicrosoftAppError
            raise MicrosoftAppError("not found")
        return {"id": item_id, "name": self.items[item_id]}


class Resolver:
    def __init__(self, token=TOKEN):
        self._token = token

    def resolve(self, ref):
        return {"access_token": self._token} if self._token else {}


def _adapter(token=TOKEN):
    fake = FakeGraph()
    ad = microsoft_docs_adapter(Resolver(token), credential_ref="microsoft:token",
                                client_factory=lambda _tok: fake)
    return ad, fake


def _envelope(cap):
    return GovernedEnvelope(intent_hash="h" * 64, capability=cap, provider="microsoft", tier=2)


# ── capability surface ──────────────────────────────────────────────────────────────────
def test_capabilities_shape_name_and_write():
    ad, _ = _adapter()
    assert ad.provider == "microsoft"
    caps = {c.name: c.write for c in ad.capabilities()}
    assert caps == {"sheet.read": False, "sheet.write": True, "document.create": True}


# ── sheet.write under a governed envelope → ok + id → observe reconciles ─────────────────
def test_sheet_write_under_envelope_then_reconciles():
    ad, fake = _adapter()
    req = {"item_id": "wb-1", "worksheet": "Sheet1", "range": "A1:B1", "values": [["Q3", 42]]}
    res = ad.execute("sheet.write", req, _envelope("sheet.write"))
    assert res.ok and res.error == ""
    assert res.provider_object_id == "sheet:wb-1:Sheet1:A1:B1"
    # no action is complete until observed — re-reading the range finds it
    obs = ad.observe(res.provider_object_id)
    assert obs.found is True
    assert fake.ranges[("wb-1", "Sheet1", "A1:B1")] == [["Q3", 42]]


def test_sheet_write_refuses_without_envelope():
    ad, fake = _adapter()
    res = ad.execute("sheet.write",
                     {"item_id": "wb-1", "worksheet": "Sheet1", "range": "A1", "values": [["x"]]}, None)
    assert not res.ok and "envelope required" in res.error
    assert fake.calls == []   # refused before any client call


def test_sheet_read_needs_no_envelope():
    ad, fake = _adapter()
    fake.ranges[("wb-1", "Sheet1", "A1:B1")] = [["Q3", 42]]
    res = ad.execute("sheet.read", {"item_id": "wb-1", "worksheet": "Sheet1", "range": "A1:B1"}, None)
    assert res.ok and res.data["values"] == [["Q3", 42]]
    assert res.provider_object_id == "sheet:wb-1:Sheet1:A1:B1"


# ── document.create → OneDrive item id ────────────────────────────────────────────────────
def test_document_create_returns_item_id_and_reconciles():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"name": "Q3 Board Memo.txt", "content": "hello"},
                     _envelope("document.create"))
    assert res.ok and res.data["id"] == "item-1"
    assert res.provider_object_id == "item:item-1"
    obs = ad.observe(res.provider_object_id)
    assert obs.found is True


def test_document_create_refuses_without_envelope():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"name": "x.txt"}, None)
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
    assert ad.observe("item:nope").found is False
    assert ad.observe("sheet:unknown:Sheet1:A1").found is False


# ── the token is resolved at use and never stored on the adapter or a result ─────────────
def test_token_is_never_stored_or_leaked():
    ad, _ = _adapter()
    res = ad.execute("document.create", {"name": "memo.txt", "content": "x"},
                     _envelope("document.create"))
    blob = json.dumps({"adapter_ref": ad.credential_ref, "result_id": res.provider_object_id,
                       "result_data": res.data})
    assert TOKEN not in blob                       # token never lands on the adapter/result
    assert ad.credential_ref == "microsoft:token"  # addressed by ref only


def test_missing_token_is_a_clean_failure():
    ad, _ = _adapter(token="")
    res = ad.execute("sheet.read", {"item_id": "wb", "worksheet": "Sheet1", "range": "A1"}, None)
    assert not res.ok and "access token" in res.error
    assert not ad.connect({}, "microsoft:token").connected


def test_connect_and_health_when_token_resolves():
    ad, _ = _adapter()
    assert ad.connect({}, "microsoft:token").connected is True
    assert ad.health().healthy is True


# ── the thin HTTP client sends a Bearer token and hits the right Graph endpoints ─────────
def test_http_client_sends_bearer_and_targets_graph():
    seen = []

    def http_request(method, url, headers, body=None):
        seen.append((method, url, headers.get("Authorization", ""), body))
        if "/range(address=" in url and method == "GET":
            return 200, {"address": "Sheet1!A1", "values": [["hi"]]}
        if "/range(address=" in url and method == "PATCH":
            return 200, {"address": "Sheet1!A1"}
        if url.endswith(":/content") and method == "PUT":
            return 200, {"id": "i1", "name": "n.txt"}
        return 200, {}

    client = MicrosoftGraphHttpClient(access_token=TOKEN, http_request=http_request)
    client.get_range("wb", "Sheet1", "A1")
    client.update_range("wb", "Sheet1", "A1", [["hi"]])
    client.upload_file("n.txt", b"hi")
    assert all(auth == f"Bearer {TOKEN}" for (_m, _u, auth, _b) in seen)
    assert all("graph.microsoft.com/v1.0" in u for (_m, u, _a, _b) in seen)
    assert any("/workbook/worksheets/" in u for (_m, u, _a, _b) in seen)
    assert any(u.endswith(":/content") for (_m, u, _a, _b) in seen)


def test_http_client_write_uses_patch():
    seen = {}

    def http_request(method, url, headers, body=None):
        seen["method"], seen["url"] = method, url
        return 200, {"address": "Sheet1!A1"}

    client = MicrosoftGraphHttpClient(access_token=TOKEN, http_request=http_request)
    client.update_range("wb", "Sheet1", "A1", [["x"]])
    assert seen["method"] == "PATCH" and "/range(address=" in seen["url"]


# ── hosted Connect for Microsoft ──────────────────────────────────────────────────────────
def test_oauth_apps_from_env_builds_microsoft_targeting_graph_with_hosted_callback():
    apps = oauth_apps_from_env(
        "https://projects.example.com/",
        env={"MICROSOFT_CLIENT_ID": "mid", "MICROSOFT_CLIENT_SECRET": "msec"})
    app = apps["microsoft"]
    assert isinstance(app, ProviderOAuthApp)
    assert app.redirect_uri == "https://projects.example.com/api/apps/connect/callback"
    # the authorize flow targets Microsoft's real v2.0 endpoints
    assert app.authorize_url == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert app.token_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    # individual Graph delegated scopes (space-joined by the flow), incl. the refresh-token scope
    assert set(app.scopes) == {"Files.ReadWrite", "offline_access", "User.Read"}
    assert "offline_access" in app.scopes   # MS refresh token comes from the *scope*, not a param
    # MS does not need access_type=offline; authorize_params only nudge account choice
    assert "access_type" not in app.authorize_params


def test_without_microsoft_creds_microsoft_is_absent():
    assert "microsoft" not in oauth_apps_from_env("https://x/", env={})
    assert "microsoft" not in oauth_apps_from_env(
        "https://x/", env={"SLACK_CLIENT_ID": "c", "SLACK_CLIENT_SECRET": "s"})
