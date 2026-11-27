"""Economic intelligence for external agents, through the mounted Projects app: investigate, explain, cite evidence within
the caller's source scopes, and propose the payment correction that a person approves. The ERP fake speaks ERPNext's own
HTTP routes, so the real writer's reads, cancel, insert and submit are what run, and ``economics.reconcile`` judges."""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.governed_writes.apps import ErpnextWriter  # noqa: E402
from agentic_os.integrations import chatwoot as chatwoot_mod  # noqa: E402
from agentic_os.integrations import twenty as twenty_mod  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests.test_economics import base_rows  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

BASE = "/api/agent-gateway/capabilities"
APPROVER = {"Authorization": "Bearer " + "A" * 40}
SECRET_NOTE, CRM_WORDS = "INTERNAL-ONLY: the 10% discount was agreed by Sam", "CRM-NOTE-WORDS: 10% discount for Acme"
LIMITS = {"acme": {"policy_refs": ["app:erpnext", "app:chatwoot", "app:twenty", "writes:decide", "egress:financial"]},
          "noerp": {"policy_refs": ["app:chatwoot", "egress:financial"]},
          "nomoney": {"policy_refs": ["app:erpnext"]},
          "deskless": {"policy_refs": ["app:erpnext", "egress:financial"]}}
FIX = {"reference": "WIRE-C1", "payment_entry": "PAY-1", "posted_amount": 3800, "bank_amount": 3380}


class Ledger:
    """ERPNext's REST routes over the economics fixtures, with payment entries that can be cancelled and amended."""

    def __init__(self):
        self.rows = base_rows()
        for r in self.rows["Timesheet Detail"]:
            r.pop("custom_supplier"), r.pop("custom_reference")
        self.rows["Bank Transaction"] = [{"name": "BT-1", "deposit": 3380, "withdrawal": 0, "reference_number": "WIRE-C1", "docstatus": 1}]
        # communications are tied to a customer only by an explicit identifier both sides hold, never by a name
        self.rows["Customer"] = [{"name": n, "email_id": f"ap@{n.lower()}.example", "website": f"https://{n.lower()}.example"}
                                 for n in ("Acme", "Birch", "Cedar", "Dune")]
        self.payments = {"PAY-1": {"name": "PAY-1", "docstatus": 1, "payment_type": "Receive", "party_type": "Customer", "party": "Cedar",
                                   "paid_amount": 3800, "received_amount": 3800, "reference_no": "WIRE-C1", "company": "Co",
                                   "paid_from": "Debtors", "paid_to": "Bank", "posting_date": "2026-09-01", "deductions": [], "taxes": [],
                                   "references": [{"reference_doctype": "Sales Invoice", "reference_name": "S5", "allocated_amount": 3800}]}}
        self.writes, self.fail_on = [], ""

    def listing(self, doctype):
        if doctype == "Payment Entry":
            return [{k: v for k, v in d.items() if not isinstance(v, list)} for d in self.payments.values()]
        if doctype == "Payment Entry Reference":
            return [{"parent": n, **r} for n, d in self.payments.items() if d["docstatus"] != 0 for r in d["references"]]
        return self.rows.get(doctype, [])

    def handle(self, request):
        path, method = unquote(request.url.path), request.method
        body = json.loads(request.content) if request.content else {}
        if method != "GET":
            self.writes.append((method, path))
            if self.fail_on and self.fail_on in path:
                return httpx.Response(500, json={})
        if path == "/api/method/frappe.client.cancel":
            self.payments[body["name"]]["docstatus"] = 2
            return httpx.Response(200, json={"message": {}})
        if path == "/api/method/frappe.client.submit":
            self.payments[json.loads(body["doc"])["name"]]["docstatus"] = 1
            return httpx.Response(200, json={"message": {}})
        parts = path.removeprefix("/api/resource/").split("/")
        if method == "POST" and parts == ["Payment Entry"]:
            name = f"PAY-{len(self.payments) + 1}"
            self.payments[name] = {"deductions": [], "taxes": [], **body, "name": name}
            return httpx.Response(200, json={"data": self.payments[name]})
        if method == "GET" and len(parts) == 2 and parts[0] == "Payment Entry":
            doc = self.payments.get(parts[1])
            return httpx.Response(200, json={"data": doc}) if doc else httpx.Response(404, json={})
        if method == "GET" and len(parts) == 1:
            fields = json.loads(request.url.params.get("fields", "[]"))
            rows = self.listing(parts[0])
            return httpx.Response(200, json={"data": [{k: r[k] for k in fields if k in r} for r in rows]})
        return httpx.Response(404, json={})

    def writer(self):
        return ErpnextWriter("http://erp.test", "k", "s", transport=httpx.MockTransport(self.handle))


