"""The governed payment correction, through the mounted Projects app: when a bank difference may be proposed at all, what
the approver is shown and attests to, what happens to the vouchers and the ledger, and how a half-done correction is
finished. The ERPNext fake speaks ERPNext's own HTTP routes (cancel, draft, submit, comments, general ledger), so the
real writer is what runs and the application's records are what is judged."""
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
from agentic_os.governed_writes import payment_correction as PC  # noqa: E402
from agentic_os.integrations import chatwoot as chatwoot_mod  # noqa: E402
from agentic_os.integrations import twenty as twenty_mod  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests.test_agent_gateway_economics import Crm, Desk, Ledger  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

BASE = "/api/agent-gateway/capabilities"
APPROVER = {"Authorization": "Bearer " + "A" * 40}
PROPOSE = "economics.request_payment_correction"
LIMITS = {"acme": {"policy_refs": ["app:erpnext", "app:chatwoot", "app:twenty", "writes:decide", "egress:financial"]},
          "nomoney": {"policy_refs": ["app:erpnext", "writes:decide"]}}
FULL = ["economics.read", "economics.propose", "egress:financial", "support.read", "crm.records.read"]
FIX = {"reference": "WIRE-C1", "payment_entry": "PAY-1", "posted_amount": 3800, "bank_amount": 3380}
#: the bank's own export: starts before the payment, shows one smaller deposit, and runs well past it
GOOD = ["2026-08-31,100,OTHER-0,incoming", "2026-09-01,3380,WIRE-C1,incoming wire", "2026-09-12,50,OTHER-1,incoming"]


def csv(lines, header="date,amount,reference,description"):
    return "\n".join([header, *lines]) + "\n"


