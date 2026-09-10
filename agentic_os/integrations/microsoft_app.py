"""Microsoft 365 (Graph) **documents** App adapter (W2) — the ACTION side of the productivity plane.

The Microsoft counterpart to the W1 :class:`~agentic_os.integrations.google_app.GoogleWorkspaceDocsAdapter`.
Where a Source connector would *index* an existing OneDrive/SharePoint corpus as evidence (the SOURCE
side — **not yet built**, see the note in ``productivity.py``), this adapter *acts* on documents under a
:class:`~agentic_os.integrations.execution.GovernedEnvelope`: read an Excel range, write an Excel range,
create a file in OneDrive.

It satisfies the :class:`~agentic_os.integrations.execution.AdapterPort` structurally (``provider`` /
``capabilities`` / ``connect`` / ``execute`` / ``observe`` / ``health``) so the W4 runner drives it with
no import coupling, and it is **self-contained, stdlib-only** — it does NOT import ``redevops-connectors``
(Outlook/Teams live there; this is the docs/workbook surface those adapters do not cover). It mirrors the
W1 ``google_app`` idioms exactly:

* a :class:`MicrosoftGraphClient` **seam** (a Protocol) so tests drive a fake with canned responses,
* a thin ``urllib`` :class:`MicrosoftGraphHttpClient` for the live path (Graph v1.0 workbook + drive),
* the OAuth access token resolved from a ``CredentialRef`` **at the moment of use** and never stored on
  the adapter, a result, or an observation.

Capabilities implemented (LIVE): ``sheet.read`` (read — Excel workbook range), ``sheet.write`` (write —
PATCH that range), ``document.create`` (write — upload/create a file in OneDrive). ``document.edit`` and
the ``slides.*`` family stay PLANNED at W2 (the Word/PowerPoint Graph surfaces are a later wave). Every
**write** refuses when ``envelope is None`` — the adapter carries the authority requirement; the membrane
validates it upstream.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

# The provider-independent logical capability ids this adapter fulfils (W0 contracts).
from .productivity import DocCapability

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: resource_ref prefixes — a typed handle so ``observe`` knows how to re-read the object.
_SHEET_REF = "sheet"
_ITEM_REF = "item"


class MicrosoftAppError(Exception):
    """A Graph API request was rejected (surfaced as ok=False / found=False, never raised through)."""


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
    verification re-observes (``sheet:<item>:<worksheet>:<address>`` or ``item:<id>``). Never
    carries the token."""

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
class MicrosoftGraphClient(Protocol):
    """The deterministic seam over Graph v1.0 (Excel workbook range + drive item). Returns
    parsed response dicts; raises :class:`MicrosoftAppError` on an API error."""

    def get_range(self, item_id: str, worksheet: str, address: str) -> dict: ...
    def update_range(self, item_id: str, worksheet: str, address: str,
                     values: Sequence[Sequence[Any]]) -> dict: ...
    def upload_file(self, name: str, content: bytes) -> dict: ...
    def get_item(self, item_id: str) -> dict: ...


def _urllib_request(method: str, url: str, headers: Dict[str, str],
                    body: Optional[Any] = None) -> Tuple[int, dict]:  # pragma: no cover - live HTTP
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


