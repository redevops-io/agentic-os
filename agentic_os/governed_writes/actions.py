"""What an agent may propose, what the approver is told, and how each write is performed and read back.

Deliberately narrow: notes, comments, drafts and add-if-absent records. Nothing here sends a message to a customer,
submits or cancels an accounting document, pays, deletes or overwrites. A ``perform`` never raises once the application
has accepted a write: a failed read-back makes the result unverified, never "not applied".
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping

from agentic_os.governed_writes.apps import ERP_DOCTYPES, NotApplied, OutcomeUnknown

#: app -> the named grant that lets an agent propose writes to it, and the words a person consents to
APP_GRANTS: Dict[str, str] = {"erpnext": "erp.propose", "chatwoot": "support.propose", "twenty": "crm.propose"}


@dataclass(frozen=True)
class Outcome:
    executed: bool            # this request changed the application
    verified: bool            # it was read back and shows the expected result
    record: str = ""
    read_back: str = ""
    note: str = ""            # why nothing was changed, or what needs a look


@dataclass(frozen=True)
class WriteAction:
    name: str
    app: str
    description: str
    input_schema: Mapping[str, Any]
    preview: Callable[[Mapping[str, Any]], str]                    # one line, without the written text
    perform: Callable[[Any, Mapping[str, Any], str, str], Outcome]  # (writer, inputs, footer, request_id)


#: text that other readers of these applications treat as a record of an approval. An agent writes the text of a proposal,
#: so it may not write these: otherwise an approved "comment" could pose as somebody's approval of something else.
RESERVED_MARKERS = ("aos-approval",)


def _skeleton(text: str) -> str:
    """The text as a lenient reader would see it: compatibility forms folded, invisible characters gone, any dash a dash."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join("-" if unicodedata.category(c) == "Pd" or c == "\u2212" else c for c in folded if unicodedata.category(c) != "Cf")


def reserved_marker(inputs: Any) -> str:
    """The reserved marker found anywhere in the proposed values, or ""."""
    if isinstance(inputs, str):
        seen = _skeleton(inputs)
        return next((m for m in RESERVED_MARKERS if m in seen), "")
    values = inputs.values() if isinstance(inputs, Mapping) else inputs if isinstance(inputs, (list, tuple)) else ()
    return next((found for found in map(reserved_marker, values) if found), "")


def _schema(required: List[str], **properties: Any) -> Dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}


def _text(maximum: int, **extra: Any) -> Dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, **extra}


def _read_back(check: Callable[[], "tuple[bool, str]"], record: str) -> Outcome:
    """The result of a write the application accepted: read it back, whatever happens while doing so."""
    try:
        ok, detail = check()
    except Exception as e:  # noqa: BLE001 - the write happened; only our look at it failed
        reason = str(e) if isinstance(e, (NotApplied, OutcomeUnknown)) else type(e).__name__
        return Outcome(True, False, record, f"written, but it could not be read back: {reason}")
    return Outcome(True, ok, record, detail)


# ── ERPNext ──────────────────────────────────────────────────────────────────────
def _comment(w: Any, i: Mapping[str, Any], footer: str, rid: str) -> Outcome:
    doctype, name = i["doctype"], i["name"]
    w.document(doctype, name)                                          # the document exists, before anything is written
    mine = [c for c in w.comments(doctype, name) if rid in str(c.get("content", ""))]
    if mine:
        return Outcome(False, True, str(mine[0].get("name", "")), "this request's comment is already there", "already written by this request")
    made = w.add_comment(doctype, name, i["text"], footer)

    def check():
        seen = any(str(c.get("name")) == made and rid in str(c.get("content", "")) for c in w.comments(doctype, name))
        return seen, "the comment is on the document" if seen else "the comment was not found on the document"
    return _read_back(check, made)


def _draft_po(w: Any, i: Mapping[str, Any], footer: str, rid: str) -> Outcome:
    w.document("Supplier", i["supplier"])
    for line in i["items"]:
        w.document("Item", line["item_code"])
    made = w.create_draft_purchase_order(supplier=i["supplier"], schedule_date=i["schedule_date"], items=list(i["items"]),
                                         company=i.get("company", ""))
    try:                                                               # who asked and who approved, on the document itself
        w.add_comment("Purchase Order", made, "Draft created through a governed request.", footer)
    except Exception:  # noqa: BLE001 - the draft exists either way; the ledger holds the same record
        pass

    def check():
        doc = w.document("Purchase Order", made)
        want = sorted((l["item_code"], float(l["qty"])) for l in i["items"])
        got = sorted((str(l.get("item_code")), float(l.get("qty") or 0)) for l in doc.get("items") or [])
        wrong = [what for what, bad in (("it is not a draft", doc.get("docstatus") != 0), ("the supplier differs", doc.get("supplier") != i["supplier"]),
                                        ("the lines differ", got != want)) if bad]
        return not wrong, f"{made} is a draft for {i['supplier']} with the approved lines" if not wrong else f"{made}: " + ", ".join(wrong)
    return _read_back(check, made)