class Books(Ledger):
    """ERPNext with payment entries that can be cancelled and amended, a general ledger that follows them, and an invoice
    whose outstanding amount follows the payments allocated to it. ``fail`` = {step: (status, applied_first)}."""

    def __init__(self):
        super().__init__()
        pay = self.payments["PAY-1"]
        pay.update(paid_from_account_currency="USD", paid_to_account_currency="USD", source_exchange_rate=1.0, target_exchange_rate=1.0,
                   unallocated_amount=0, references=[{"reference_doctype": "Sales Invoice", "reference_name": "INV-9", "allocated_amount": 3800}])
        self.invoice_total, self.comments_made, self.fail, self.gl_extra = 3800, [], {}, []

    def outstanding(self):
        return self.invoice_total - sum(r["allocated_amount"] for d in self.payments.values() if d["docstatus"] == 1
                                        for r in d["references"] if r["reference_name"] == "INV-9")

    def ledger_rows(self):
        out = []
        for name, d in self.payments.items():
            if d["docstatus"] == 0:
                continue
            for account, debit, credit in (("Bank", d["received_amount"], 0), ("Debtors", 0, d["paid_amount"])):
                out.append({"voucher_no": name, "account": account, "debit": debit, "credit": credit, "is_cancelled": int(d["docstatus"] == 2)})
        return out + self.gl_extra

    def listing(self, doctype):
        return self.ledger_rows() if doctype == "GL Entry" else super().listing(doctype)

    def _step(self, step, apply):
        status, applied_first = self.fail.get(step, (0, False))
        if not status or applied_first:
            result = apply()
        return httpx.Response(status, json={}) if status else result

    def handle(self, request):
        path, method = unquote(request.url.path), request.method
        body = json.loads(request.content) if request.content else {}
        if method == "GET":
            if path == "/api/method/frappe.auth.get_logged_user":
                return httpx.Response(200, json={"message": "corrections-api@co.example"})
            if path == "/api/resource/Sales Invoice/INV-9":
                return httpx.Response(200, json={"data": {"name": "INV-9", "docstatus": 1, "outstanding_amount": self.outstanding()}})
            return super().handle(request)
        self.writes.append((method, path))
        ok = httpx.Response(200, json={"message": {}})
        if path == "/api/method/frappe.client.cancel":
            return self._step("cancel", lambda: self.payments[body["name"]].update(docstatus=2) or ok)
        if path == "/api/method/frappe.client.submit":
            return self._step("submit", lambda: self.payments[json.loads(body["doc"])["name"]].update(docstatus=1) or ok)
        if path == "/api/resource/Payment Entry":
            def make():
                name = f"{body['amended_from']}-1"
                assert name not in self.payments, "a second amendment of the same voucher"
                self.payments[name] = {"deductions": [], "taxes": [], "unallocated_amount": 0, **body, "name": name}
                return httpx.Response(200, json={"data": self.payments[name]})
            return self._step("create", make)
        if path == "/api/method/frappe.desk.form.utils.add_comment":     # (a direct Comment insert needs more rights)
            def note():
                assert body["comment_by"] == body["comment_email"] == "corrections-api@co.example"
                self.comments_made.append((body["reference_name"], body["content"]))
                return httpx.Response(200, json={"message": {"name": f"C-{len(self.comments_made)}"}})
            return self._step("comment", note)
        return httpx.Response(405, json={})


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB", PC.ENV_ENABLED, PC.ENV_STATEMENTS):
        monkeypatch.delenv(k, raising=False)
    statements = tmp_path / "statements"
    (statements / "acme").mkdir(parents=True)

    def build(lines=GOOD, *, enabled=True, header="date,amount,reference,description"):
        books = build.books if getattr(build, "books", None) else Books()
        build.books = books
        monkeypatch.setattr(apps_mod, "writers_from_env", lambda: {"erpnext": books.writer()})
        monkeypatch.setattr(chatwoot_mod, "chatwoot_from_env", Desk)
        monkeypatch.setattr(twenty_mod, "twenty_from_env", Crm)
        bank_file = statements / "acme" / "bank.csv"
        if lines is None:
            bank_file.unlink(missing_ok=True)
        else:
            bank_file.write_text(csv(lines, header))
        agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
        ui = tmp_path / "ui"
        ui.mkdir(exist_ok=True)
        (ui / "index.html").write_text("<html></html>")
        env = {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
               "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
               "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
               "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40, "PROJECTS_UI_DIR": str(ui)}
        if enabled:
            env.update({PC.ENV_ENABLED: "1", PC.ENV_STATEMENTS: str(statements)})
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
        count = iter(range(100, 200))

        def agent(grants=FULL, tenant="acme"):
            return {"Authorization": "Bearer " + AT.issue(agents, f"a{next(count) + len(books.writes)}-{tenant}-{len(grants)}", grants, minutes=10,
                                                          tenant=tenant, authority_db=db)}

        who = agent()
        keys = iter(range(1000))

        def call(tool, /, as_=None, **args):
            return client.post(f"{BASE}/{tool}/invoke", headers=as_ or who, json=args).json()

        def propose(key=None, **changed):
            return call(PROPOSE, **{**FIX, **changed}, idempotency_key=key or f"fix-{id(client)}-{next(keys):04d}")

        def card(asked):
            assert asked["status"] == "ok", asked
            return client.get(f"/api/writes/{asked['output']['request_id']}", headers=APPROVER).json()

        def decide(asked, approved=True, attested=True):
            return client.post(f"/api/writes/{asked['output']['request_id']}/decision", headers=APPROVER,
                               json={"approved": approved, "digest": card(asked)["digest"], **({"attested": True} if attested else {})})

        return type("W", (), {"client": client, "books": books, "agent": staticmethod(agent), "call": staticmethod(call),
                              "propose": staticmethod(propose), "card": staticmethod(card), "decide": staticmethod(decide),
                              "bank_file": bank_file, "who": who})

    return build


def journal(w):
    return w.client.app.state.governed_writes.context("acme").journal.get("acme", "PAY-1")


def amendments(w):
    return [n for n, d in w.books.payments.items() if d.get("amended_from")]


# ── a difference alone changes nothing ───────────────────────────────────────────
def test_without_the_banks_own_statement_a_difference_cannot_even_be_proposed(world):
    w = world(lines=None)
    out = w.propose()
    assert out["status"] == "error" and "a bank difference alone changes nothing" in out["error"], out
    assert not w.books.writes and w.client.get("/api/writes", headers=APPROVER).json()["requests"] == []
    seen = w.call("economics.discrepancies")["output"]["discrepancies"][0]["correction"]
    assert seen["may_be_proposed"] is False and "inputs" not in seen


@pytest.mark.parametrize("name,lines,why", [
    ("fee on the statement closes the gap", [*GOOD, "2026-09-01,-420,WIRE-C1-FEE,Bank wire fee"], "matched_with_fee|fee is supported"),
    ("a fee line naming the payment that does not close it", [*GOOD, "2026-09-02,-15,WIRE-C1-FEE,Bank wire fee"], "also names this payment"),
    ("an unlinked line of exactly the difference", [*GOOD, "2026-09-02,-420,ADJ-77,adjustment"], "exactly the difference"),
    ("the rest arrived under another reference", [*GOOD, "2026-09-09,420,WIRE-C1B-X,second part"], "exactly the difference|also names"),
    ("an unlinked bank charge beside the deposit", [*GOOD, "2026-09-03,-25,,wire charges"], "bank charge no larger"),
    ("a second instalment", [GOOD[0], GOOD[1], "2026-09-08,200,WIRE-C1,second", GOOD[2]], "ambiguous|partial_payment|partial_settlement"),
    ("the statement does not run far enough for the rest to arrive", GOOD[:2], "it must reach 2026-09-06"),
    ("the statement starts after the payment", ["2026-09-02,100,OTHER-0,incoming", "2026-09-02,3380,WIRE-C1,incoming wire", GOOD[2]], "starts 2026-09-02"),
    ("the money was returned", [*GOOD, "2026-09-10,-3380,RTN-WIRE-C1,returned"], "reversal"),
    ("the bank's export shows another figure than the application's bank line", [GOOD[0], "2026-09-01,3400,WIRE-C1,incoming wire", GOOD[2]], "does not independently show"),
    ("the payment is not on the statement", [GOOD[0], GOOD[2]], "does not independently show"),
    ("a row that cannot be read", [*GOOD, "yesterday,oops,WIRE-C1,?"], "could not be read completely"),
])
def test_anything_that_could_explain_the_difference_stops_the_proposal(world, name, lines, why):
    w = world(lines=lines)
    out = w.propose()
    assert out["status"] == "error" and re.search(why, out["error"]), (name, out)
    assert not w.books.writes and not journal(w), name


def test_another_currency_on_the_statement_is_never_converted(world):
    w = world(lines=[f"{l},{'EUR' if 'WIRE-C1' in l else 'USD'}" for l in GOOD], header="date,amount,reference,description,currency")
    out = w.propose()
    assert out["status"] == "error" and "fx is supported" in out["error"] and not w.books.writes, out


@pytest.mark.parametrize("change,why", [
    (lambda p: p.update(unallocated_amount=20), "no invoice was allocated|allocation is supported"),
    (lambda p: p["references"][0].update(allocated_amount=3000), "not allocated in full"),
    (lambda p: p["references"].append({"reference_doctype": "Sales Invoice", "reference_name": "S6", "allocated_amount": 0}), "exactly one sales invoice"),
    (lambda p: p.update(source_exchange_rate=1.1), "currency conversion"),
    (lambda p: p.update(paid_from_account_currency="EUR"), "currency conversion"),
    (lambda p: p.update(deductions=[{"amount": 1}]), "deductions or taxes"),
    (lambda p: p.update(received_amount=3700), "another currency, or charges"),
])
def test_a_payment_that_is_not_the_simple_case_is_left_to_a_person(world, change, why):
    w = world()
    change(w.books.payments["PAY-1"])
    out = w.propose()
    assert out["status"] == "error" and re.search(why, out["error"]) and not w.books.writes, out


def test_two_payments_under_one_reference_are_never_corrected(world):
    w = world()
    w.books.payments["PAY-9"] = {**w.books.payments["PAY-1"], "name": "PAY-9", "paid_amount": 100, "received_amount": 100}
    out = w.propose()
    assert out["status"] == "error" and "cannot be told" in out["error"] and not w.books.writes, out


# ── who may ask, who may decide ──────────────────────────────────────────────────
def test_it_is_off_unless_the_deployment_turns_it_on(world):
    w = world(enabled=False)
    names = [c["name"] for c in w.client.get(BASE, headers=w.who).json()["capabilities"]]
    assert PROPOSE not in names and "economics.request_status" not in names and "economics.discrepancies" in names
    assert "correction" not in w.call("economics.discrepancies")["output"]["discrepancies"][0]
    assert w.propose()["status"] != "ok" and not w.books.writes


def test_proposing_needs_its_own_grant_and_the_money_grant_on_agent_and_tenant(world):
    w = world()
    for grants, tenant in ((["economics.read", "egress:financial"], "acme"), (["economics.propose"], "acme"), (FULL, "nomoney")):
        who = w.agent(grants, tenant=tenant)
        out = w.call(PROPOSE, as_=who, **FIX, idempotency_key=f"nope-{tenant}-{len(grants)}-000")
        assert out["status"] != "ok", (grants, tenant, out)
        assert not w.books.writes
    reader = w.agent(["economics.read", "egress:financial"])
    assert "correction" not in w.call("economics.discrepancies", as_=reader)["output"]["discrepancies"][0]


def test_the_approver_sees_the_evidence_must_attest_and_the_agent_sees_neither(world):
    w = world()
    judged = w.call("economics.discrepancies")["output"]["discrepancies"][0]["correction"]
    assert judged == {"may_be_proposed": True, "inputs": FIX}
    asked = w.propose()
    assert asked["status"] == "ok" and asked["output"]["state"] == "pending" and not w.books.writes, asked
    assert "evidence" not in asked["output"] and "sha256" not in json.dumps(asked)
    shown = w.card(asked)
    ev = shown["evidence"]
    assert ev["mode"] == "correct" and ev["attestation"] == PC.ATTEST and ev["deposit"]["amount"] == "3380.00"
    assert {c["status"] for c in ev["classification"]["causes"]} == {"excluded"} and re.fullmatch(r"[0-9a-f]{64}", ev["statement_sha256"]["bank.csv"])
    refused = w.decide(asked, attested=False)
    assert refused.status_code == 403 and "needs your attestation" in refused.text and not w.books.writes
    assert w.card(asked)["state"] == "pending"
    done = w.decide(asked)
    assert done.status_code == 200 and done.json()["state"] == "executed_verified", done.text
    assert w.client.get(f"/api/writes/{asked['output']['request_id']}", headers=APPROVER).json()["attested"] == PC.ATTEST
    status = w.call("economics.request_status", request_id=asked["output"]["request_id"])["output"]
    assert status["state"] == "executed_verified" and "evidence" not in status and status["decided_by"] == "approver:acme"


def test_the_signed_in_page_needs_the_box_ticked_to_approve_and_not_to_reject(world):
    w = world()
    asked = w.propose()
    rid = asked["output"]["request_id"]
    signed = w.client.post("/approvals/signin", data={"token": "A" * 40}, follow_redirects=False)
    assert signed.status_code == 303
    page = w.client.get(f"/approvals/writes/{rid}").text
    assert 'name="attested"' in page and "I checked the bank statement lines shown" in page and "Bank statement shows" in page
    field = lambda name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)  # noqa: E731
    form = {"csrf": field("csrf"), "digest": field("digest"), "decision": "approve"}
    no = w.client.post(f"/approvals/writes/{rid}/decision", data=form)
    assert no.status_code == 403 and "needs your attestation" in no.text and not w.books.writes
    done = w.client.post(f"/approvals/writes/{rid}/decision", data={**form, "attested": "yes"})
    assert done.status_code == 200 and "Done and verified" in done.text and "The approver attested" in done.text


