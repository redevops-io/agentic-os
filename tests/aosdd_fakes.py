"""An in-process fake of the four apps (Chatwoot, Twenty, ERPNext, Lago) for UNIT tests of the AOS-DD adapters.

It mirrors the request/response shapes and the quirks observed on the live stack (Lago metadata is replace-whole-set and
422s on an unchanged entry; Twenty keeps a soft-deleted id and 400s a re-create; Chatwoot deletes a message as a tombstone;
ERPNext and Chatwoot do not deduplicate). It is a transport, never a verifier: the acceptance evidence comes from the live
stack. Faults: ``fail(app, method, path_substr, status, times=1, commit=False)`` -- with ``commit=True`` the effect is
applied and the reply is then lost (status 0), which is the ambiguous case the runtime must park as UNKNOWN."""
from __future__ import annotations

import json
import re
import urllib.parse
import uuid

from agentic_os.integrations.aosdd.http import Request, Resp

REF = "AOSDD-HFC-001"
BASES = {"chatwoot": "http://127.0.0.1:18101", "twenty": "http://127.0.0.1:18102", "erpnext": "http://127.0.0.1:18103",
         "lago": "http://127.0.0.1:18104", "lago_front": "http://127.0.0.1:18105"}
TOKENS = {  # app -> {role: secret-string}
    "chatwoot": {"writer": "cwTOKENwriter0001", "reader": "cwTOKENwriter0001"},
    "twenty": {"writer": "twTOKENwriter0002", "reader": "twTOKENreader0003"},
    "erpnext": {"writer": "erpKEYwriter:erpSECRETwriter", "reader": "erpKEYreader:erpSECRETreader"},
    "lago": {"writer": "lagoKEYsecret0004", "reader": "lagoKEYsecret0004"},
}
APPROVAL_SECRET = "approval-secret-for-tests-0123456789abcdef"


def config_dict(**over) -> dict:
    d = {"schema": "aosdd.config/1", "tenant": "aosdd-test", "account_id": 1, "erp_company": "Beacon Industrial Parks",
         "base_urls": dict(BASES),
         "writer": {"chatwoot": {"token": TOKENS["chatwoot"]["writer"]}, "twenty": {"token": TOKENS["twenty"]["writer"]},
                    "erpnext": {"api_key": "erpKEYwriter", "api_secret": "erpSECRETwriter"}, "lago": {"api_key": TOKENS["lago"]["writer"]}},
         "reader": {"chatwoot": {"token": TOKENS["chatwoot"]["reader"]}, "twenty": {"token": TOKENS["twenty"]["reader"]},
                    "erpnext": {"api_key": "erpKEYreader", "api_secret": "erpSECRETreader"}, "lago": {"api_key": TOKENS["lago"]["reader"]}},
         "approval_secret": APPROVAL_SECRET}
    d.update(over)
    return d


class Crash(BaseException):
    """The runtime process was killed (not an Exception: nothing in the runtime may catch it, exactly like SIGKILL)."""


