"""A REAL HTTP server (stdlib, a thread, a real socket) that speaks the Edge Sentinel / redevops-qa product contract
(``docs/products/README.md``), used by the products tests so ProductClient, the hub, projections, tools and approvals
are exercised over a real connection.

It is a contract double, not a product: its approval-token handling uses the SAME ``agentic_os.mission.approval``
functions a product uses (HMAC, digest-bound inputs, single use through a replay guard), and it records the decision
actor from the token marker through a linked-user table, so the tests prove what the product side would see.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from agentic_os.mission.approval import ReplayGuard, verify_approval_token

SERVICE_TOKEN = "svc-" + "s" * 40
DECISION_TOKEN = "dec-" + "d" * 40
SECRET = "approval-secret-" + "x" * 32
CASE = "case-0123456789abcdef"
ACT = "act-0123456789abcdef"


def digest_of(params: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


class FakeSentinel:
    """State + behaviour; ``serve()`` runs it on a loopback port."""

    def __init__(self, *, dry_run: bool = True, linked: Optional[Dict[str, str]] = None, ready: bool = True) -> None:
        self.dry_run = dry_run
        self.ready = ready
        self.linked = {"projects:local-owner": "alex"} if linked is None else linked
        self.actions: Dict[str, Dict[str, Any]] = {}
        self.cases: Dict[str, Dict[str, Any]] = {}
        self.changes: List[Dict[str, Any]] = []
        self.guard = ReplayGuard()
        self.calls: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.executions = 0
        self.server: Optional[ThreadingHTTPServer] = None
        self.verify_ok = True
        self.add_alert_case()

    # ── state ────────────────────────────────────────────────────────────────────────────────────
    def _chg(self, typ: str, **kw: Any) -> None:
        self.changes.append({"seq": len(self.changes) + 1, "at": "2026-11-13T12:00:00Z", "type": typ, **kw})

    def add_alert_case(self, ip: str = "203.0.113.50", cid: str = CASE, rid: str = ACT) -> None:
        params = {"ip": ip, "duration_hours": 4}
        self.cases[cid] = {"case_id": cid, "severity": "high", "status": "OPEN", "title": f"CrowdSec alert from {ip}",
                           "case_type": "alert", "evidence": [{"artifact_id": "ev-1", "sha256": "ab" * 32, "kind": "alert",
                                                                "source": "crowdsec", "acquired_at": "2026-11-13T11:59:00Z"}]}
        self.actions[rid] = {"request_id": rid, "case_id": cid, "capability": "sentinel.block_ip", "params": params,
                             "digest": digest_of({"c": "sentinel.block_ip", **params}), "state": "AWAITING_APPROVAL",
                             "attempts": 0, "decision": None, "receipt": None, "verification": None}
        self._chg("action.proposed", case_id=cid, request_id=rid)

    # ── http ─────────────────────────────────────────────────────────────────────────────────────
    def serve(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, status: int, body: Any = None, ctype: str = "application/json") -> None:
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self, decision: bool = False) -> bool:
                tok = self.headers.get("X-Control-Token", "")
                ok = tok == (DECISION_TOKEN if decision else SERVICE_TOKEN) or (not decision and tok == DECISION_TOKEN)
                if not ok:
                    self._send(401, {"error": "unauthenticated", "detail": "bad or missing control token"})
                return ok

            def do_GET(self):  # noqa: N802
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                with fake.lock:
                    fake.calls.append({"method": "GET", "path": u.path, "headers": dict(self.headers)})
                if u.path == "/readyz":
                    return self._send(200 if fake.ready else 503, {"status": "ready" if fake.ready else "starting"})
                if not self._auth():
                    return
                p = u.path
                if p == "/api/v1/info":
                    return self._send(200, {"version": "0.6.3", "api_version": 1, "tenant_label": "Edge Sentinel",
                                            "mode": {"demo": False, "block_enabled": not fake.dry_run, "dry_run": fake.dry_run},
                                            "features": {"cases": True, "actions": True, "events": True},
                                            "unavailable": [{"feature": "hunt", "code": "NOT_WIRED",
                                                             "reason": "module present, no route yet", "remedy": ""}]})
                if p == "/api/v1/posture":
                    return self._send(200, {"decisions_active": 2, "alerts_total": 7, "by_severity": {"high": 3, "low": 4},
                                            "connected": True, "source": "live"})
                if p == "/api/v1/cases":
                    return self._send(200, {"items": list(fake.cases.values()), "next_cursor": None})
                if p.startswith("/api/v1/cases/"):
                    cid = p.rsplit("/", 1)[1]
                    c = fake.cases.get(cid)
                    if not c:
                        return self._send(404, {"error": "not_found", "detail": "no such case"})
                    acts = [a for a in fake.actions.values() if a["case_id"] == cid]
                    return self._send(200, {"case": c, "evidence": c.get("evidence", []), "actions": acts, "actions_detail": acts,
                                            "summary": {k: c[k] for k in ("case_id", "severity", "status", "title", "case_type")}})
                if p == "/api/v1/actions":
                    items = [a for a in fake.actions.values() if not q.get("state") or a["state"] == q["state"]]
                    return self._send(200, {"items": items[::-1], "next_cursor": None})
                if p.endswith("/verification"):
                    a = fake.actions.get(p.split("/")[-2])
                    if not a:
                        return self._send(404, {"error": "not_found", "detail": "no such action"})
                    return self._send(200, a["verification"] or {"state": "not_run"})
                if p.startswith("/api/v1/actions/"):
                    a = fake.actions.get(p.rsplit("/", 1)[1])
                    if not a:
                        return self._send(404, {"error": "not_found", "detail": "no such action"})
                    return self._send(200, {**a, "mode": {"dry_run": fake.dry_run, "demo": False}})
                if p == "/api/v1/changes":
                    after = int(q.get("after", 0))
                    return self._send(200, {"items": [c for c in fake.changes if c["seq"] > after], "head": len(fake.changes)})
                return self._send(404, {"error": "not_found", "detail": "no route"})

            def do_POST(self):  # noqa: N802
                u = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with fake.lock:
                    fake.calls.append({"method": "POST", "path": u.path, "headers": dict(self.headers), "body": body})
                p = u.path
                decision_route = p.endswith("/decision") or p.endswith("/execute")
                if not self._auth(decision=decision_route):
                    return
                if p == "/api/v1/cases":
                    ip = body.get("ip", "")
                    import ipaddress
                    try:
                        ipaddress.ip_address(ip)
                    except ValueError:
                        return self._send(422, {"error": "refused", "message": "not an IP address"})
                    if ip == "127.0.0.1":
                        return self._send(422, {"error": "refused", "message": "protected address"})
                    if body.get("type") not in ("manual_block", "manual_unblock"):
                        return self._send(422, {"error": "invalid_type", "message": "type"})
                    n_ = len(fake.actions) + 1
                    cid = f"case-{n_:016x}"
                    rid = f"act-{n_:016x}"
                    hours = int(str(body.get("duration", "4h")).rstrip("h") or 4)
                    params = {"ip": ip, "duration_hours": hours}
                    fake.cases[cid] = {"case_id": cid, "severity": "medium", "status": "OPEN", "title": f"manual: {body.get('reason', '')}",
                                       "case_type": body["type"], "evidence": []}
                    cap = "sentinel.block_ip" if body["type"] == "manual_block" else "sentinel.unblock_ip"
                    fake.actions[rid] = {"request_id": rid, "case_id": cid, "capability": cap, "params": params,
                                         "digest": digest_of({"c": cap, **params}), "state": "AWAITING_APPROVAL", "attempts": 0,
                                         "decision": None, "receipt": None, "verification": None}
                    fake._chg("action.proposed", case_id=cid, request_id=rid, actor=self.headers.get("X-Acting-User", ""))
                    return self._send(201, {"case_id": cid, "request_id": rid, "state": "AWAITING_APPROVAL", "digest": fake.actions[rid]["digest"]})
                parts = p.split("/")
                if len(parts) == 8 and parts[3] == "cases" and parts[5] == "actions":
                    cid, rid, verb = parts[4], parts[6], parts[7]
                    a = fake.actions.get(rid)
                    if not a or a["case_id"] != cid:
                        return self._send(404, {"error": "not_found", "detail": "no such action"})
                    token = self.headers.get("X-Approval-Token", "")
                    if verb == "decision":
                        inputs = {"case_id": cid, "request_id": rid, "approved": body.get("approved"), "digest": body.get("digest")}
                        cap = "sentinel.decide"
                    else:
                        inputs = {"case_id": cid, "request_id": rid, "digest": body.get("digest")}
                        cap = "sentinel.execute"
                    marker = verify_approval_token(SECRET, token, operator="edge-sentinel", capability=cap, inputs=inputs,
                                                   replay_guard=fake.guard)
                    if marker is None:
                        return self._send(403, {"error": "human_decision_required", "detail": "approval token missing, invalid, replayed or for different content"})
                    user = fake.linked.get(str(marker.get("by")))
                    if not user:
                        return self._send(403, {"error": "actor_not_linked", "detail": "this Projects user is not linked to a product user",
                                                "remedy": "link it in the product"})
                    if body.get("digest") != a["digest"]:
                        return self._send(409, {"error": "stale_approval", "detail": "digest differs"})
                    if verb == "decision":
                        if a["state"] != "AWAITING_APPROVAL":
                            return self._send(409, {"error": "conflict", "detail": "not awaiting approval"})
                        a["decision"] = {"actor": f"user:{user}", "via": marker.get("via"), "approved": bool(body.get("approved")),
                                         "decision_id": "dec-1"}
                        a["state"] = "APPROVED" if body.get("approved") else "REJECTED"
                        fake._chg("action.decided", case_id=cid, request_id=rid, actor=f"user:{user}")
                        return self._send(200, {"decision": a["decision"], "action_state": a["state"], "awaiting_execution": a["state"] == "APPROVED"})
                    if a["state"] != "APPROVED":
                        return self._send(409, {"error": "conflict", "detail": "not approved"})
                    with fake.lock:
                        fake.executions += 1
                    a["state"] = "EXECUTED"
                    a["attempts"] += 1
                    a["receipt"] = {"receipt_id": "rcpt-0123456789abcdef", "status": "DRY_RUN" if fake.dry_run else "SUCCEEDED"}
                    a["verification"] = ({"state": "dry_run", "detail": "dry run", "at": "2026-11-13T12:05:00Z"} if fake.dry_run else
                                         {"state": "verified" if fake.verify_ok else "unverified", "detail": "read back from LAPI",
                                          "at": "2026-11-13T12:05:00Z", "receipt_id": "rcpt-0123456789abcdef"})
                    fake._chg("action.executed", case_id=cid, request_id=rid, actor=f"user:{user}")
                    return self._send(200, {"executed": True, "dry_run": fake.dry_run, "receipt": a["receipt"], "verification": a["verification"]})
                return self._send(404, {"error": "not_found", "detail": "no route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def write_registry(tmp_path, base: str, *, kind: str = "security", pid: str = "edge-sentinel", with_tokens: bool = True,
                   single_owner: str = "local-owner", extra_products: str = "") -> str:
    """A registry file + token files under ``tmp_path``; returns the registry path."""
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    (d / "service").write_text(SERVICE_TOKEN if with_tokens else "")
    (d / "decision").write_text(DECISION_TOKEN)
    (d / "secret").write_text(SECRET)
    reg = tmp_path / "products.yaml"
    reg.write_text(f"""