def test_rejecting_changes_nothing_and_needs_no_attestation(world):
    w = world()
    asked = w.propose()
    no = w.decide(asked, approved=False, attested=False)
    assert no.status_code == 200 and no.json()["state"] == "rejected" and not w.books.writes and not journal(w)


# ── what happens to the books ────────────────────────────────────────────────────
def test_the_original_is_kept_the_replacement_is_its_amendment_and_the_ledger_agrees(world):
    w = world()
    asked = w.propose(key="correct-once-0001")
    done = w.decide(asked).json()
    assert done["state"] == "executed_verified" and done["record"] == "PAY-1-1", done
    old, new = w.books.payments["PAY-1"], w.books.payments["PAY-1-1"]
    assert (old["docstatus"], old["paid_amount"]) == (2, 3800)                       # kept as it was, cancelled
    assert (new["docstatus"], new["paid_amount"], new["received_amount"], new["amended_from"], new["posting_date"], new["reference_no"]) == (
        1, 3380.0, 3380.0, "PAY-1", "2026-09-01", "WIRE-C1")
    assert new["references"] == [{"reference_doctype": "Sales Invoice", "reference_name": "INV-9", "allocated_amount": 3380.0}]
    assert w.books.outstanding() == 420
    assert [m for m, _ in w.books.writes] == ["POST"] * 5
    assert [n for n, _ in w.books.comments_made] == ["PAY-1", "PAY-1-1"]
    assert all(asked["output"]["request_id"] in c and "approver:acme" in c and "Evidence sha256" in c for _, c in w.books.comments_made)
    kept = journal(w)
    assert kept["state"] == "done" and kept["snapshot"]["paid_amount"] == 3800 and kept["approver"] == "approver:acme"
    assert kept["attestation"] == PC.ATTEST and kept["evidence"]["statement_sha256"] and kept["replacement"] == "PAY-1-1"
    assert [h["state"] for h in kept["history"]] == ["planned", "cancel_sent", "cancelled", "create_sent", "draft", "submitted", "done"]
    # idempotent: the same key is the same request; a second proposal of a corrected payment is refused
    assert w.propose(key="correct-once-0001")["output"]["request_id"] == asked["output"]["request_id"]
    again = w.propose()
    assert again["status"] == "error" and "already corrected: see PAY-1-1" in again["error"] and len(w.books.writes) == 5