def second(led, amount):
    """Another submitted payment under the same reference."""
    return {"PAY-9": {**led.payments["PAY-1"], "name": "PAY-9", "paid_amount": amount, "received_amount": amount,
                      "references": [{"reference_doctype": "Sales Invoice", "reference_name": "S6", "allocated_amount": amount}]}}


def bank_line(name, deposit, withdrawal=0):
    return {"name": name, "deposit": deposit, "withdrawal": withdrawal, "reference_number": "WIRE-C1", "docstatus": 1}


class Desk:
    base_url, account_id = "http://desk.test", "1"

    def read_conversations(self, status="all"):
        return [{"id": 1, "meta": {"sender": {"name": "Acme", "email": "ap@acme.example"}}}]

    def read_messages(self, cid):
        return [{"id": 10, "message_type": 0, "private": False, "content": "Please confirm the 10% discount on our order, ap@acme.example", "created_at": 1_786_500_000, "sender": {"name": "Acme"}},
                {"id": 11, "message_type": 1, "private": True, "content": SECRET_NOTE, "created_at": 1_786_500_100, "sender": {"name": "Agent"}}]


class Crm:
    base_url = "http://crm.test"

    def read_all(self, obj):
        return {"notes": [{"id": "n1", "title": "S2", "bodyV2": {"markdown": CRM_WORDS}, "createdAt": "2026-08-12T10:00:00Z", "updatedAt": "2026-08-12T10:00:00Z"}],
                "noteTargets": [{"noteId": "n1", "targetCompanyId": "c1"}], "companies": [{"id": "c1", "name": "Acme", "domainName": {"primaryLinkUrl": "https://acme.example"}}]}[obj]


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB", "AGENTIC_OS_PAYMENT_CORRECTIONS"):
        monkeypatch.delenv(k, raising=False)

    def build(*, corrections=True):
        ledger = Ledger()
        monkeypatch.setattr(apps_mod, "writers_from_env", lambda: {"erpnext": ledger.writer()})
        monkeypatch.setattr(chatwoot_mod, "chatwoot_from_env", Desk)
        monkeypatch.setattr(twenty_mod, "twenty_from_env", Crm)
        agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
        ui = tmp_path / "ui"
        ui.mkdir(exist_ok=True)
        (ui / "index.html").write_text("<html></html>")
        env = {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
               "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
               "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
               "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40, "PROJECTS_UI_DIR": str(ui)}
        if corrections:
            env["AGENTIC_OS_PAYMENT_CORRECTIONS"] = "1"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
        count = iter(range(100))

        def agent(grants, tenant="acme"):
            return {"Authorization": "Bearer " + AT.issue(agents, f"a{next(count)}", grants, minutes=10, tenant=tenant, authority_db=db)}

        def call(who, tool, /, **args):
            return client.post(f"{BASE}/{tool}/invoke", headers=who, json=args).json()

        def decide(rid, approved=True):
            shown = client.get(f"/api/writes/{rid}", headers=APPROVER).json()
            return client.post(f"/api/writes/{rid}/decision", headers=APPROVER, json={"approved": approved, "digest": shown["digest"]})
        return type("W", (), {"client": client, "ledger": ledger, "agent": staticmethod(agent), "call": staticmethod(call), "decide": staticmethod(decide)})

    return build


FULL = ["economics.read", "economics.propose", "egress:financial", "support.read", "crm.records.read"]


def offered(w, who):
    return sorted(c["name"] for c in w.client.get(BASE, headers=who).json()["capabilities"] if c["name"].startswith("economics."))


def test_discrepancy_to_governed_correction_and_the_application_says_it_reconciles(world):
    w = world()
    who = w.agent(FULL)
    found = w.call(who, "economics.discrepancies")
    assert found["status"] == "ok", found
    (d,) = found["output"]["discrepancies"]
    assert d["correction_inputs"] == FIX and d["invoices"] == ["S5"] and d["proposed_correction"]["applied"] is False
    asked = w.call(who, "economics.request_payment_correction", **d["correction_inputs"], idempotency_key="fix-wire-c1-001")
    assert asked["status"] == "ok" and asked["output"]["state"] == "pending" and asked["output"]["executed"] is False, asked
    assert "CANCEL Payment Entry PAY-1" in asked["output"]["summary"] and not w.ledger.writes and "inputs" not in asked["output"]
    assert w.client.get(f"/api/writes/{asked['output']['request_id']}", headers=APPROVER).json()["inputs"] == FIX
    done = w.decide(asked["output"]["request_id"])
    assert done.status_code == 200 and done.json()["state"] == "executed_verified", done.text
    assert w.ledger.payments["PAY-1"]["docstatus"] == 2
    new = w.ledger.payments["PAY-2"]
    assert (new["docstatus"], new["paid_amount"], new["received_amount"], new["amended_from"]) == (1, 3380.0, 3380.0, "PAY-1")
    assert new["references"] == [{"reference_doctype": "Sales Invoice", "reference_name": "S5", "allocated_amount": 3380.0}]
    after = w.call(who, "economics.discrepancies")["output"]
    assert after["discrepancies"] == [] and after["corrections"][0]["reconciled"] is True
    service = w.client.app.state.governed_writes
    granted = next(e for e in service.runtime.store.for_mission(asked["output"]["request_id"]) if e.type == "ApprovalGranted")
    assert (granted.payload["approver"], granted.payload["capability"]) == ("approver:acme", "economics.request_payment_correction")
    status = w.call(who, "economics.request_status", request_id=asked["output"]["request_id"])["output"]
    assert status["state"] == "executed_verified" and status["decided_by"] == "approver:acme"
    again = w.client.post(f"/api/writes/{asked['output']['request_id']}/decision", headers=APPROVER, json={"approved": True, "digest": "0" * 64})
    assert again.status_code == 409 and len(w.ledger.payments) == 2


@pytest.mark.parametrize("change,why", [
    (lambda led: led.rows["Bank Transaction"][0].update(deposit=3400), "figures changed"),
    (lambda led: led.rows["Bank Transaction"][0].update(deposit=3800), "no longer disagree"),
    (lambda led: led.payments["PAY-1"].update(deductions=[{"amount": 1}]), "deductions or taxes"),
    (lambda led: led.payments["PAY-1"]["references"].append({"reference_doctype": "Sales Invoice", "reference_name": "S6", "allocated_amount": 0}), "exactly one sales invoice"),
    (lambda led: led.payments["PAY-1"].update(received_amount=3700), "another currency"),
    (lambda led: led.payments.update(second(led, 3380)), "submitted payments carry this reference"),
    (lambda led: led.rows["Bank Transaction"].append(bank_line("BT-2", 0)), "2 bank lines"),
    (lambda led: led.rows["Bank Transaction"].__setitem__(slice(None), [bank_line("BT-1", 3800), bank_line("BT-2", 0, 420)]), "2 bank lines"),
    (lambda led: led.rows["Bank Transaction"][0].update(docstatus=0), "not submitted"),
    (lambda led: led.rows["Bank Transaction"][0].update(deposit=3800, withdrawal=420), "not a single deposit"),
])
def test_whatever_changed_or_is_not_the_simple_case_is_refused_and_nothing_is_written(world, change, why):
    w = world()
    rid = w.call(w.agent(FULL), "economics.request_payment_correction", **FIX, idempotency_key="fix-wire-c1-002")["output"]["request_id"]
    change(w.ledger)
    out = w.decide(rid).json()
    assert out["state"] == "failed" and why in json.dumps(out) and not w.ledger.writes, out


def test_a_cancelled_payment_whose_replacement_fails_is_never_reported_as_nothing_changed(world):
    w = world()
    rid = w.call(w.agent(FULL), "economics.request_payment_correction", **FIX, idempotency_key="fix-wire-c1-003")["output"]["request_id"]
    w.ledger.fail_on = "frappe.client.submit"
    out = w.decide(rid).json()
    assert out["state"] == "outcome_unknown" and "CHECK NOW" in json.dumps(out) and "nothing was changed" not in json.dumps(out), out
    assert w.ledger.payments["PAY-1"]["docstatus"] == 2


def test_without_the_deployments_switch_there_is_no_correction_to_propose(world):
    w = world(corrections=False)
    assert offered(w, w.agent(FULL)) == ["economics.discrepancies", "economics.evidence", "economics.explain_adjustment"]


@pytest.mark.parametrize("grants,tenant,code", [
    (["economics.read"], "acme", "missing_grant"),                          # no financial grant on the agent
    (["economics.read", "egress:financial"], "nomoney", "missing_grant"),    # none on its tenant
    (["economics.read", "egress:financial"], "noerp", "tenant_not_granted"),
])
def test_economic_information_needs_the_application_and_the_money_grant_on_agent_and_tenant(world, grants, tenant, code):
    w = world()
    who = w.agent(grants, tenant=tenant)
    for tool, args in (("economics.discrepancies", {}), ("economics.explain_adjustment", {"invoice": "S2"}), ("economics.evidence", {"invoice": "S2"})):
        out = w.call(who, tool, **args)
        assert out["status"] == "error" and f"not_authorized ({code})" in out["error"], (tool, out)
        assert not re.search(r"3800|3380|1800|Cedar", json.dumps(out))


def test_an_adjustment_is_explained_with_its_approval_claim_marked_unverified(world):
    w = world()
    out = w.call(w.agent(FULL), "economics.explain_adjustment", invoice="S2")["output"]
    discounts = [e for e in out["events"] if e["kind"] == "discount"]
    claim = discounts[0]["approval_claim"]
    assert (claim["claimed_approval"], claim["claimed_approver"], claim["verified"]) == ("APR-1", "usr-cfo", False) and claim["source"]


def test_evidence_follows_the_source_grants_and_says_only_how_much_it_withheld(world):
    w = world()
    everything = w.call(w.agent(FULL), "economics.evidence", invoice="S2")["output"]
    text = json.dumps(everything)
    assert "10% discount on our order" in text and CRM_WORDS in text and "@acme.example" not in text, text      # addresses stay masked
    assert everything["withheld"]["count"] == 0
    none = w.call(w.agent(["economics.read", "egress:financial"]), "economics.evidence", invoice="S2")["output"]
    assert none["evidence"] == [] and none["links"] == [] and none["withheld"]["count"] == 3
    assert not re.search(r"INTERNAL-ONLY|CRM-NOTE-WORDS|on our order|desk\.test|crm\.test", json.dumps(none))
    assert none["withheld"]["by_scope"] == {"tenant:acme/source:chatwoot": 1, "tenant:acme/source:chatwoot/internal": 1, "tenant:acme/source:twenty": 1}
    crm_only = w.call(w.agent(["economics.read", "egress:financial", "crm.records.read"]), "economics.evidence", invoice="S2")["output"]
    assert CRM_WORDS in json.dumps(crm_only) and "INTERNAL-ONLY" not in json.dumps(crm_only) and "on our order" not in json.dumps(crm_only)


def test_a_source_the_tenant_is_not_granted_is_not_even_read(world, monkeypatch):
    w = world()
    reads = []
    monkeypatch.setattr(Desk, "read_conversations", lambda self, status="all": reads.append("desk") or [])
    monkeypatch.setattr(Crm, "read_all", lambda self, obj: reads.append("crm") or [])
    out = w.call(w.agent(FULL, tenant="deskless"), "economics.evidence", invoice="S2")["output"]
    assert reads == [] and out["withheld"]["count"] == 0 and sorted(out["not_read"]) == [
        "chatwoot: this tenant is not granted it, so it was not read", "twenty: this tenant is not granted it, so it was not read"]


def test_the_tenant_losing_the_money_grant_after_the_proposal_beats_the_approval(world, monkeypatch):
    w = world()
    rid = w.call(w.agent(FULL), "economics.request_payment_correction", **FIX, idempotency_key="fix-wire-c1-004")["output"]["request_id"]
    limits = {**LIMITS, "acme": {"policy_refs": [r for r in LIMITS["acme"]["policy_refs"] if r != "egress:financial"]}}
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(limits))
    refused = w.decide(rid)
    assert refused.status_code == 403 and "missing_grant" in refused.text and not w.ledger.writes, refused.text