schema: aos.products/v1
principal: {{single_owner: {single_owner}}}
products:
  - id: {pid}
    display_name: {'Edge Sentinel' if kind == 'security' else 'redevops-qa'}
    kind: {kind}
    api_base: {base}
    ui_url: {base}/ui
    health: {{path: /readyz, interval_s: 1, timeout_s: 2}}
    contract: {{api: {'/api/v1' if kind == 'security' else '/v1'}, info: {'/api/v1/info' if kind == 'security' else '/v1/info'}, min_version: "1"}}
    auth:
      service_token_file: {d / 'service'}
      decision_token_file: {d / 'decision'}
      approval_secret_file: {d / 'secret'}
      scheme: {'control_token' if kind == 'security' else 'bearer'}
{extra_products}""")
    return str(reg)


RUN = "r20261113T120000Z-abc123"
TGT = "tgt-0123456789ab"
EST = "sha256:" + "e" * 64


class FakeQa:
    """redevops-qa contract double (``/v1``), used only with the gate opened by a test."""

    def __init__(self) -> None:
        self.guard = ReplayGuard()
        self.calls: List[Dict[str, Any]] = []
        self.started: List[Dict[str, Any]] = []
        self.runs: Dict[str, Dict[str, Any]] = {RUN: {
            "id": RUN, "plan": "demo", "profile": "edge-sentinel-fixture-readonly", "target_id": TGT, "state": "awaiting_approval",
            "estimate_digest": EST, "outcome": None, "level": None, "progress": None}}
        self.server: Optional[ThreadingHTTPServer] = None

    def serve(self) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                u = urlsplit(self.path)
                fake.calls.append({"method": "GET", "path": u.path})
                if u.path == "/readyz":
                    return self._send(200, {"status": "ready"})
                if self.headers.get("Authorization") not in (f"Bearer {SERVICE_TOKEN}", f"Bearer {DECISION_TOKEN}"):
                    return self._send(401, {"error": "unauthenticated", "detail": "no"})
                p = u.path
                if p == "/v1/info":
                    return self._send(200, {"version": "0.6.3", "profiles": [{"id": "edge-sentinel-fixture-readonly", "available": True},
                                                                              {"id": "robot", "available": False, "unavailable": {"code": "ARCHITECTURE_UNSUPPORTED", "reason": "arm64", "remedy": "x86_64"}}]})
                if p == "/v1/targets":
                    return self._send(200, {"items": [{"id": TGT, "name": "edge sentinel fixture", "kind": "external-origin"}]})
                if p == "/v1/profiles":
                    return self._send(200, {"items": [{"id": "edge-sentinel-fixture-readonly", "available": True}]})
                if p == "/v1/runs":
                    return self._send(200, {"items": list(fake.runs.values()), "next_cursor": None})
                if p.startswith("/v1/runs/") and p.endswith("/events"):
                    return self._send(200, {"items": [{"seq": 1, "type": "run.approval_requested", "label": "waiting"}], "head": 1, "terminal": False})
                if p.startswith("/v1/runs/"):
                    r = fake.runs.get(p.rsplit("/", 1)[1])
                    return self._send(200, r) if r else self._send(404, {"error": "not_found", "detail": "no run"})
                return self._send(404, {"error": "not_found", "detail": "no route"})

            def do_POST(self):  # noqa: N802
                u = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.calls.append({"method": "POST", "path": u.path, "body": body, "headers": dict(self.headers)})
                p = u.path
                if p == "/v1/runs":
                    fake.started.append(body)
                    return self._send(202, {**fake.runs[RUN], "state": "awaiting_approval"})
                if p == "/v1/estimates":
                    return self._send(200, {"estimate_id": "est-1", "digest": EST, "will_refuse": False})
                if p.endswith("/approval"):
                    run_id = p.split("/")[3]
                    inputs = {"run_id": run_id, "approved": body.get("approved"), "estimate_digest": body.get("estimate_digest")}
                    m = verify_approval_token(SECRET, self.headers.get("X-Approval-Token", ""), operator="redevops-qa",
                                              capability="qa.approve_run", inputs=inputs, replay_guard=fake.guard)
                    if m is None:
                        return self._send(403, {"error": "human_decision_required", "detail": "bad token"})
                    fake.runs[run_id]["state"] = "queued" if body.get("approved") else "rejected"
                    return self._send(200, {"state": fake.runs[run_id]["state"], "approved_by": m.get("by")})
                return self._send(404, {"error": "not_found", "detail": "no route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