def test_two_approvals_of_the_same_correction_write_it_once(world):
    w = world()
    first, second = w.propose(), w.propose()
    assert w.decide(first).json()["state"] == "executed_verified"
    two = w.decide(second).json()
    assert two["state"] == "no_change" and two["record"] == "PAY-1-1" and amendments(w) == ["PAY-1-1"] and len(w.books.writes) == 5, two


@pytest.mark.parametrize("change", [
    lambda w: w.bank_file.write_text(csv([*GOOD, "2026-09-20,10,LATER,incoming"])),                  # the export was replaced
    lambda w: w.bank_file.write_text(csv([*GOOD, "2026-09-02,-420,ADJ,adjustment"])),                # and now explains it
    lambda w: w.bank_file.unlink(),
    lambda w: w.books.rows["Bank Transaction"][0].update(deposit=3400),
    lambda w: w.books.payments["PAY-1"].update(deductions=[{"amount": 1}]),
])
def test_what_changed_after_the_proposal_stops_the_write(world, change):
    w = world()
    asked = w.propose()
    change(w)
    out = w.decide(asked).json()
    assert out["state"] == "failed" and "nothing was changed" in out["note"] and not w.books.writes, out


def test_a_ledger_that_does_not_agree_afterwards_is_not_called_verified(world):
    w = world()
    w.books.gl_extra = [{"voucher_no": "PAY-1-1", "account": "Round Off", "debit": 1, "credit": 0, "is_cancelled": 0}]
    out = w.decide(w.propose()).json()
    assert out["state"] == "executed_unverified" and "do not balance" in out["note"] + out["read_back"], out
    assert journal(w)["state"] == "submitted"


