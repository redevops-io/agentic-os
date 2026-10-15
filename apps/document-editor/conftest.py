"""Fixtures shared by the document-editor tests (pure Python; identical on Windows, macOS and Linux)."""
from __future__ import annotations

import hashlib
import importlib
import json
import io
import re
import types
import zipfile
from pathlib import Path

import pytest

_NAMES = ("errors", "safe_fetch", "backend", "store", "ledger", "locking", "classification", "config", "content",
          "jwtlib", "ooxml", "patch", "onlyoffice", "onlyoffice_edit", "writepath", "service", "operator", "secure_router", "wiring", "manifest",
          "app")


def load(name: str):
    return importlib.import_module(f"document-editor.{name}")


@pytest.fixture(scope="session")
def mods():
    out = {}
    for n in _NAMES:
        if n == "app":
            continue
        try:
            out[n] = load(n)
        except ModuleNotFoundError as e:           # a module introduced by a later layer of the connector
            if f"document-editor.{n}" not in str(e):
                raise
    return types.SimpleNamespace(**out)


def make_docx(paragraphs) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{_esc(p)}</w:t></w:r></w:p>' for p in paragraphs)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    return b.getvalue()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_xlsx(cells: dict) -> bytes:
    rows = "".join(f'<row r="{re.sub("[A-Z]", "", ref)}"><c r="{ref}"><v>{v}</v></c></row>' for ref, v in cells.items())
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        z.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + rows + "</sheetData></worksheet>")
    return b.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def edit_docx_text(data: bytes, ops, track: bool) -> bytes:
    """What a correct editor would produce: text replaced / paragraphs appended (+ revision marks if tracking)."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    xml = zin.read("word/document.xml").decode()
    for op in ops:
        if op["type"] == "replace_text":
            if track:
                xml = xml.replace(op["search"], f'</w:t></w:r><w:del><w:r><w:delText>{op["search"]}</w:delText></w:r></w:del><w:ins><w:r><w:t>{_esc(op["replace"])}</w:t></w:r></w:ins><w:r><w:t xml:space="preserve">')
            else:
                xml = xml.replace(op["search"], _esc(op["replace"]))
        elif op["type"] == "append_paragraph":
            ins = "<w:ins>" if track else ""
            end = "</w:ins>" if track else ""
            xml = xml.replace("</w:body>", f'<w:p>{ins}<w:r><w:t>{_esc(op["text"])}</w:t></w:r>{end}</w:p></w:body>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n in zin.namelist():
            z.writestr(n, xml if n == "word/document.xml" else zin.read(n))
    return b.getvalue()


@pytest.fixture()
def fake_backend_cls(mods):
    B = mods.backend

    class FakeBackend(B.DocumentBackend):
        provider = "onlyoffice"
        PROVEN = frozenset({"open", "convert", "inspect", "export", "automated_edit", "tracked_changes"})

        def __init__(self, decl, store=None):
            super().__init__(decl)
            self.calls = []            # every backend operation actually attempted
            self.mode = None           # None | unavailable | unknown | bad_output | wrong_text | corrupt
            self.fetched = []

        def _do_health(self):
            self.calls.append("health")
            if self.mode == "unavailable":
                raise mods.errors.BackendUnavailable("editor down")
            return {"ok": True, "editor_version": "9.4.0.129-fake"}

        def _do_inspect(self, ref):
            self.calls.append("inspect")
            return {"editor_version": "fake", "opened_by_editor": True}

        def _do_open_session(self, ref, *, user_id, ttl_s, mode):
            self.calls.append("open_session")
            return {"config": {"document": {"url": ref.content_url}}, "expires_in_s": min(ttl_s, 300), "mode": "view"}

        def _do_convert(self, ref, target_format):
            self.calls.append("convert")
            if self.mode == "unavailable":
                raise mods.errors.BackendUnavailable("editor down")
            out = PDF if self.mode != "corrupt" else b"not a pdf"
            if self.mode == "corrupt":
                mods.ooxml.validate_output(out, target_format)       # the real adapter validates before returning
            return B.BackendFile(content=out, filename="x." + target_format,
                                 sha256=hashlib.sha256(out).hexdigest())

        def _do_apply_edit(self, ref, ops, *, track_changes, idem_key):
            self.calls.append("apply_edit")
            if self.mode == "unavailable":
                raise mods.errors.BackendUnavailable("editor down (request not sent)")
            if self.mode == "unknown":
                raise mods.errors.OutcomeUnknown("connection reset after the request was sent")
            data = self.source_for(ref)
            if self.mode == "wrong_text":
                out = edit_docx_text(data, [{"type": "append_paragraph", "text": "SOMETHING ELSE"}], False)
            else:
                out = edit_docx_text(data, ops, track_changes)
            return B.BackendFile(content=out, filename="result.docx", sha256=hashlib.sha256(out).hexdigest())

        source_for = None

    return FakeBackend


@pytest.fixture()
def env(tmp_path, monkeypatch, mods, fake_backend_cls):
    """A fully wired service over a temp document root with a fake external editor."""
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "unit-test-approval-secret-0123456789abcdef")
    root, state, pub = tmp_path / "docs", tmp_path / "state", tmp_path / "published"
    for d in (root / "acme", root / "globex", state, pub):
        d.mkdir(parents=True)
    raw = {
        "document_backend": {"provider": "onlyoffice", "deployment": "external",
                             "capabilities": {"open": True, "convert": True, "inspect": True, "export": True,
                                              "automated_edit": True, "tracked_changes": True}},
        "document_root": str(root), "state_dir": str(state), "content_base_url": "http://connector.test:8216",
        "backend_zone": "PRIVATE_NETWORK",
        "tenants": {
            "acme": {"backends": {"onlyoffice": "PRIVATE_NETWORK"},
                     "publish_destinations": {"board-share": {"path": str(pub), "zone": "PRIVATE_NETWORK"},
                                              "public-site": {"path": str(pub / "site"), "zone": "EXTERNAL"}}},
            "globex": {"backends": {"onlyoffice": "PRIVATE_NETWORK"}},
        }}
    cfg = mods.config.parse_config(raw, env={})
    store = mods.store.LocalFSStore(root)
    content = mods.content.ContentRegistry(cfg.content_base_url, secret="test-content-secret")
    backend = fake_backend_cls(cfg.decl)
    from agentic_os.mission.approval import ReplayGuard
    svc = mods.service.DocumentService(cfg, store, backend, content, replay_guard=ReplayGuard())
    backend.source_for = lambda ref: store.read(ref.tenant, ref.doc_id, ref.version)[0]
    ns = types.SimpleNamespace(root=root, state=state, pub=pub, cfg=cfg, store=store, content=content,
                               backend=backend, svc=svc, raw=raw, mods=mods)

    def put(tenant, doc_id, data, label=None):
        p = root / tenant / doc_id
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if label is not None:
            store.head(tenant, doc_id)
            store.set_classification(tenant, doc_id, label)
        return p
    ns.put = put
    return ns


def mint(operator: str, capability: str, inputs: dict, marker=None, mission_id="m-1", node_id="n-1", ttl_s=300):
    from agentic_os.mission.approval import mint_approval_token
    return mint_approval_token(None, operator=operator, capability=capability, mission_id=mission_id,
                               node_id=node_id, inputs=inputs, marker=marker or {"approved": True, "by": "alice"},
                               ttl_s=ttl_s)


@pytest.fixture(scope="session")
def kit():
    return types.SimpleNamespace(make_docx=make_docx, make_xlsx=make_xlsx, PDF=PDF, mint=mint,
                                 edit_docx_text=edit_docx_text)


class LocalServer:
    """A tiny threaded HTTP server on 127.0.0.1:<random port>. ``handler(req) -> (status, headers, body)``."""

    def __init__(self, handler):
        import http.server
        import socketserver
        import threading
        outer = self
        self.requests = []
        self.handler = handler

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = types.SimpleNamespace(method=self.command, path=self.path, headers=dict(self.headers.items()),
                                            body=body)
                outer.requests.append(req)
                status, headers, payload = outer.handler(req)
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if not any(k.lower() == "content-length" for k in (headers or {})):
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = _do

            def log_message(self, *a):
                pass

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.httpd = S(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    made = []

    def make(handler):
        s = LocalServer(handler)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


ADMIN_TOKEN, ACME_TOKEN, GLOBEX_TOKEN = "admin-token-xxxxxxxxxxxxxxxx", "acme-token-yyyyyyyyyyyyyyyy", "globex-token-zzzzzzzzzzzzzz"


@pytest.fixture()
def client(env, monkeypatch):
    """TestClient over the real app, wired to the fake-editor service, with admin + per-tenant bearer tokens."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={ACME_TOKEN},globex={GLOBEX_TOKEN}")
    appmod = importlib.import_module("document-editor.app")
    appmod.holder._svc = env.svc
    appmod.operator.replay_guard = env.svc.replay_guard.__class__()
    yield TestClient(appmod.app)
    appmod.holder._svc = None