def test_the_approval_card_says_what_will_be_cancelled_and_the_exact_figures(world, monkeypatch):
    monkeypatch.setenv("ERPNEXT_PUBLIC_URL", "https://erp.example")
    w = world()
    asked = w.call(w.agent(FULL), "economics.request_payment_correction", **FIX, idempotency_key="fix-wire-c1-005")["output"]
    assert asked["approval_url"] == f"/approvals/writes/{asked['request_id']}"
    signed = TestClient(w.client.app, base_url="http://localhost", follow_redirects=False)
    assert signed.post("/approvals/signin", data={"token": "A" * 40, "next": asked["approval_url"]}, headers={"Origin": "http://localhost"}).status_code == 303
    page = signed.get(asked["approval_url"]).text
    for needle in ("CANCEL Payment Entry PAY-1", "will be CANCELLED", "WIRE-C1", "3800", "3380", "https://erp.example/app/payment-entry/PAY-1"):
        assert needle in page, needle


def test_empty_evidence_says_when_nothing_could_be_tied_to_a_customer(world):
    """A site without customer e-mail or website: names match, identifiers do not, so nothing links and the answer says why."""
    w = world()
    for row in w.ledger.rows["Customer"]:
        del row["email_id"], row["website"]
    out = w.call(w.agent(FULL), "economics.evidence", invoice="S2")["output"]
    assert out["evidence"] == [] and out["withheld"]["count"] == 0
    loose = out["not_tied_to_a_customer"]
    assert loose["count"] == 3 and any("no identifier does" in why for why in loose["why"]), loose
    hidden = w.call(w.agent(["economics.read", "egress:financial"]), "economics.evidence", invoice="S2")["output"]
    assert hidden["not_tied_to_a_customer"]["count"] == 0 and hidden["not_tied_to_a_customer"]["why"] == []     # nothing about unseen items