def test_a_comment_that_cannot_be_added_is_said_not_hidden(world):
    w = world()
    w.books.fail = {"comment": (500, False)}
    out = w.decide(w.propose()).json()
    assert out["state"] == "executed_verified" and "could not be added" in out["note"] + out["read_back"], out


# ── partial execution and recovery ───────────────────────────────────────────────
@pytest.mark.parametrize("fail,left_behind", [
    ({"create": (417, False)}, []),                    # ERPNext refuses the replacement after the cancellation
    ({"create": (500, False)}, []),                    # the replacement's answer is lost, nothing was made
    ({"create": (500, True)}, ["PAY-1-1"]),            # the answer is lost, the draft WAS made
    ({"submit": (417, False)}, ["PAY-1-1"]),           # the draft is made and cannot be submitted
    ({"submit": (500, True)}, ["PAY-1-1"]),            # submitted, the answer lost
])
def test_a_half_done_correction_says_so_is_never_retried_and_is_finished_only_after_a_new_approval_without_a_duplicate(world, fail, left_behind):
    w = world()
    w.books.fail = dict(fail)
    asked = w.propose()
    out = w.decide(asked).json()
    assert out["state"] == "outcome_unknown" and "CHECK NOW" in out["note"] and "PAY-1 is cancelled" in out["note"], out
    assert w.books.payments["PAY-1"]["docstatus"] == 2 and amendments(w) == left_behind
    assert journal(w)["state"] in PC.UNFINISHED
    writes = len(w.books.writes)
    spent = w.client.post(f"/api/writes/{asked['output']['request_id']}/decision", headers=APPROVER,
                          json={"approved": True, "digest": "0" * 64, "attested": True})
    assert spent.status_code == 409 and len(w.books.writes) == writes                     # the same approval is spent
    w.books.fail = {}
    w2 = world()                                                                          # a restart: the journal is on disk
    finish = w2.propose()
    assert finish["status"] == "ok" and finish["output"]["summary"].startswith("FINISH correcting PAY-1"), finish
    assert len(w2.books.writes) == writes                                                 # proposing wrote nothing
    shown = w2.card(finish)
    assert shown["evidence"]["mode"] == "finish" and shown["evidence"]["attestation"] == PC.ATTEST_FINISH
    assert shown["evidence"]["original_approval"] == {"request_id": asked["output"]["request_id"], "approver": "approver:acme"}
    assert w2.decide(finish, attested=False).status_code == 403
    done = w2.decide(finish).json()
    assert done["state"] == "executed_verified", done
    assert amendments(w2) == ["PAY-1-1"] and w2.books.payments["PAY-1-1"]["docstatus"] == 1 and w2.books.outstanding() == 420
    kept = journal(w2)
    assert kept["state"] == "done" and kept["request_id"] == asked["output"]["request_id"]
    assert kept["finish_approval"] == {"request_id": finish["output"]["request_id"], "approver": "approver:acme"}


