"""What an agent may propose, what the approver is told, and how each write is performed and read back.

Deliberately narrow: notes, comments, drafts and add-if-absent records. Nothing here sends a message to a customer,
submits or cancels an accounting document, pays, deletes or overwrites. A ``perform`` never raises once the application
has accepted a write: a failed read-back makes the result unverified, never "not applied".
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

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
    perform: Callable[..., Outcome]  # (writer, inputs, footer, request_id[, context] when ``prepare`` is set)
    grant: str = ""                  # the named grant that lets an agent propose it ("" = the application's own, APP_GRANTS)
    served_by: str = ""              # the capability module that offers it ("" = the application record capabilities)
    financial: bool = False          # the tenant must hold the financial-data grant for the application too
    #: (context, writer, inputs) -> the evidence the approver is shown, attests to, and whose digest they approve
    prepare: Optional[Callable[[Any, Any, Mapping[str, Any]], Dict[str, Any]]] = None


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


def _payment_correction(w: Any, i: Mapping[str, Any], footer: str, rid: str, context: Any) -> Outcome:
    from agentic_os.governed_writes import payment_correction  # noqa: PLC0415 (it imports Outcome from here)
    return payment_correction.perform(w, i, footer, rid, context)


def _prepare_correction(context: Any, w: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    from agentic_os.governed_writes import payment_correction  # noqa: PLC0415
    return payment_correction.prepare(context, w, i)


def _market(name: str) -> Callable[..., Any]:
    def run(*args: Any) -> Any:
        from agentic_os.governed_writes import market  # noqa: PLC0415 (it imports Outcome from here)
        return getattr(market, name)(*args)
    return run


def _prepare_market(name: str) -> Callable[..., Dict[str, Any]]:
    def run(context: Any, w: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
        from agentic_os.governed_writes import market  # noqa: PLC0415
        return getattr(market, name)(context, w, i)
    return run


def _apps(name: str) -> Callable[..., Any]:
    def run(*args: Any) -> Any:
        from agentic_os import app_definitions  # noqa: PLC0415
        return getattr(app_definitions, name)(*args)
    return run


def _media(name: str) -> Callable[..., Any]:
    def run(*args: Any) -> Any:
        from agentic_os.governed_writes import media  # noqa: PLC0415 (it imports Outcome from here)
        return getattr(media, name)(*args)
    return run


def _prepare_media(name: str) -> Callable[..., Dict[str, Any]]:
    def run(context: Any, w: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
        from agentic_os.governed_writes import media  # noqa: PLC0415
        return getattr(media, name)(context, w, i)
    return run


#: a media file as agents name it (governed_writes.media.PUBLIC_ASSET)
_ASSET_ID = {"type": "string", "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
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
    WriteAction("economics.request_payment_correction", "erpnext",
                "Propose correcting a received payment the bank's own statement shows for another amount: CANCEL the payment entry and "
                "enter it again, as its amendment, for the bank's amount. A difference alone is not enough: it is refused unless the "
                "statement rules out a fee, currency conversion, further instalment, allocation, reversal and timing. Take every value "
                "from economics.discrepancies",
                _schema(["reference", "payment_entry", "posted_amount", "bank_amount"], reference=_text(140, pattern=_ID),
                        payment_entry=_text(140, pattern=_ID),
                        posted_amount={"type": "number", "minimum": 0.01, "maximum": 1_000_000_000},
                        bank_amount={"type": "number", "minimum": 0.01, "maximum": 1_000_000_000}),
                lambda i: (f"CANCEL Payment Entry {i['payment_entry']} ({i['posted_amount']}) and enter it again for {i['bank_amount']}, "
                           f"the amount on the bank statement under {i['reference']}"),
                _payment_correction, grant="economics.propose", served_by="economics", financial=True, prepare=_prepare_correction),
    WriteAction("social.request_post", "postiz",
                "Propose a social post: `when` is 'draft' (saved, never published) or an ISO 8601 UTC time at least 15 minutes ahead "
                "(scheduled). There is no publish-now. An attached file must come from media.export for the channel's platform",
                _schema(["channel_id", "caption", "when"], channel_id=_text(80, pattern=r"^[A-Za-z0-9_-]+$"), caption=_text(2200),
                        when=_text(40), asset=_ASSET_ID),
                lambda i: ("Save a draft social post" if i.get("when") == "draft" else f"Schedule a social post for {i.get('when')}"),
                _media("perform_post"), grant="social.propose", served_by="media", prepare=_prepare_media("prepare_post")),
    WriteAction("market.request_watch", "market-radar",
                "Propose watching a public page of one of the researched sites, so that changes to it are reported",
                _schema(["url"], url=_text(400), title={"type": "string", "maxLength": 200}),
                lambda i: f"Start watching {i.get('url')}", _market("perform_watch"), grant="market.propose", served_by="market",
                prepare=_prepare_market("prepare_watch")),
    WriteAction("market.request_change_plan", "market-radar",
                "Propose a plan of changes: recommendations of one of your research missions, by id. A person approves the exact list",
                _schema(["research_id", "recommendation_ids"], research_id=_text(80, pattern=r"^[A-Za-z0-9_-]+$"),
                        recommendation_ids={"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string", "maxLength": 80}}),
                lambda i: f"Approve a change plan of {len(i.get('recommendation_ids') or [])} recommendation(s)",
                _market("perform_plan"), grant="market.propose", served_by="market", prepare=_prepare_market("prepare_plan")),
    WriteAction("apps.request_install", "apps",
                "Propose installing an application you defined, or a newer version of one. A person sees what it uses and keeps, "
                "and approves that exact definition",
                _schema(["definition"], definition={"type": "object"}),
                lambda i: f"Install the application {str((i.get('definition') or {}).get('name', ''))[:80]}",
                lambda w, i, footer, rid, ctx: _apps("perform_install")(w, i, footer, rid, ctx), grant="apps.install",
                served_by="apps", prepare=lambda ctx, w, i: _apps("prepare_install")(ctx, w, i)),
    WriteAction("media.request_generate", "vibexgen",
                "Propose generating an image or a video with a model, from a prompt (and optionally one of your images)",
                _schema(["kind", "prompt"], kind={"type": "string", "enum": ["image", "video"]}, prompt=_text(2000),
                        source_asset=_ASSET_ID,
                        width={"type": "integer", "minimum": 256, "maximum": 2048}, height={"type": "integer", "minimum": 256, "maximum": 2048},
                        duration={"type": "number", "minimum": 0.5, "maximum": 15}, seed={"type": "integer", "minimum": 0, "maximum": 2147483647}),
                lambda i: f"Generate a {i.get('kind')} with a model", _media("perform_generate"), grant="media.generate", served_by="media",
                prepare=_prepare_media("prepare_generate")),
)}

__all__ = ["APP_GRANTS", "Outcome", "WriteAction", "ACTIONS", "RESERVED_MARKERS", "reserved_marker"]