class FakeStack:
    def __init__(self):
        self.calls: list[tuple] = []          # (app, method, path)
        self.roles: list[str] = []            # the credential role of each call, parallel to ``calls``
        self.crashes: list[dict] = []
        self.hooks: list[dict] = []
        self.blocks: list[dict] = []
        self.faults: list[dict] = []
        self.revoked: set = set()             # (app, role)
        self.denied_write_roles = {"twenty": "reader", "erpnext": "reader"}
        self.company_id = "df239f4e-4333-49c6-b997-18dec458d918"
        self.lago_invoice_id = "8eddd35e-aa0b-4962-941a-976424864f57"
        self.lago_customer_id = "43f689bc-f1b5-45b0-979b-0909085bb10b"
        self.opp_id = "8851a2f4-d526-4e48-80a3-5468c33015a0"
        self.quotations: dict[str, dict] = {"SAL-QTN-2026-00001": {"name": "SAL-QTN-2026-00001", "status": "Draft", "grand_total": 48000.0,
                                                                  "valid_till": "2026-11-27", "party_name": "Harborview Freight Co",
                                                                  "docstatus": 1, "title": "Harborview Freight Co"}}
        self._qn = 1
        self.lago_metadata: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.task_targets: dict[str, dict] = {}
        self.messages: list[dict] = [{"id": 1, "content": "Where is my invoice?", "private": False, "message_type": 0,
                                      "conversation_id": 1, "content_attributes": {}}]
        self._mid = 1

    # ---- test controls -------------------------------------------------------------------------------------
    def fail(self, app, method, path_substr, status, times=1, commit=False, body=None):
        self.faults.append({"app": app, "method": method, "sub": path_substr, "status": status, "times": times,
                            "commit": commit, "body": body})

    def crash(self, app, method, path_substr, after_effect=True):
        """Raise ``Crash`` the first time this request arrives: after the effect was applied (the reply is lost) or before it."""
        self.crashes.append({"app": app, "method": method, "sub": path_substr, "after": after_effect, "done": False})

    def on_effect(self, app, method, path_substr, fn):
        self.hooks.append({"app": app, "method": method, "sub": path_substr, "fn": fn})

    def block(self, app, method, path_substr, entered, release):
        self.blocks.append({"app": app, "method": method, "sub": path_substr, "entered": entered, "release": release, "used": False})

    def revoke(self, app, role="writer"):
        self.revoked.add((app, role))

    def count(self, app, method=None, sub=""):
        return sum(1 for a, m, p in self.calls if a == app and (method is None or m == method) and sub in p)

    def writes(self):
        return [(a, m, p) for a, m, p in self.calls if m in ("POST", "PUT", "DELETE", "PATCH")]

    # ---- Transport ------------------------------------------------------------------------------------------
    def send(self, req: Request) -> Resp:
        app = next((a for a, b in BASES.items() if req.url.startswith(b)), None)
        path = req.url[len(BASES[app]):]
        self.calls.append((app, req.method, path))
        role = self._role(app, req.headers)
        self.roles.append(role or "none")
        for b in self.blocks:
            if not b["used"] and b["app"] == app and b["method"] == req.method and b["sub"] in path:
                b["used"] = True
                b["entered"].set()
                b["release"].wait(20)
        cr = next((c for c in self.crashes if not c["done"] and c["app"] == app and c["method"] == req.method and c["sub"] in path), None)
        if cr and not cr["after"]:
            cr["done"] = True
            raise Crash(f"killed before {req.method} {path}")
        if role is None or (app, role) in self.revoked:
            body = {"statusCode": 403, "messages": ["This API Key is revoked"]} if (role and app == "twenty") else {"error": "Invalid Access Token"}
            return Resp(403 if (role and app == "twenty") else 401, body, json.dumps(body))
        if req.method != "GET" and self.denied_write_roles.get(app) == role:
            body = {"exc_type": "PermissionError"} if app == "erpnext" else {"error": "PERMISSION_DENIED"}
            return Resp(403 if app == "erpnext" else 400, body, json.dumps(body))
        fault = next((f for f in self.faults if f["app"] == app and f["method"] == req.method and f["sub"] in path and f["times"] > 0), None)
        if fault and not fault["commit"]:
            fault["times"] -= 1
            return Resp(fault["status"], fault["body"] or {"error": "injected"}, json.dumps(fault["body"] or {"error": "injected"}))
        resp = getattr(self, "_" + app)(req, path)
        for h in self.hooks:
            if h["app"] == app and h["method"] == req.method and h["sub"] in path:
                h["fn"](self)
        if cr and cr["after"]:
            cr["done"] = True
            raise Crash(f"killed after {req.method} {path} was applied")
        if fault and fault["commit"]:
            fault["times"] -= 1
            return Resp(0, {"transport_error": "TimeoutError"}, "TimeoutError")
        return resp

    def _role(self, app, headers):
        h = {k.lower(): v for k, v in headers.items()}
        presented = h.get("api_access_token") or (h.get("authorization", "").split(" ", 1)[-1] if app != "erpnext" else h.get("authorization", "")[6:])
        for role, tok in TOKENS[app].items():
            if presented == tok:
                return role
        return None

    @staticmethod
    def _ok(body, status=200):
        return Resp(status, body, json.dumps(body))

    @staticmethod
    def _qs(path):
        u = urllib.parse.urlsplit(path)
        return u.path, urllib.parse.parse_qs(u.query)

    # ---- ERPNext ---------------------------------------------------------------------------------------------
    def _erpnext(self, req, path):
        p, qs = self._qs(path)
        p = urllib.parse.unquote(p)
        if req.method == "GET" and p == "/api/resource/Customer":
            return self._ok({"data": [{"name": "Harborview Freight Co", "customer_ref": REF}]})
        if req.method == "GET" and p == "/api/resource/Sales Invoice":
            return self._ok({"data": [{"name": "ACC-SINV-2026-00001", "status": "Overdue", "outstanding_amount": 12000.0, "due_date": "2026-10-20",
                                       "grand_total": 12000.0, "currency": "USD"}]})
        if req.method == "GET" and p == "/api/resource/Quotation":
            flt = json.loads(qs.get("filters", ["[]"])[0])
            rows = [q for q in self.quotations.values() if all(self._match(q, f) for f in flt)]
            return self._ok({"data": [dict(q) for q in rows]})
        if p.startswith("/api/resource/Quotation/"):
            name = p.rsplit("/", 1)[1]
            q = self.quotations.get(name)
            if req.method == "GET":
                return self._ok({"data": dict(q)}) if q else Resp(404, {"exc_type": "DoesNotExistError"}, "{}")
            if req.method == "DELETE":
                if not q:
                    return Resp(404, {"exc_type": "DoesNotExistError"}, "{}")
                if q["docstatus"] != 0:
                    return Resp(417, {"exc_type": "ValidationError"}, "{}")
                del self.quotations[name]
                return Resp(202, {"message": "ok"}, "{}")
        if req.method == "POST" and p == "/api/resource/Quotation":
            b = json.loads(req.body)
            self._qn += 1
            name = "SAL-QTN-2026-%05d" % self._qn
            total = sum(i["qty"] * i["rate"] for i in b["items"])
            doc = {**b, "name": name, "docstatus": 0, "grand_total": float(total), "status": "Draft"}
            self.quotations[name] = doc
            return self._ok({"data": dict(doc)})
        return Resp(404, {"error": "no route " + p}, "{}")

    @staticmethod
    def _match(doc, f):
        k, op, v = f
        return doc.get(k) == v if op == "=" else False

    # ---- Lago --------------------------------------------------------------------------------------------------
    def _inv(self):
        return {"lago_id": self.lago_invoice_id, "number": "BEA-97FA-001-001", "payment_due_date": "2026-10-20", "payment_overdue": True,
                "payment_status": "pending", "total_due_amount_cents": 730435, "currency": "USD",
                "web_url": "%s/beacon-industrial-parks/customer/%s/invoice/%s/overview" % (BASES["lago_front"], self.lago_customer_id, self.lago_invoice_id),
                "metadata": [dict(m) for m in self.lago_metadata]}

    def _lago(self, req, path):
        p, qs = self._qs(path)
        if req.method == "GET" and p == "/api/v1/customers/" + REF:
            return self._ok({"customer": {"lago_id": self.lago_customer_id, "external_id": REF, "email": "accounts@harborview-freight.example",
                                          "name": "Harborview Freight Co"}})
        if req.method == "GET" and p == "/api/v1/invoices":
            return self._ok({"invoices": [self._inv()]})
        if p == "/api/v1/invoices/" + self.lago_invoice_id:
            if req.method == "GET":
                return self._ok({"invoice": self._inv()})
            if req.method == "PUT":
                new = json.loads(req.body)["invoice"]["metadata"]
                for m in new:
                    if len(m["key"]) > 20:
                        return Resp(422, {"error": "unprocessable"}, "{}")
                    if any(o["key"] == m["key"] and o["value"] == m["value"] for o in self.lago_metadata):
                        return Resp(422, {"status": 422, "error": "Unprocessable Entity", "error_details": {"metadata": {"key": ["value_already_exist"]}}}, '{"value_already_exist":1}')
                self.lago_metadata = [{"lago_id": str(uuid.uuid4()), **m} for m in new]
                return self._ok({"invoice": self._inv()})
        return Resp(404, {"error": "no route " + p}, "{}")

    # ---- Twenty ------------------------------------------------------------------------------------------------
    def _twenty(self, req, path):
        p, qs = self._qs(path)
        if req.method == "GET" and p == "/rest/companies":
            return self._ok({"data": {"companies": [{"id": self.company_id, "name": "Harborview Freight Co", "customerRef": REF}]}})
        if req.method == "GET" and p == "/rest/opportunities":
            return self._ok({"data": {"opportunities": [{"id": self.opp_id, "name": "Renewal", "stage": "PROPOSAL", "closeDate": "2026-11-27T00:00:00.000Z",
                                                          "amount": {"amountMicros": 48000000000, "currencyCode": "USD"}}]}})
        m = re.fullmatch(r"/rest/tasks/([0-9a-f-]+)", p)
        if m:
            tid = m.group(1)
            t = self.tasks.get(tid)
            if req.method == "GET":
                return self._ok({"data": {"task": dict(t)}}) if t and not t["deletedAt"] else Resp(404, {"statusCode": 404, "messages": ["Record not found"]}, "{}")
            if req.method == "DELETE" and qs.get("soft_delete") == ["true"]:
                if not t:
                    return Resp(404, {"statusCode": 404}, "{}")
                t["deletedAt"] = "2026-11-06T00:00:00Z"
                return self._ok({"data": {"deleteTask": {"id": tid}}})
            return Resp(400, {"messages": ["PERMISSION_DENIED"]}, '{"PERMISSION_DENIED":1}')
        if req.method == "POST" and p == "/rest/tasks":
            b = json.loads(req.body)
            if b["id"] in self.tasks:
                return Resp(400, {"messages": ["duplicate key value violates unique constraint task_pkey"]}, '{"duplicate key":1}')
            self.tasks[b["id"]] = {**b, "deletedAt": None}
            return self._ok({"data": {"createTask": dict(self.tasks[b["id"]])}}, 201)
        if req.method == "POST" and p == "/rest/taskTargets":
            b = json.loads(req.body)
            self.task_targets[b["id"]] = {**b, "deletedAt": None}
            return self._ok({"data": {"createTaskTarget": dict(self.task_targets[b["id"]])}}, 201)
        if req.method == "GET" and p == "/rest/taskTargets":
            flt = qs.get("filter", [""])[0]
            mm = re.search(r'taskId\[eq\]:"([^"]+)"', flt)
            rows = [dict(t) for t in self.task_targets.values() if mm and t["taskId"] == mm.group(1) and not t["deletedAt"]]
            return self._ok({"data": {"taskTargets": rows}})
        m = re.fullmatch(r"/rest/taskTargets/([0-9a-f-]+)", p)
        if m and req.method == "DELETE" and qs.get("soft_delete") == ["true"]:
            t = self.task_targets.get(m.group(1))
            if not t:
                return Resp(404, {"statusCode": 404}, "{}")
            t["deletedAt"] = "2026-11-06T00:00:00Z"
            return self._ok({"data": {"deleteTaskTarget": {"id": m.group(1)}}})
        return Resp(404, {"error": "no route " + p}, "{}")

    # ---- Chatwoot -----------------------------------------------------------------------------------------------
    def _chatwoot(self, req, path):
        p, qs = self._qs(path)
        if req.method == "GET" and p == "/api/v1/accounts/1/contacts/search":
            return self._ok({"payload": [{"id": 1, "identifier": REF, "email": "dana.whitfield@harborview-freight.example", "name": "Dana Whitfield"}]})
        if req.method == "GET" and p == "/api/v1/accounts/1/contacts/1/conversations":
            return self._ok({"payload": [{"id": 1, "status": "open", "inbox_id": 1}]})
        if p == "/api/v1/accounts/1/conversations/1/messages":
            if req.method == "GET":
                return self._ok({"payload": [dict(m) for m in self.messages]})
            if req.method == "POST":
                b = json.loads(req.body)
                self._mid += 1
                msg = {"id": self._mid, "content": b["content"], "private": b.get("private", False), "message_type": 1, "conversation_id": 1, "content_attributes": {}}
                self.messages.append(msg)
                return self._ok(dict(msg))
        m = re.fullmatch(r"/api/v1/accounts/1/conversations/1/messages/(\d+)", p)
        if m and req.method == "DELETE":
            for msg in self.messages:
                if msg["id"] == int(m.group(1)):
                    msg["content"], msg["content_attributes"] = "This message was deleted", {"deleted": True}
                    return self._ok(dict(msg))
            return Resp(404, {"error": "not found"}, "{}")
        return Resp(404, {"error": "no route " + p}, "{}")