def test_finishing_stops_when_someone_has_recorded_the_money_by_other_means(world):
    w = world()
    w.books.fail = {"create": (417, False)}
    w.decide(w.propose())
    w.books.fail = {}
    finish = w.propose()
    w.books.payments["PAY-7"] = {**w.books.payments["PAY-1"], "name": "PAY-7", "docstatus": 1, "paid_amount": 3380, "received_amount": 3380}
    writes = len(w.books.writes)
    out = w.decide(finish).json()
    assert out["state"] == "outcome_unknown" and "recorded under the same reference by other means" in out["note"], out
    assert len(w.books.writes) == writes and not amendments(w)


def test_finishing_stops_at_an_amendment_that_differs_from_what_was_approved(world):
    w = world()
    w.books.fail = {"submit": (417, False)}
    w.decide(w.propose())
    w.books.fail = {}
    w.books.payments["PAY-1-1"].update(paid_amount=3000, received_amount=3000)
    writes = len(w.books.writes)
    out = w.decide(w.propose()).json()
    assert out["state"] == "outcome_unknown" and "differs from what was approved" in out["note"], out
    assert len(w.books.writes) == writes and w.books.payments["PAY-1-1"]["docstatus"] == 0


def test_a_refused_cancellation_changes_nothing_and_can_be_proposed_again(world):
    w = world()
    w.books.fail = {"cancel": (417, False)}
    out = w.decide(w.propose()).json()
    assert out["state"] == "failed" and w.books.payments["PAY-1"]["docstatus"] == 1 and not amendments(w), out
    assert journal(w)["state"] == "abandoned"
    w.books.fail = {}
    assert w.decide(w.propose()).json()["state"] == "executed_verified"


@pytest.mark.parametrize("cancelled", [True, False])
def test_an_unconfirmed_cancellation_enters_nothing_and_the_next_proposal_follows_what_the_books_show(world, cancelled):
    w = world()
    w.books.fail = {"cancel": (500, cancelled)}
    out = w.decide(w.propose()).json()
    assert out["state"] == "outcome_unknown" and "was not confirmed" in out["note"] and not amendments(w), out
    w.books.fail = {}
    again = w.propose()
    assert w.card(again)["evidence"]["mode"] == ("finish" if cancelled else "correct")
    assert w.decide(again).json()["state"] == "executed_verified" and amendments(w) == ["PAY-1-1"]


def test_a_finish_approval_never_cancels_anything(world):
    w = world()
    w.books.fail = {"create": (417, False)}
    w.decide(w.propose())
    w.books.fail = {}
    finish = w.propose()
    w.books.payments["PAY-1"]["docstatus"] = 1                       # someone restored the original meanwhile
    writes = len(w.books.writes)
    out = w.decide(finish).json()
    assert out["state"] == "failed" and len(w.books.writes) == writes and w.books.payments["PAY-1"]["docstatus"] == 1, out


@pytest.mark.parametrize("left", [["app:erpnext", "writes:decide"], ["writes:decide", "egress:financial"]])
def test_an_approval_never_overrides_a_grant_the_tenant_has_lost_since(world, monkeypatch, left):
    w = world()
    asked = w.propose()
    shown = w.card(asked)
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": left}}))
    no = w.client.post(f"/api/writes/{asked['output']['request_id']}/decision", headers=APPROVER,
                       json={"approved": True, "digest": shown["digest"], "attested": True})
    assert no.status_code == 403 and "not_authorized" in no.text and not w.books.writes, no.text