MOCK_SECRET = "mock-jwt-secret"


class MockDS:
    """Document Server stand-in implementing the DOCUMENTED request/response contracts (not the real product)."""
    
    def __init__(self, mods, server, kit):
        self.mods, self.kit = mods, kit
        self.jwt = mods.jwtlib
        self.calls, self.polls = [], 0
        self.convert_steps = [{"endConvert": False, "percent": 40}, {"endConvert": True, "percent": 100}]
        self.file_url = None
        self.result = kit.PDF
        self.health = (200, b"true")
        self.command_reply = {"error": 0, "version": "9.4.0.129"}
        self.builder_urls = None
        self.status_override = None
        self.srv = server(self.handle)
        self.base = f"http://editor.test:{self.srv.port}"

    def handle(self, req):
        path = req.path.split("?")[0]
        if path == "/healthcheck":
            return (*self.health[:1], {}, self.health[1])
        if self.status_override and path in self.status_override:
            return self.status_override[path], {}, b""
        if path.startswith("/cache/files/"):
            return 200, {}, self.result
        # JWT: header Authorization: Bearer <jwt>; the signed payload is authoritative
        auth = req.headers.get("Authorization", "")
        try:
            payload = self.jwt.verify(auth.split(" ", 1)[1], MOCK_SECRET)
        except Exception:
            body = {"error": -8} if path != "/command" else {"error": 6}
            return 200, {}, json.dumps(body).encode()
        self.calls.append((path, payload))
        if path == "/command":
            return 200, {}, json.dumps(self.command_reply).encode()
        if path == "/converter":
            i = min(self.polls, len(self.convert_steps) - 1)
            self.polls += 1
            step = dict(self.convert_steps[i])
            if step.get("endConvert"):
                step["fileUrl"] = self.file_url or f"{self.base}/cache/files/data/conv_x/output.pdf/a.pdf?md5=x&expires=1"
                step["fileType"] = "pdf"
            return 200, {}, json.dumps(step).encode()
        if path == "/docbuilder":
            if "key" in payload:
                return 200, {}, json.dumps({"key": payload["key"], "end": True,
                                            "urls": self.builder_urls or {"result.docx": f"{self.base}/cache/files/data/b/out.docx"}}).encode()
            return 200, {}, json.dumps({"key": "bld_1", "end": False}).encode()
        return 404, {}, b""


