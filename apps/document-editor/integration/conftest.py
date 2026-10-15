"""Fixtures for the REAL-Document-Server integration suite (skipped unless the throwaway container env is set)."""
from __future__ import annotations

import importlib
import os
import socketserver
import http.server
import threading
import types

import pytest

URL = os.environ.get("DOCEDITOR_IT_ONLYOFFICE_URL", "")
SECRET = os.environ.get("DOCEDITOR_IT_ONLYOFFICE_JWT_SECRET", "")
HOST_ADDR = os.environ.get("DOCEDITOR_IT_HOST_ADDR", "172.17.0.1")      # how the container reaches THIS host


def pytest_collection_modifyitems(config, items):
    if URL and SECRET:
        return
    skip = pytest.mark.skip(reason="integration: set DOCEDITOR_IT_ONLYOFFICE_URL / _JWT_SECRET (see integration/README.md)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _m(name):
    return importlib.import_module(f"document-editor.{name}")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A real service over a temp document root wired to the REAL Document Server, plus a content server on all
    interfaces that the container fetches signed bytes from (the DS's JWT Authorization header is verified)."""
    if not (URL and SECRET):
        pytest.skip("no real Document Server configured")
    tmp = tmp_path_factory.mktemp("docedit-it")
    root, state = tmp / "docs", tmp / "state"
    (root / "acme").mkdir(parents=True)
    state.mkdir()
    holder = {}
    ds_auth = []
    jwtlib = _m("jwtlib")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.path.startswith("/content/"):
                self.send_response(404)
                self.end_headers()
                return
            auth = self.headers.get("Authorization", "")
            try:
                jwtlib.verify(auth.split(" ", 1)[1], SECRET)
                ds_auth.append(True)
            except Exception:  # noqa: BLE001
                ds_auth.append(False)
            try:
                data, media = holder["svc"].serve_content(self.path[len("/content/"):].split("?")[0])
            except Exception:  # noqa: BLE001
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", media)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
    httpd = S(("0.0.0.0", 0), H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://{HOST_ADDR}:{port}"
    raw = {"document_backend": {"provider": "onlyoffice", "deployment": "external",
                                "capabilities": {c: True for c in ("open", "convert", "inspect", "export",
                                                                    "automated_edit", "tracked_changes")}},
           "document_root": str(root), "state_dir": str(state), "content_base_url": base, "backend_zone": "LOCAL",
           "tenants": {"acme": {"backends": {"onlyoffice": "LOCAL"}}}}
    cfg = _m("config").parse_config(raw, env={})
    oo_cfg = _m("onlyoffice").OnlyOfficeConfig.from_env(
        zone="LOCAL", env={"DOCEDITOR_ONLYOFFICE_URL": URL, "DOCEDITOR_ONLYOFFICE_JWT_SECRET": SECRET},
        poll_interval_s=0.5, operation_timeout_s=180)
    content = _m("content").ContentRegistry(base, secret="integration-content-secret")
    try:
        backend_cls = _m("onlyoffice_edit").OnlyOfficeEditBackend
    except ModuleNotFoundError:                     # layered build: read-only adapter only
        backend_cls = _m("onlyoffice").OnlyOfficeBackend
    backend = backend_cls(cfg.decl, oo_cfg, content)
    from agentic_os.mission.approval import ReplayGuard
    svc = _m("service").DocumentService(cfg, _m("store").LocalFSStore(root), backend, content, replay_guard=ReplayGuard())
    holder["svc"] = svc
    ns = types.SimpleNamespace(svc=svc, backend=backend, root=root, base=base, port=port, ds_auth=ds_auth,
                               store=svc.store, content=content, cfg=oo_cfg, secret=SECRET, url=URL)
    yield ns
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def seeds(live):
    """A real docx and xlsx produced BY the Document Server from plain text / csv (so they are known-good files)."""
    mods = types.SimpleNamespace(**{n: _m(n) for n in ("ooxml", "errors")})
    (live.root / "acme" / "seed.txt").write_text("Hello original world.\nThe price is 100 EUR.\nEnd of document.\n")
    live.store.head("acme", "seed.txt")
    live.store.set_classification("acme", "seed.txt", "PUBLIC")
    r = live.svc.convert("acme", "seed.txt", "docx")
    docx_bytes = live.store.read("acme", r["artifact_doc_id"])[0]
    (live.root / "acme" / "contract.docx").write_bytes(docx_bytes)
    live.store.head("acme", "contract.docx")
    live.store.set_classification("acme", "contract.docx", "PUBLIC")
    (live.root / "acme" / "sheet.csv").write_text("name,qty\nwidget,42\n")
    live.store.head("acme", "sheet.csv")
    live.store.set_classification("acme", "sheet.csv", "PUBLIC")
    rx = live.svc.convert("acme", "sheet.csv", "xlsx")
    (live.root / "acme" / "sheet.xlsx").write_bytes(live.store.read("acme", rx["artifact_doc_id"])[0])
    live.store.head("acme", "sheet.xlsx")
    live.store.set_classification("acme", "sheet.xlsx", "PUBLIC")
    return types.SimpleNamespace(docx="contract.docx", xlsx="sheet.xlsx", txt="seed.txt", mods=mods)