def _graph_error(data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", "")) or str(err.get("code", "")) or "error"
    if isinstance(err, str):
        return err
    return ""


@dataclass
class MicrosoftGraphHttpClient:
    """Live Graph v1.0 client (stdlib urllib) over the Excel workbook + drive surfaces. The
    access token is used at call time and never stored on a result. ``http_request`` is
    injectable so the client itself is testable offline."""

    access_token: str
    http_request: Callable[[str, str, Dict[str, str], Optional[Any]], Tuple[int, dict]] = _urllib_request

    def _headers(self, *, content_type: str = "application/json") -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json", "Content-Type": content_type}

    def _call(self, method: str, url: str, body: Optional[Any] = None,
              *, content_type: str = "application/json") -> dict:
        status, data = self.http_request(method, url, self._headers(content_type=content_type), body)
        if status >= 400 or _graph_error(data):
            raise MicrosoftAppError(f"{method} {url.split('?', 1)[0]} failed: "
                                    f"{status} {_graph_error(data)}")
        return data

    def _range_url(self, item_id: str, worksheet: str, address: str) -> str:
        import urllib.parse
        item, ws = urllib.parse.quote(item_id), urllib.parse.quote(worksheet)
        addr = urllib.parse.quote(address, safe="")
        return (f"{_GRAPH_BASE}/me/drive/items/{item}"
                f"/workbook/worksheets/{ws}/range(address='{addr}')")

    def get_range(self, item_id: str, worksheet: str, address: str) -> dict:
        return self._call("GET", self._range_url(item_id, worksheet, address))

    def update_range(self, item_id: str, worksheet: str, address: str,
                     values: Sequence[Sequence[Any]]) -> dict:
        body = _json.dumps({"values": [list(row) for row in values]})
        return self._call("PATCH", self._range_url(item_id, worksheet, address), body)

    def upload_file(self, name: str, content: bytes) -> dict:
        import urllib.parse
        # PUT /me/drive/root:/{name}:/content — simple upload for a small file.
        path = urllib.parse.quote(name)
        url = f"{_GRAPH_BASE}/me/drive/root:/{path}:/content"
        return self._call("PUT", url, content, content_type="text/plain")

    def get_item(self, item_id: str) -> dict:
        import urllib.parse
        return self._call("GET", f"{_GRAPH_BASE}/me/drive/items/{urllib.parse.quote(item_id)}")


def _default_client_factory(token: str) -> MicrosoftGraphClient:
    return MicrosoftGraphHttpClient(token)


@dataclass
class MicrosoftWorkbookDocsAdapter:
    """Microsoft 365 (Graph) documents App adapter. ``resolver`` resolves the credential ref to
    ``{"access_token": …}`` (the token a one-click Connect fetched); ``client_factory`` builds a
    :class:`MicrosoftGraphClient` from that token (defaults to the live HTTP client). The token
    is resolved on every call and never stored on the adapter."""

    provider: str = "microsoft"
    resolver: Any = None
    credential_ref: str = ""
    client_factory: Callable[[str], MicrosoftGraphClient] = _default_client_factory

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
    def _client(self, credential_ref: str = "") -> MicrosoftGraphClient:
        ref = credential_ref or self.credential_ref
        material = dict(self.resolver.resolve(ref)) if (ref and self.resolver is not None) else {}
        token = material.get("access_token", "")
        if not token:
            raise MicrosoftAppError("no access token — connect Microsoft first (OAuth)")
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
            return DocResult(False, capability, error=f"microsoft does not implement {capability!r}")
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

    @staticmethod
    def _sheet_target(req: Dict[str, Any]) -> Tuple[str, str, str]:
        item = str(req.get("item_id", ""))
        worksheet = str(req.get("worksheet") or req.get("sheet", "Sheet1"))
        address = str(req.get("range") or req.get("address", "A1"))
        return item, worksheet, address

    def _sheet_read(self, client: MicrosoftGraphClient, capability: str, req: Dict[str, Any]) -> DocResult:
        item, ws, addr = self._sheet_target(req)
        data = client.get_range(item, ws, addr)
        return DocResult(True, capability,
                         provider_object_id=f"{_SHEET_REF}:{item}:{ws}:{addr}",
                         data={"values": data.get("values", []),
                               "address": data.get("address", addr)})

    def _sheet_write(self, client: MicrosoftGraphClient, capability: str, req: Dict[str, Any]) -> DocResult:
        item, ws, addr = self._sheet_target(req)
        values: List[List[Any]] = [list(r) for r in (req.get("values") or [])]
        data = client.update_range(item, ws, addr, values)
        # Graph echoes the full range address (incl. the sheet name) it wrote.
        updated = str(data.get("address", "") or addr)
        return DocResult(True, capability,
                         provider_object_id=f"{_SHEET_REF}:{item}:{ws}:{addr}",
                         data={"address": updated,
                               "rows": len(values), "cols": len(values[0]) if values else 0})

    def _document_create(self, client: MicrosoftGraphClient, capability: str, req: Dict[str, Any]) -> DocResult:
        name = str(req.get("name", "Untitled.txt"))
        content = req.get("content", "")
        blob = content.encode("utf-8") if isinstance(content, str) else bytes(content or b"")
        data = client.upload_file(name, blob)
        item_id = str(data.get("id", ""))
        if not item_id:
            return DocResult(False, capability, error="drive upload returned no item id")
        return DocResult(True, capability, provider_object_id=f"{_ITEM_REF}:{item_id}",
                         data={"id": item_id, "name": data.get("name", name)})

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
                item, _, tail = rest.partition(":")
                ws, _, addr = tail.partition(":")
                data = client.get_range(item, ws or "Sheet1", addr or "A1")
                return DocObservation(resource_ref, found="address" in data,
                                      data={"address": data.get("address", "")})
            if kind == _ITEM_REF:
                data = client.get_item(rest)
                found = str(data.get("id", "")) == rest
                return DocObservation(resource_ref, found=found,
                                      data={"id": data.get("id", "")} if found else {})
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


def microsoft_docs_adapter(resolver: Any, *, credential_ref: str = "microsoft:token",
                           client_factory: Optional[Callable[[str], MicrosoftGraphClient]] = None
                           ) -> MicrosoftWorkbookDocsAdapter:
    """Build a :class:`MicrosoftWorkbookDocsAdapter` bound to ``resolver`` and the conventional
    ``microsoft:token`` credential ref (parity with ``live.PROVIDER_TOKEN_ENV``)."""
    return MicrosoftWorkbookDocsAdapter(
        resolver=resolver, credential_ref=credential_ref,
        client_factory=client_factory or _default_client_factory)
