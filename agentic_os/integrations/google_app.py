"""Google Workspace **documents** App adapter (W1) — the ACTION side of the productivity plane.

The APP-role counterpart to the read-only :class:`~agentic_os.sources_drive.GoogleDriveSourceConnector`
(the SOURCE side). Where the Source connector *indexes* an existing corpus as evidence, this adapter
*acts* on documents under a :class:`~agentic_os.integrations.execution.GovernedEnvelope`: read a sheet,
write a sheet, create a doc.

It satisfies the :class:`~agentic_os.integrations.execution.AdapterPort` structurally (``provider`` /
``capabilities`` / ``connect`` / ``execute`` / ``observe`` / ``health``) so the W4 runner drives it with
no import coupling, and it is **self-contained, stdlib-only** — it does NOT import ``redevops-connectors``
(Gmail/Calendar live there; this is the docs/sheets surface those adapters do not cover). It mirrors
``sources_drive`` idioms exactly:

* a :class:`GoogleDocsSheetsClient` **seam** (a Protocol) so tests drive a fake with canned responses,
* a thin ``urllib`` :class:`GoogleDocsSheetsHttpClient` for the live path (Sheets v4 ``values`` + Docs v1),
* the OAuth access token resolved from a ``CredentialRef`` **at the moment of use** and never stored on
  the adapter, a result, or an observation.

Capabilities implemented (LIVE): ``sheet.read`` (read), ``sheet.write`` (write), ``document.create``
(write). ``document.edit`` and the ``slides.*`` family stay PLANNED at W1 (the Docs ``batchUpdate`` and
Slides surfaces are a later wave). Every **write** refuses when ``envelope is None`` — the adapter carries
the authority requirement; the membrane validates it upstream.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

# The provider-independent logical capability ids this adapter fulfils (W0 contracts).
from .productivity import DocCapability

_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_DOCS_BASE = "https://docs.googleapis.com/v1/documents"

#: resource_ref prefixes — a typed handle so ``observe`` knows how to re-read the object.
_SHEET_REF = "sheet"
_DOC_REF = "doc"


class GoogleAppError(Exception):
    """A Docs/Sheets API request was rejected (surfaced as ok=False / found=False, never raised through)."""


# ── The result / observation / connection / health shapes (self-contained, attribute-read) ──
@dataclass(frozen=True)
class AppCapability:
    """One capability the adapter advertises. Carries ``.name`` and ``.write`` (what the W4
    runner's ``_is_write`` reads) plus a coarse risk ``tier``."""

    name: str
    write: bool = False
    tier: int = 0


@dataclass(frozen=True)
class DocResult:
    """The normalized outcome of one ``execute``. ``provider_object_id`` is the typed handle
    verification re-observes (``sheet:<id>:<range>`` or ``doc:<id>``). Never carries the token."""

    ok: bool
    capability: str
    provider_object_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class DocObservation:
    resource_ref: str
    found: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocConnection:
    provider: str
    connected: bool
    detail: str = ""


@dataclass(frozen=True)
class DocHealth:
    healthy: bool
    detail: str = ""


# ── The client seam (a Protocol) — tests drive a fake; production uses the HTTP client ──
class GoogleDocsSheetsClient(Protocol):
    """The deterministic seam over Sheets v4 (values) + Docs v1 (documents). Returns parsed
    response dicts; raises :class:`GoogleAppError` on an API error."""

    def get_values(self, spreadsheet_id: str, a1_range: str) -> dict: ...
    def write_values(self, spreadsheet_id: str, a1_range: str,
                     values: Sequence[Sequence[Any]], *, mode: str = "update") -> dict: ...
    def create_document(self, title: str) -> dict: ...
    def get_document(self, document_id: str) -> dict: ...


def _urllib_request(method: str, url: str, headers: Dict[str, str],
                    body: Optional[str] = None) -> Tuple[int, dict]:  # pragma: no cover - live HTTP
    import urllib.error
    import urllib.request
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            raw = fh.read().decode("utf-8")
            return fh.status, (_json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _google_error(data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", "")) or "error"
    if isinstance(err, str):
        return err
    return ""


@dataclass
class GoogleDocsSheetsHttpClient:
    """Live Sheets v4 (``values``) + Docs v1 (``documents``) client (stdlib urllib). The access
    token is used at call time and never stored on a result. ``http_request`` is injectable so the
    client itself is testable offline."""

    access_token: str
    http_request: Callable[[str, str, Dict[str, str], Optional[str]], Tuple[int, dict]] = _urllib_request

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json", "Content-Type": "application/json"}

    def _call(self, method: str, url: str, body: Optional[str] = None) -> dict:
        status, data = self.http_request(method, url, self._headers(), body)
        if status >= 400 or _google_error(data):
            raise GoogleAppError(f"{method} {url.split('?', 1)[0]} failed: "
                                 f"{status} {_google_error(data)}")
        return data

    def get_values(self, spreadsheet_id: str, a1_range: str) -> dict:
        import urllib.parse
        url = f"{_SHEETS_BASE}/{urllib.parse.quote(spreadsheet_id)}/values/{urllib.parse.quote(a1_range)}"
        return self._call("GET", url)

    def write_values(self, spreadsheet_id: str, a1_range: str,
                     values: Sequence[Sequence[Any]], *, mode: str = "update") -> dict:
        import urllib.parse
        sid, rng = urllib.parse.quote(spreadsheet_id), urllib.parse.quote(a1_range)
        body = _json.dumps({"values": [list(row) for row in values], "majorDimension": "ROWS"})
        if mode == "append":
            url = (f"{_SHEETS_BASE}/{sid}/values/{rng}:append"
                   "?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
            return self._call("POST", url, body)
        url = f"{_SHEETS_BASE}/{sid}/values/{rng}?valueInputOption=RAW"
        return self._call("PUT", url, body)

    def create_document(self, title: str) -> dict:
        return self._call("POST", _DOCS_BASE, _json.dumps({"title": title}))

    def get_document(self, document_id: str) -> dict:
        import urllib.parse
        return self._call("GET", f"{_DOCS_BASE}/{urllib.parse.quote(document_id)}")


def _default_client_factory(token: str) -> GoogleDocsSheetsClient:
    return GoogleDocsSheetsHttpClient(token)


@dataclass
class GoogleWorkspaceDocsAdapter:
    """Google Workspace documents App adapter. ``resolver`` resolves the credential ref to
    ``{"access_token": …}`` (the token a one-click Connect fetched); ``client_factory`` builds a
    :class:`GoogleDocsSheetsClient` from that token (defaults to the live HTTP client). The token
    is resolved on every call and never stored on the adapter."""

    provider: str = "google"
    resolver: Any = None
    credential_ref: str = ""
    client_factory: Callable[[str], GoogleDocsSheetsClient] = _default_client_factory

    # ── the advertised surface (W0 logical ids; .name + .write are what the runner reads) ──
    def capabilities(self) -> Tuple[AppCapability, ...]:
        return (
            AppCapability(DocCapability.SHEET_READ.value, write=False, tier=0),
            AppCapability(DocCapability.SHEET_WRITE.value, write=True, tier=2),
            AppCapability(DocCapability.DOCUMENT_CREATE.value, write=True, tier=2),
        )

    def _capability(self, name: str) -> Optional[AppCapability]:
        for c in self.capabilities():
            if c.name == name:
                return c
        return None

    # ── token resolution: at the moment of use, never stored ────────────────────────────
    def _client(self, credential_ref: str = "") -> GoogleDocsSheetsClient:
        ref = credential_ref or self.credential_ref
        material = dict(self.resolver.resolve(ref)) if (ref and self.resolver is not None) else {}
        token = material.get("access_token", "")
        if not token:
            raise GoogleAppError("no access token — connect Google first (OAuth)")
        return self.client_factory(token)

    # ── connect (one-click OAuth landing): a token resolves ⇒ connected ──────────────────
    def connect(self, config: Any, credential_ref: str) -> DocConnection:
        if credential_ref:
            self.credential_ref = credential_ref
        try:
            self._client()
        except Exception as e:  # noqa: BLE001 — report, never raise through
            return DocConnection(self.provider, False, str(e))
        return DocConnection(self.provider, True, "token resolved")

    # ── execute (writes refuse without an envelope) ──────────────────────────────────────
    def execute(self, capability: str, request: Any, envelope: Optional[object]) -> DocResult:
        cap = self._capability(capability)
        if cap is None:
            return DocResult(False, capability, error=f"google does not implement {capability!r}")
        if cap.write and envelope is None:
            return DocResult(False, capability,
                             error="execution envelope required for a consequential (write) capability")
        req: Dict[str, Any] = dict(request or {})
        try:
            client = self._client()
        except Exception as e:  # noqa: BLE001
            return DocResult(False, capability, error=str(e))
        try:
            if capability == DocCapability.SHEET_READ.value:
                return self._sheet_read(client, capability, req)
            if capability == DocCapability.SHEET_WRITE.value:
                return self._sheet_write(client, capability, req)
            if capability == DocCapability.DOCUMENT_CREATE.value:
                return self._document_create(client, capability, req)
        except Exception as e:  # noqa: BLE001 — API errors are results, not exceptions
            return DocResult(False, capability, error=f"{type(e).__name__}: {e}")
        return DocResult(False, capability, error="unhandled capability")

    def _sheet_read(self, client: GoogleDocsSheetsClient, capability: str, req: Dict[str, Any]) -> DocResult:
        sid, rng = str(req.get("spreadsheet_id", "")), str(req.get("range", "A1"))
        data = client.get_values(sid, rng)
        return DocResult(True, capability, provider_object_id=f"{_SHEET_REF}:{sid}:{rng}",
                         data={"values": data.get("values", []), "range": data.get("range", rng)})

    def _sheet_write(self, client: GoogleDocsSheetsClient, capability: str, req: Dict[str, Any]) -> DocResult:
        sid, rng = str(req.get("spreadsheet_id", "")), str(req.get("range", "A1"))
        values: List[List[Any]] = [list(r) for r in (req.get("values") or [])]
        mode = str(req.get("mode", "update"))
        data = client.write_values(sid, rng, values, mode=mode)
        # PUT echoes updatedRange at the top; :append nests it under "updates".
        updated = str(data.get("updatedRange")
                      or (data.get("updates", {}) or {}).get("updatedRange") or rng)
        return DocResult(True, capability, provider_object_id=f"{_SHEET_REF}:{sid}:{updated}",
                         data={"updated_range": updated, "updated_cells":
                               data.get("updatedCells") or (data.get("updates", {}) or {}).get("updatedCells")})

    def _document_create(self, client: GoogleDocsSheetsClient, capability: str, req: Dict[str, Any]) -> DocResult:
        data = client.create_document(str(req.get("title", "Untitled document")))
        doc_id = str(data.get("documentId", ""))
        if not doc_id:
            return DocResult(False, capability, error="Docs create returned no documentId")
        return DocResult(True, capability, provider_object_id=f"{_DOC_REF}:{doc_id}",
                         data={"documentId": doc_id, "title": data.get("title", "")})

    # ── observe: reconcile by re-reading the object (no action complete until observed) ──
    def observe(self, resource_ref: str) -> DocObservation:
        if not resource_ref:
            return DocObservation(resource_ref, found=False)
        try:
            client = self._client()
        except Exception:  # noqa: BLE001
            return DocObservation(resource_ref, found=False)
        kind, _, rest = resource_ref.partition(":")
        try:
            if kind == _SHEET_REF:
                sid, _, rng = rest.partition(":")
                data = client.get_values(sid, rng or "A1")
                return DocObservation(resource_ref, found="range" in data,
                                      data={"range": data.get("range", "")})
            if kind == _DOC_REF:
                data = client.get_document(rest)
                found = str(data.get("documentId", "")) == rest
                return DocObservation(resource_ref, found=found,
                                      data={"documentId": data.get("documentId", "")} if found else {})
        except Exception:  # noqa: BLE001 — an API error is "not found", not a raise
            return DocObservation(resource_ref, found=False)
        return DocObservation(resource_ref, found=False)

    # ── health: the token resolves ⇒ ready (no live probe here) ──────────────────────────
    def health(self) -> DocHealth:
        try:
            self._client()
        except Exception as e:  # noqa: BLE001
            return DocHealth(False, str(e))
        return DocHealth(True, "ok")


def google_docs_adapter(resolver: Any, *, credential_ref: str = "google:token",
                        client_factory: Optional[Callable[[str], GoogleDocsSheetsClient]] = None) -> GoogleWorkspaceDocsAdapter:
    """Build a :class:`GoogleWorkspaceDocsAdapter` bound to ``resolver`` and the conventional
    ``google:token`` credential ref (parity with ``live.PROVIDER_TOKEN_ENV``)."""
    return GoogleWorkspaceDocsAdapter(
        resolver=resolver, credential_ref=credential_ref,
        client_factory=client_factory or _default_client_factory)