@pytest.fixture()
def ds(mods, server, kit):
    return MockDS(mods, server, kit)


@pytest.fixture()
def oo(mods):
    """Builders for the ONLYOFFICE adapter under test (edit-capable subclass; read-only tests use the same object)."""
    def backend(ds, **cfg_kw):
        cfg = mods.onlyoffice.OnlyOfficeConfig(url=ds.base, jwt_secret=MOCK_SECRET, zone="PRIVATE_NETWORK",
                                               poll_interval_s=0.0, operation_timeout_s=5, **cfg_kw)
        decl = mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {
            "open": True, "convert": True, "inspect": True, "export": True, "automated_edit": True,
            "tracked_changes": True}})
        content = mods.content.ContentRegistry("http://connector.test", secret="s")
        cls = getattr(mods, "onlyoffice_edit", None)
        cls = cls.OnlyOfficeEditBackend if cls else mods.onlyoffice.OnlyOfficeBackend
        return cls(decl, cfg, content, resolver=lambda h, p: ["127.0.0.1"], sleep=lambda s: None), content

    def ref(**kw):
        d = dict(doc_id="a.docx", tenant="acme", version=1, sha256="a" * 64, filename="a.docx",
                 content_url="http://connector.test/content/tok")
        d.update(kw)
        return mods.backend.DocRef(**d)
    return types.SimpleNamespace(backend=backend, ref=ref)


@pytest.fixture()
def ds(mods, server, kit):
    return MockDS(mods, server, kit)


def backend(mods, ds, **cfg_kw):
    cfg = mods.onlyoffice.OnlyOfficeConfig(url=ds.base, jwt_secret=SECRET, zone="PRIVATE_NETWORK",
                                           poll_interval_s=0.0, operation_timeout_s=5, **cfg_kw)
    decl = mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {
        "open": True, "convert": True, "inspect": True, "export": True, "automated_edit": True, "tracked_changes": True}})
    content = mods.content.ContentRegistry("http://connector.test", secret="s")
    return mods.onlyoffice_edit.OnlyOfficeEditBackend(decl, cfg, content, resolver=lambda h, p: ["127.0.0.1"],
                                                      sleep=lambda s: None), content


def ref(mods, **kw):
    d = dict(doc_id="a.docx", tenant="acme", version=1, sha256="a" * 64, filename="a.docx",
             content_url="http://connector.test/content/tok")
    d.update(kw)
    return mods.backend.DocRef(**d)




@pytest.fixture()
def ds(mods, server, kit):
    return MockDS(mods, server, kit)
