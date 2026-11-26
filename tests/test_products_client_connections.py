"""A product that does not read a request body must not break the call after it.

Found against a real redevops-qa: for a few seconds after a cancel, every call said the service "did not answer its
readiness probe (HTTP 501)" while the service itself was healthy. The client posted ``{}`` on a connection it then
reused; a route that never read the body left those bytes on the wire, and the next request arrived as ``{}GET /readyz``."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentic_os.products.client import ProductClient
from agentic_os.products.registry import load_registry
from tests._products_fakes import write_registry


class DoesNotReadBodies(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"                      # keeps the connection open, as the real service does

    def log_message(self, *a):
        pass

    def _answer(self):
        data = json.dumps({"state": "cancelled"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer                         # never touches rfile


@pytest.fixture
def client(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), DoesNotReadBodies)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = write_registry(tmp_path, f"http://127.0.0.1:{server.server_address[1]}", kind="quality", pid="redevops-qa")
    made = ProductClient(load_registry(path).get("redevops-qa"))
    yield made
    made.close()
    server.shutdown()


def test_the_probe_and_the_next_calls_work_after_a_post_whose_body_the_product_ignored(client):
    for _ in range(4):
        assert client.post("/v1/runs/r1/cancel", {})["state"] == "cancelled"
        assert client.probe() == {"ok": True, "detail": "ready"}
        assert client.get("/v1/runs/r1")["state"] == "cancelled"
