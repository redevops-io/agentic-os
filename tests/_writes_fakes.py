"""In-memory ERPNext, Chatwoot and Twenty behind an httpx transport, with the failures the writes must classify."""
from __future__ import annotations

import json
from urllib.parse import unquote

import httpx

from agentic_os.governed_writes.apps import ChatwootWriter, ErpnextWriter, TwentyWriter


class FakeApps:
    def __init__(self) -> None:
        self.docs = {("Sales Invoice", "SINV-1"): {"name": "SINV-1", "customer": "Acme", "status": "Overdue", "docstatus": 1},
                     ("Supplier", "Steelco"): {"name": "Steelco"}, ("Item", "BOLT"): {"name": "BOLT"}}
        self.comments: list = []
        self.messages = {7: [{"id": 1, "content": "hello", "private": False, "message_type": 0}]}
        self.companies: dict = {}
        self.people: dict = {}
        self.posts: list = []
        self.fail: dict = {}          # (method, path fragment) -> "refuse" | "error" | "timeout" | "unreachable"
        self.note_private = True
        self.n = 100

    def _next(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}-{self.n}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = unquote(request.url.path), request.method
        for (m, fragment), how in self.fail.items():
            if m == method and fragment in path:
                if how == "unreachable":
                    raise httpx.ConnectError("refused", request=request)
                if how == "timeout":                                    # the request arrived and was carried out; the answer did not come back
                    self._route(method, path, request)
                    raise httpx.ReadTimeout("timed out", request=request)
                return httpx.Response(417 if how == "refuse" else 502, json={"exc": "SECRET-IN-BODY"})
        return self._route(method, path, request)

    def _route(self, method: str, path: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}") if method == "POST" else {}
        if method == "POST":
            self.posts.append((path, body))
        ok = lambda d: httpx.Response(200, json=d)  # noqa: E731
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["api", "resource"]:
            doctype, name = parts[2], parts[3] if len(parts) > 3 else ""
            if doctype == "Comment":
                if method == "POST":
                    row = {**body, "name": self._next("C"), "creation": "now"}
                    self.comments.append(row)
                    return ok({"data": row})
                want = {f[0]: f[2] for f in json.loads(request.url.params.get("filters", "[]"))}
                return ok({"data": [c for c in self.comments if all(c.get(k) == v for k, v in want.items())]})
            if method == "POST":
                made = self._next("PO")
                self.docs[(doctype, made)] = {**body, "name": made}
                return ok({"data": self.docs[(doctype, made)]})
            if name:
                return ok({"data": self.docs[(doctype, name)]}) if (doctype, name) in self.docs else httpx.Response(404, json={})
            return ok({"data": [d for (t, _), d in self.docs.items() if t == doctype]})
        if "conversations" in parts:
            tail = parts[parts.index("conversations") + 1:]
            if not tail:
                return ok({"data": {"payload": [{"id": i, "status": "open"} for i in self.messages]}})
            cid = int(tail[0])
            if cid not in self.messages:
                return httpx.Response(404, json={})
            if len(tail) == 1:
                return ok({"id": cid, "status": "open", "meta": {"sender": {"name": "Jo", "email": "jo@example.com"}}})
            if method == "POST":
                row = {"id": self.n + 1, "content": body["content"], "private": body["private"] and self.note_private, "message_type": 1}
                self.n += 1
                self.messages[cid].append(row)
                return ok(row)
            return ok({"payload": self.messages[cid]})
        if parts[:1] == ["rest"]:
            kind, rid = parts[1], parts[2] if len(parts) > 2 else ""
            store, one = (self.companies, "company") if kind == "companies" else (self.people, "person")
            if method == "POST":
                made = self._next(one)
                store[made] = {**body, "id": made}
                return ok({"data": {"createCompany" if kind == "companies" else "createPerson": store[made]}})
            if rid:
                return ok({"data": {one: store[rid]}}) if rid in store else httpx.Response(404, json={})
            return ok({"data": {kind: list(store.values())}})
        return httpx.Response(404, json={})

    def writers(self, *, shadow: bool = False) -> dict:
        t = httpx.MockTransport(self.handle)
        return {"erpnext": ErpnextWriter("http://erp.test", "k", "s", transport=t),
                "chatwoot": ChatwootWriter("http://cw.test", "tok", transport=t),
                "twenty": TwentyWriter("http://crm.test", "key", shadow=shadow, transport=t)}
