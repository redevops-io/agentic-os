"""Economic intelligence for external agents, through the mounted Projects app: investigate, explain and cite evidence within
the caller's source scopes. Read-only. The ERP fake speaks ERPNext's own HTTP routes, so the real client's reads are what
run, and ``economics`` judges."""
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


class Ledger:
    """ERPNext's REST read routes over the economics fixtures. Anything but a read is recorded, so a test can tell that
    nothing was ever written."""

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
        self.writes = []

    def listing(self, doctype):
        if doctype == "Payment Entry":
            return [{k: v for k, v in d.items() if not isinstance(v, list)} for d in self.payments.values()]
        if doctype == "Payment Entry Reference":
            return [{"parent": n, **r} for n, d in self.payments.items() if d["docstatus"] != 0 for r in d["references"]]
        return self.rows.get(doctype, [])

    def handle(self, request):
        path, method = unquote(request.url.path), request.method
        if method != "GET":
            self.writes.append((method, path))
            return httpx.Response(405, json={})
        parts = path.removeprefix("/api/resource/").split("/")
        if method == "GET" and len(parts) == 2 and parts[0] == "Payment Entry":
            doc = self.payments.get(parts[1])
            return httpx.Response(200, json={"data": doc}) if doc else httpx.Response(404, json={})
        if method == "GET" and len(parts) == 1:
            child_of = {"Payment Entry Reference": "Payment Entry", "Sales Invoice Item": "Sales Invoice", "Timesheet Detail": "Timesheet"}
            if parts[0] in child_of and request.url.params.get("parent") != child_of[parts[0]]:
                return httpx.Response(403, json={})              # as ERPNext answers a user who names no parent
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
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB"):
        monkeypatch.delenv(k, raising=False)

    def build():
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
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
        count = iter(range(100))

        def agent(grants, tenant="acme"):
            return {"Authorization": "Bearer " + AT.issue(agents, f"a{next(count)}", grants, minutes=10, tenant=tenant, authority_db=db)}

        def call(who, tool, /, **args):
            return client.post(f"{BASE}/{tool}/invoke", headers=who, json=args).json()

        return type("W", (), {"client": client, "ledger": ledger, "agent": staticmethod(agent), "call": staticmethod(call)})

    return build


FULL = ["economics.read", "egress:financial", "support.read", "crm.records.read"]


def offered(w, who):
    return sorted(c["name"] for c in w.client.get(BASE, headers=who).json()["capabilities"] if c["name"].startswith("economics."))


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
        said = json.dumps({k: v for k, v in out.items() if k != "request_id"})    # a random id can hold any digits
        assert not re.search(r"3800|3380|1800|Cedar", said)


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


def test_it_is_read_only_and_a_difference_is_never_presented_as_something_to_correct(world):
    """A bank difference alone authorizes nothing: no tool proposes a change, and nothing in the answer says to make one."""
    w = world()
    who = w.agent(FULL)
    assert offered(w, who) == ["economics.bank_reconciliation", "economics.discrepancies", "economics.evidence", "economics.explain_adjustment", "economics.findings"]
    out = w.call(who, "economics.discrepancies")
    assert out["status"] == "ok", out
    (d,) = out["output"]["discrepancies"]
    assert (d["reference"], d["posted"], d["bank"], d["invoices"], d["ambiguous"]) == ("WIRE-C1", "3800.00", "3380.00", ["S5"], "")
    text = json.dumps(out["output"]).lower()
    assert not re.search(r"cancel payment|record it again|correction_inputs|proposed_correction", text), text
    assert "not by itself an error" in out["output"]["note"]
    w.call(who, "economics.explain_adjustment", invoice="S5"), w.call(who, "economics.evidence", invoice="S2")
    assert w.ledger.writes == []


@pytest.mark.parametrize("change,why", [
    (lambda led: led.payments.update(second(led, 500)), "2 payments are submitted under this reference"),
    (lambda led: led.rows["Bank Transaction"].append(bank_line("BT-2", 200)), "2 bank lines carry this reference"),
    (lambda led: led.rows["Bank Transaction"][0].update(deposit=3800, withdrawal=420), "is a withdrawal"),
])
def test_a_difference_that_compares_a_payment_with_a_total_is_marked_ambiguous(world, change, why):
    w = world()
    change(w.ledger)
    rows = w.call(w.agent(FULL), "economics.discrepancies")["output"]["discrepancies"]
    assert rows and all(why in r["ambiguous"] for r in rows), rows



def test_findings_name_what_they_did_not_evaluate_and_show_message_text_only_within_the_source_grants(world):
    w = world()
    plain = w.call(w.agent(["economics.read", "egress:financial"]), "economics.findings")
    assert plain["status"] == "ok", plain
    out = plain["output"]
    kinds = {f["kind"] for f in out["findings"]}
    assert "adjustment_without_verified_authorization" in kinds, out["findings"]          # a comment saying "approved" is a claim, never a verification
    assert all(f["recorded"]["kind"] in ("discount", "return") and f["recorded"]["on"] for f in out["findings"] if f["subject"].endswith((":discount", ":sale")))
    assert set(out["not_evaluated"]) == {"recorded_without_rationale", "commitment_without_record", "decision_not_executed"}
    assert out["withheld"]["count"] > 0 and all(not f["evidence"] for f in out["findings"])
    assert SECRET_NOTE not in json.dumps(out) and CRM_WORDS not in json.dumps(out)
    full = w.call(w.agent(FULL), "economics.findings", customer="Acme")["output"]
    assert full["customer"] == "Acme" and all(f["customer"] == "Acme" for f in full["findings"])
    assert w.call(w.agent(["economics.read"]), "economics.findings")["status"] == "error"     # no financial grant: nothing read
    assert not w.ledger.writes


def test_bank_reconciliation_compares_the_operators_statement_files_and_says_when_there_are_none(world, monkeypatch, tmp_path):
    w = world()
    who = w.agent(FULL)
    none = w.call(who, "economics.bank_reconciliation")["output"]
    assert none["reconciled"] is False and none["findings"] == [] and "nothing was compared" in none["note"]          # no statement is not "all matched"
    folder = tmp_path / "statements" / "acme"
    folder.mkdir(parents=True)
    (folder / "oct.csv").write_text("date,amount,reference,description\n2026-09-03,3380,WIRE-C1,incoming\n2026-09-12,1,OTHER,unrelated\n")
    monkeypatch.setenv("AGENTIC_OS_BANK_STATEMENTS_DIR", str(tmp_path / "statements"))
    out = w.call(who, "economics.bank_reconciliation")["output"]
    assert out["reconciled"] is True and out["statements"] == ["oct.csv"] and out["counts"]["amount_mismatch"] == 1
    (f,) = [x for x in out["findings"] if x["kind"] == "amount_mismatch"]
    assert (f["reference"], f["booked"], f["banked"]) == ("WIRE-C1", "3800.00", "3380.00") and "not by itself an error" in out["note"]
    assert w.call(w.agent(["economics.read"]), "economics.bank_reconciliation")["status"] == "error"          # no financial grant: nothing read
    assert not w.ledger.writes
