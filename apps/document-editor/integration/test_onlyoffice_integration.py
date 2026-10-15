"""Integration tests against a REAL stock ONLYOFFICE Document Server (throwaway container, digest-pinned).

Run with ``python apps/document-editor/integration/run_throwaway.py`` (starts/stops the container). Each test name
below is the proof referenced by ``OnlyOfficeEditBackend.PROOF_TESTS``: a capability is reported true only while its
proof test passes against the real product.
"""
from __future__ import annotations

import hashlib
import importlib
import json

import pytest

pytestmark = pytest.mark.integration

APPROVAL = {"approved": True, "by": "integration-test"}


def _m(name):
    return importlib.import_module(f"document-editor.{name}")


def test_health_and_jwt_is_enforced_by_the_real_server(live):
    h = live.backend.health()
    assert h["ok"] and h["editor_version"].startswith("9.") and h["jwt"] == "verified"
    # an UNSIGNED command is rejected by the server (error 6 = token), i.e. JWT really is on
    sf = _m("safe_fetch")
    r = sf.safe_fetch(live.url + "/command", policy=live.cfg.fetch_policy(), method="POST",
                      headers={"Content-Type": "application/json"}, body=b'{"c":"version"}')
    assert json.loads(r.body)["error"] == 6
    # a wrong secret is reported precisely
    bad = _m("onlyoffice").OnlyOfficeBackend(live.backend.decl,
                                              _m("onlyoffice").OnlyOfficeConfig(url=live.url, jwt_secret="x" * 20, zone="LOCAL"),
                                              live.content)
    with pytest.raises(_m("errors").BackendError, match="JWT secret mismatch"):
        bad.health()


def test_convert_docx_to_pdf_verified(live, seeds):
    r = live.svc.convert("acme", seeds.docx, "pdf")
    data, info = live.store.read("acme", r["artifact_doc_id"])
    assert data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:] and len(data) > 1000
    assert hashlib.sha256(data).hexdigest() == r["artifact_sha256"] == info.sha256 and r["verified"]
    assert True in live.ds_auth and False not in live.ds_auth            # the DS authenticated its fetches with our JWT
    # and a docx -> docx round trip through the real converter keeps the text
    r2 = live.svc.export("acme", seeds.docx, "docx")
    text = _m("ooxml").docx_paragraphs(live.store.read("acme", r2["artifact_doc_id"])[0])
    assert any("price is 100 EUR" in t for t in text)


def test_inspect_reports_version_and_text(live, seeds):
    i = live.svc.inspect("acme", seeds.txt)
    assert i["transmitted"] and i["editor"]["opened_by_editor"] and i["editor"]["editor_version"].startswith("9.")
    assert "Hello original world." in i["editor"]["preview"] and i["editor"]["lines"] >= 3
    assert i["version"] == 1 and len(i["sha256"]) == 64


def test_open_session_config_is_accepted_by_server(live, seeds, tmp_path):
    cdp = importlib.import_module("document-editor.integration.cdp")
    if not cdp.find_chrome():
        pytest.skip("no Chrome/Chromium available to open a real editor session")
    s = live.svc.open_session("acme", seeds.docx, user_id="alice")["session"]
    page = f"""<!doctype html><html><body><div id="ph"></div><pre id="out">PENDING</pre>
<script src="{s['editor_script_url']}"></script><script>
var cfg={json.dumps(s['config'])};
cfg.events={{onDocumentReady:function(){{document.getElementById('out').textContent='DOCEDITOR_READY';}},
 onError:function(e){{document.getElementById('out').textContent='DOCEDITOR_ERROR '+JSON.stringify(e.data);}}}};
new DocsAPI.DocEditor("ph",cfg);</script></body></html>"""
    import http.server, socketserver, threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = page.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def log_message(self, *a):
            pass
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = cdp.run_page(f"http://127.0.0.1:{httpd.server_address[1]}/", "document.getElementById('out').textContent", timeout_s=120)
    finally:
        httpd.shutdown(); httpd.server_close()
    assert out == "DOCEDITOR_READY", out
    claims = _m("jwtlib").verify(s["config"]["token"], live.secret)
    assert claims["document"]["permissions"]["edit"] is False
