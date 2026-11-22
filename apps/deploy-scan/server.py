"""The deploy-scan MCP service: a single-endpoint JSON-RPC HTTP server matching exactly what
apps/edge-sentinel/deploy_scan.py already sends — {"jsonrpc":"2.0","method":"tools/call","params":
{"name":...,"arguments":...}} in, {"result":{"structuredContent":{...}}} out.

Run it with the operator's own credentials already configured (kubeconfig on PATH/KUBECONFIG, nmap
installed) and SCAN_ALLOWLIST naming exactly what it may touch:

    pip install -e apps/deploy-scan
    SCAN_ALLOWLIST="web-baseline=127.0.0.1:28787,k8s-rbac=my-context/my-namespace" \
        deploy-scan --port 8900

Binds to loopback only by default — this service is meant to sit beside the client that calls it (Edge
Sentinel, on the same host or a private network), never to be reachable from the internet.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .allowlist import is_allowed, load as load_allowlist
from .jobs import SCANNERS, JobStore

_STORE = JobStore()


def _error(message: str) -> dict:
    return {"error": message}


def _handle_scan_start(arguments: dict) -> dict:
    target = (arguments.get("target") or "").strip()
    profile = (arguments.get("profile") or "web-baseline").strip()
    if not target:
        return _error("target is required")
    if profile not in SCANNERS:
        return _error(f"unknown profile {profile!r}; one of {sorted(SCANNERS)}")
    entries = load_allowlist()
    if not is_allowed(profile, target, entries):
        return _error(f"{profile}={target} is not in SCAN_ALLOWLIST; refusing (fail closed)")
    job = _STORE.start(profile, target)
    return {"job_id": job.job_id, "state": job.state}


def _handle_scan_status(arguments: dict) -> dict:
    job = _STORE.get((arguments.get("job_id") or "").strip())
    if job is None:
        return _error("no such job_id")
    return job.to_status()


def _handle_scan_results(arguments: dict) -> dict:
    job = _STORE.get((arguments.get("job_id") or "").strip())
    if job is None:
        return _error("no such job_id")
    return job.to_results()


_TOOLS = {
    "scan_start": _handle_scan_start,
    "scan_status": _handle_scan_status,
    "scan_results": _handle_scan_results,
}


def dispatch(body: dict) -> dict:
    """The pure request -> response mapping, so it's testable without any HTTP machinery."""
    if body.get("method") != "tools/call":
        return {"error": {"code": "method_not_found", "message": "only tools/call is implemented"}}
    params = body.get("params") or {}
    name = params.get("name")
    handler = _TOOLS.get(name)
    if handler is None:
        return {"error": {"code": "unknown_tool", "message": f"unknown tool {name!r}"}}
    return {"result": {"structuredContent": handler(params.get("arguments") or {})}}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet; the operator's own process supervisor owns logging format
        pass

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        response = dispatch(body)
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(host: str = "127.0.0.1", port: int = 8900) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    return httpd


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.getenv("DEPLOY_SCAN_BIND_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("DEPLOY_SCAN_PORT", "8900")))
    args = p.parse_args()
    httpd = serve(args.host, args.port)
    print(f"deploy-scan listening on http://{args.host}:{args.port}/ "
          f"(SCAN_ALLOWLIST entries: {len(load_allowlist())})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