def test_a_second_payment_under_the_reference_is_never_re_entered_for_the_banks_total(world):
    """The bank's one line is already matched by PAY-1; a second payment of 500 carries the same reference. Re-entering it
    "for the bank's amount" would record 3800 twice."""
    w = world()
    who = w.agent(FULL)
    w.ledger.rows["Bank Transaction"][0]["deposit"] = 3800
    w.ledger.payments.update(second(w.ledger, 500))
    (d,) = w.call(who, "economics.discrepancies")["output"]["discrepancies"]
    assert d["payment"] == "PAY-9" and "correction_inputs" not in d and "proposed_correction" not in d and "2 submitted payments" in d["not_correctable_here"], d
    forced = w.call(who, "economics.request_payment_correction", reference="WIRE-C1", payment_entry="PAY-9", posted_amount=500,
                    bank_amount=3800, idempotency_key="fix-wire-c1-006")["output"]
    out = w.decide(forced["request_id"]).json()
    assert out["state"] == "failed" and "2 submitted payments" in json.dumps(out) and not w.ledger.writes, out
    assert [(n, p["docstatus"]) for n, p in w.ledger.payments.items()] == [("PAY-1", 1), ("PAY-9", 1)]


def test_an_unconfirmed_cancellation_says_to_check_and_enters_nothing(world):
    w = world()
    rid = w.call(w.agent(FULL), "economics.request_payment_correction", **FIX, idempotency_key="fix-wire-c1-007")["output"]["request_id"]
    w.ledger.fail_on = "frappe.client.cancel"
    out = w.decide(rid).json()
    assert out["state"] == "outcome_unknown" and "cancellation of Payment Entry PAY-1 was not confirmed" in json.dumps(out), out
    assert list(w.ledger.payments) == ["PAY-1"]