# ── Chatwoot ─────────────────────────────────────────────────────────────────────
def _private_note(w: Any, i: Mapping[str, Any], footer: str, rid: str) -> Outcome:
    cid = i["conversation_id"]
    w.conversation(cid)
    content = f"{i['text']}\n\n— {footer}"
    mine = [m for m in w.messages(cid) if rid in str(m.get("content", ""))]
    if mine:
        return Outcome(False, True, str(mine[0].get("id", "")), "this request's note is already there", "already written by this request")
    made = w.add_private_note(cid, content)

    def check():
        found = next((m for m in w.messages(cid) if m.get("id") == made), None)
        if found is None:
            return False, "the note was not found in the conversation"
        if found.get("private") is not True:
            return False, "CHECK THE CONVERSATION NOW: the message is there but is not marked private"
        return True, "the note is in the conversation and is private"
    return _read_back(check, str(made))


# ── Twenty ───────────────────────────────────────────────────────────────────────
_SHADOW = Outcome(False, False, "", "", "shadow mode (TWENTY_WRITE_SHADOW_MODE): previewed only, nothing was written")


def _company(w: Any, i: Mapping[str, Any], footer: str, rid: str) -> Outcome:
    from agentic_os.integrations.twenty_write import normalize_domain  # noqa: PLC0415
    host = normalize_domain(i["domain"])
    if not host:
        raise NotApplied("the domain is not a host name")
    existing = w.companies_by_domain(host)
    if existing:
        return Outcome(False, True, str(existing[0].get("id", "")), "a company with this domain is already recorded",
                       "already existed; nothing was changed")
    if w.shadow:
        return _SHADOW
    made = w.create_company(i["name"], host)

    def check():
        got = w.record("companies", "company", made)
        ok = got.get("name") == i["name"] and normalize_domain(((got.get("domainName") or {}).get("primaryLinkUrl")) or "") == host
        return ok, "the company is recorded with this name and domain" if ok else "the recorded company differs from what was approved"
    return _read_back(check, made)


def _person(w: Any, i: Mapping[str, Any], footer: str, rid: str) -> Outcome:
    email = i["email"].strip()
    existing = w.people_by_email(email)
    if existing:
        return Outcome(False, True, str(existing[0].get("id", "")), "a person with this email is already recorded",
                       "already existed; nothing was changed")
    company_id = i.get("company_id", "")
    if company_id:
        w.record("companies", "company", company_id)
    if w.shadow:
        return _SHADOW
    made = w.create_person(i["first_name"], i.get("last_name", ""), email, company_id)

    def check():
        got = w.record("people", "person", made)
        ok = str((got.get("emails") or {}).get("primaryEmail") or "").lower() == email.lower()
        return ok, "the person is recorded with this email" if ok else "the recorded person differs from what was approved"
    return _read_back(check, made)


_ID = r"^[^\x00-\x1f/\\]{1,140}$"
_DATE = r"^\d{4}-\d{2}-\d{2}$"

ACTIONS: Dict[str, WriteAction] = {a.name: a for a in (
    WriteAction("erp.request_comment", "erpnext", "Propose a comment on an accounting, sales or procurement record in ERPNext",
                _schema(["doctype", "name", "text"], doctype={"type": "string", "enum": sorted(ERP_DOCTYPES)},
                        name=_text(140, pattern=_ID), text=_text(2000)),
                lambda i: f"Add a comment to {i['doctype']} {i['name']}", _comment),
    WriteAction("erp.request_draft_purchase_order", "erpnext",
                "Propose a DRAFT purchase order in ERPNext. A person still has to submit it in ERPNext before anything is ordered",
                _schema(["supplier", "schedule_date", "items"], supplier=_text(140, pattern=_ID), schedule_date=_text(10, pattern=_DATE),
                        company=_text(140, pattern=_ID),
                        items={"type": "array", "minItems": 1, "maxItems": 20, "items": _schema(
                            ["item_code", "qty"], item_code=_text(140, pattern=_ID),
                            qty={"type": "number", "minimum": 0.001, "maximum": 1_000_000},
                            rate={"type": "number", "minimum": 0, "maximum": 1_000_000_000})}),
                lambda i: f"Create a draft purchase order (not submitted) for supplier {i['supplier']}: {len(i['items'])} line(s), needed by {i['schedule_date']}",
                _draft_po),
    WriteAction("support.request_private_note", "chatwoot",
                "Propose an internal note on a support conversation. The customer never sees it; there is no public reply",
                _schema(["conversation_id", "text"], conversation_id={"type": "integer", "minimum": 1, "maximum": 2_000_000_000}, text=_text(4000)),
                lambda i: f"Add a private note to support conversation {i['conversation_id']} (not visible to the customer)", _private_note),
    WriteAction("crm.request_company", "twenty", "Propose a CRM company. Nothing is created if one with the same domain exists",
                _schema(["name", "domain"], name=_text(200), domain=_text(253)),
                lambda i: f"Create CRM company {i['name']} ({i['domain']}) unless that domain is already recorded", _company),
    WriteAction("crm.request_person", "twenty", "Propose a CRM contact. Nothing is created if one with the same email exists",
                _schema(["first_name", "email"], first_name=_text(100), last_name={"type": "string", "maxLength": 100},
                        email=_text(254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"), company_id=_text(64, pattern=r"^[A-Za-z0-9-]+$")),
                lambda i: "Create a CRM contact unless one with the same email is already recorded", _person),
)}

__all__ = ["APP_GRANTS", "Outcome", "WriteAction", "ACTIONS", "RESERVED_MARKERS", "reserved_marker"]
