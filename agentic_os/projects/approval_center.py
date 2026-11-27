"""One list of what waits for a person, whoever proposed it, as cards; and one way to decide.

It holds no approvals of its own. A governed write is decided by ``GovernedWrites.decide`` (the Mission Runtime's gate);
a product action by the same ``products.approvals`` functions the product routes call, so the product still verifies a
tenant-bound, single-use token and keeps its own mandatory gate and read-back. Who may decide is the rule the routes
apply: not an agent, not a requester whose tenant has an approver, and a tenant credential needs the route's permission
among its tenant's refs and, for a product, the product granted to its tenant.

The one thing kept here is who proposed a product record (``Proposals``): Projects knows which agent made the proposal
and the products do not all record it.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from agentic_os.products.principal import SEPARATED, may_decide

WRITES = "writes"
#: what a tenant credential needs among its tenant's policy_refs: the same permissions the decision routes require
WRITE_PERMISSION, PRODUCT_PERMISSION = "writes:decide", "projects:write"
_APPS = {"erpnext": "ERPNext", "chatwoot": "Support (Chatwoot)", "twenty": "CRM (Twenty)", "postiz": "Social (Postiz)",
         "vibexgen": "VibeXGen", "market-radar": "Market Radar", "apps": "Applications"}
_PUBLIC_URL = {"erpnext": "ERPNEXT_PUBLIC_URL", "chatwoot": "CHATWOOT_PUBLIC_URL", "twenty": "TWENTY_PUBLIC_URL"}


class DecisionRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Person:
    principal: str

    @property
    def role(self) -> str:
        return self.principal.partition(":")[0]

    @property
    def tenant(self) -> Optional[str]:
        """The one tenant this person is limited to, or None (the operator, the admin, a single owner)."""
        role, _, tenant = self.principal.partition(":")
        return tenant if role in ("approver", "requester") and tenant else None

    def refusal(self, permission: str) -> str:
        """Why this person may not decide something needing ``permission``; empty when they may."""
        if not may_decide(self.principal):
            return SEPARATED
        if self.tenant is None:
            return ""
        from agentic_os.mission.control_auth import ConfigError, tenant_limits  # noqa: PLC0415
        try:
            refs = tenant_limits(self.tenant)[0]
        except ConfigError:
            refs = []
        return "" if any(fnmatchcase(permission, r) for r in refs) else f"your tenant {self.tenant} is not granted {permission}"


@dataclass
class Card:
    source: str                                      # "writes", or a product id
    id: str
    title: str
    application: str
    state: str
    state_words: str
    tenant: str = ""
    requested_by: str = ""
    requester_label: str = ""
    changes: List[Tuple[str, str]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)       # {title, columns, rows}
    affected: List[Dict[str, str]] = field(default_factory=list)     # {label, url?}
    created_at: Any = None
    expires_at: Any = None
    expiry_note: str = ""
    digest: str = ""                                 # what a decision must name
    attestation: str = ""                            # a statement the approver must tick to approve ("" = none)
    decisions: List[str] = field(default_factory=list)               # what THIS person may do now
    blocked: str = ""                                # why a decision is not offered although one is awaited
    waiting: bool = False
    outcome: List[Tuple[str, str]] = field(default_factory=list)
    native: List[Dict[str, str]] = field(default_factory=list)       # {label, url}
    native_note: str = ""

    @property
    def ref(self) -> str:
        return f"{self.source}/{self.id}"


class Proposals:
    """Which agent proposed a product record. ``path`` None = in memory only."""

    def __init__(self, path: "str | Path | None" = None) -> None:
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not Path(path).exists():
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path) if path is not None else ":memory:", check_same_thread=False, isolation_level=None)
        self._db.execute("CREATE TABLE IF NOT EXISTS proposals(product TEXT, record TEXT, agent TEXT, tenant TEXT, at REAL,"
                         " PRIMARY KEY(product, record))")

    def record(self, product: str, record_id: str, agent: str, tenant: str) -> None:
        with self._lock:                              # the first proposer stands: a replay or a later read changes nothing
            self._db.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?)", (product, record_id, agent, tenant, time.time()))

    def get(self, product: str, record_id: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            row = self._db.execute("SELECT agent, tenant FROM proposals WHERE product=? AND record=?", (product, record_id)).fetchone()
        return None if row is None else (row[0], row[1])


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _web(url: str) -> str:
    """Only an http(s) address is ever made a link."""
    return url if re.match(r"^https?://[^\s\"'<>]+$", url or "") else ""


def app_link(app: str, kind: str, record: str) -> str:
    """The record in the application's own UI, where the deployment says what that UI's address is for a person (the
    address Projects itself uses is usually internal, so it is never used here)."""
    if app not in _PUBLIC_URL:                                   # no per-record address is known for it
        return ""
    base = _web(os.environ.get(_PUBLIC_URL[app], "").strip().rstrip("/"))
    if not base or not record:
        return ""
    if app == "erpnext":
        return f"{base}/app/{quote(kind.lower().replace(' ', '-'))}/{quote(record, safe='')}"
    if app == "chatwoot":
        account = os.environ.get("CHATWOOT_ACCOUNT_ID", "1").strip() or "1"
        return f"{base}/app/accounts/{quote(account)}/conversations/{quote(record, safe='')}"
    return f"{base}/object/{quote(kind)}/{quote(record, safe='')}"


_STATE_WORDS = {"pending": "Waiting for a decision", "deciding": "Being performed", "executed_verified": "Done and verified",
                "executed_unverified": "Written, but NOT verified: check the application", "no_change": "Nothing to change: it already held",
                "previewed": "Preview only: nothing was written", "failed": "Not performed", "rejected": "Rejected",
                "outcome_unknown": "OUTCOME UNKNOWN: check the application before asking again", "expired": "Expired without a decision",
                "AWAITING_APPROVAL": "Waiting for a decision", "APPROVED": "Approved, waiting to be executed", "EXECUTED": "Executed",
                "REJECTED": "Rejected", "FAILED": "Execution failed", "awaiting_approval": "Waiting for a decision"}


class ApprovalCenter:
    def __init__(self, *, writes: Any = None, hub: Any = None, proposals: Optional[Proposals] = None,
                 agent_label: Callable[[str], str] = lambda principal: "") -> None:
        self.writes, self.hub, self.agent_label = writes, hub, agent_label
        self.proposals = proposals or Proposals()

    # ── governed writes ──────────────────────────────────────────────────────────
    def _write_card(self, person: Person, v: Mapping[str, Any]) -> Card:
        app, action, inputs = v["app"], v["action"], v.get("inputs")
        card = Card(source=WRITES, id=v["request_id"], title=v["summary"], application=_APPS.get(app, app), state=v["state"],
                    state_words=_STATE_WORDS.get(v["state"], v["state"]), tenant=v["tenant"], requested_by=v["requested_by"],
                    requester_label=self.agent_label(v["requested_by"]), created_at=v["created_at"], expires_at=v["expires_at"],
                    waiting=v["state"] == "pending")
        if person.role == "requester":
            card.changes = [("Proposed values", "shown to those who can decide: sign in as an approver")]
        elif inputs is None:
            card.changes = [("Proposed values", "erased after the retention period; the ledger keeps their digest")]
        else:
            for key, value in inputs.items():
                if key == "items" and isinstance(value, list):
                    columns = ["item_code", "qty", "rate"]
                    card.tables.append({"title": "Lines", "columns": columns,
                                        "rows": [[_text(line.get(c, "")) for c in columns] for line in value]})
                else:
                    card.changes.append((key.replace("_", " "), _text(value)))
        evidence = v.get("evidence") or {}
        card.changes += [(str(k), _text(val)) for k, val in evidence.get("show", [])]
        if not card.waiting and v.get("attested"):
            card.changes.append(("The approver attested", _text(v["attested"])))
        card.affected = self._affected(app, action, inputs or {}, v)
        if card.waiting:
            card.digest = v.get("digest", "")
            card.attestation = str(evidence.get("attestation", ""))
            authorization = v.get("authorization") or {}
            why = person.refusal(WRITE_PERMISSION) or ("a requester does not decide changes" if person.role == "requester" else "")
            if authorization.get("verdict") == "denied":
                card.decisions, card.blocked = ([] if why else ["reject"]), why or f"It cannot be approved: {authorization.get('reason', '')}"
            else:
                card.decisions, card.blocked = ([] if why else ["approve", "reject"]), why
        else:
            executed = {True: "yes", False: "no", None: "unknown"}[v.get("executed")]
            card.outcome = [(k, val) for k, val in (("Result", card.state_words), ("Decided by", v.get("decided_by", "")),
                            ("Application changed", executed), ("Verified by reading it back", "yes" if v.get("verified") else "no"),
                            ("Record", v.get("record", "")), ("Read-back", v.get("read_back", "")), ("Note", v.get("note", ""))) if val]
        card.native_note = ("ERPNext has no approval step for this proposal; decide it here. A draft purchase order is then "
                            "submitted in ERPNext, by a person." if action == "erp.request_draft_purchase_order"
                            else f"{_APPS.get(app, app)} has no approval step for this; decide it here.")
        card.native = [{"label": f"Open in {card.application}", "url": a["url"]} for a in card.affected if a.get("url")][:3]
        return card

    @staticmethod
    def _affected(app: str, action: str, inputs: Mapping[str, Any], v: Mapping[str, Any]) -> List[Dict[str, str]]:
        made = v.get("record", "")
        out: List[Dict[str, str]] = []

        def add(label: str, kind: str = "", record: str = "") -> None:
            url = app_link(app, kind, record) if record else ""
            out.append({"label": label, **({"url": url} if url else {})})
        if action == "economics.request_payment_correction":
            if inputs:
                add(f"Payment Entry {inputs['payment_entry']} (is cancelled by this, and kept)", "Payment Entry", str(inputs["payment_entry"]))
                add(f"Bank reference {inputs['reference']}")
            if made:
                add(f"Payment Entry {made} (its replacement)", "Payment Entry", made)
        elif action == "social.request_post":
            if inputs:
                add(f"Channel {inputs['channel_id']}")
                if inputs.get("asset"):
                    add(f"Media file {inputs['asset']}")
            if made:
                add(f"Post {made} (created)")
        elif action == "erp.request_comment" and inputs:
            add(f"{inputs['doctype']} {inputs['name']}", inputs["doctype"], inputs["name"])
            if made:
                add(f"Comment {made} (created)")
        elif action == "erp.request_draft_purchase_order":
            if inputs:
                add(f"Supplier {inputs['supplier']}", "Supplier", inputs["supplier"])
            if made:
                add(f"Purchase Order {made} (draft, created)", "Purchase Order", made)
        elif action == "support.request_private_note" and inputs:
            add(f"Conversation {inputs['conversation_id']}", "conversation", str(inputs["conversation_id"]))
            if made:
                add(f"Private note {made} (created)")
        elif app == "twenty":
            kind = "company" if action == "crm.request_company" else "person"
            if inputs:
                add(f"CRM {kind} {inputs.get('domain') or inputs.get('email')}")
            if made:
                add(f"CRM {kind} record {made}", kind, made)
        return out or ([{"label": f"Record {made}"}] if made else [])

    # ── product actions ──────────────────────────────────────────────────────────
    def _product_allowed(self, person: Person, product_id: str) -> bool:
        if person.tenant is None:
            return True
        from agentic_os.agent_gateway.product_capabilities import tenant_has_product  # noqa: PLC0415
        return tenant_has_product(person.tenant, product_id)

    def _requested_by(self, card: Card, product_id: str, record_id: str, recorded: str) -> None:
        known = self.proposals.get(product_id, record_id)
        if known:
            card.requested_by, card.tenant = known
            card.requester_label = self.agent_label(known[0])
        else:
            card.requested_by = recorded or "not recorded by the product"

    def _sentinel_card(self, person: Person, spec: Any, a: Mapping[str, Any]) -> Card:
        from agentic_os.products import sentinel as sn  # noqa: PLC0415
        record = sn.approval_card(spec, a)
        rid, state = str(a.get("request_id", "")), str(a.get("state", ""))
        card = Card(source=spec.id, id=rid, title=record["title"], application=spec.display_name, state=state,
                    state_words=_STATE_WORDS.get(state, state), created_at=a.get("created_at") or None,
                    expiry_note="Edge Sentinel sets no expiry; a changed action voids the approval")
        self._requested_by(card, spec.id, rid, "")
        card.changes = [("operation", str(a.get("capability", "")))] + [(k.replace("_", " "), _text(v)) for k, v in dict(a.get("params") or {}).items()]
        card.changes.append(("consequence", record["consequence"]))
        card.affected = [{"label": f"Case {a.get('case_id', '')}"}] + ([{"label": f"Address {sn.action_ip(a)}"}] if sn.action_ip(a) else [])
        card.waiting = state in ("AWAITING_APPROVAL", "APPROVED")
        if card.waiting:
            card.digest = record["digest"]
            why = person.refusal(PRODUCT_PERMISSION)
            card.blocked = why
            if not why:
                card.decisions = ["approve", "reject"] if state == "AWAITING_APPROVAL" else ["execute"]
        decision, receipt, check = a.get("decision") or {}, a.get("receipt") or {}, a.get("verification") or {}
        if decision or receipt or not card.waiting:
            card.outcome = [(k, v) for k, v in (("State", card.state_words), ("Decided by", _text(decision.get("actor"))),
                            ("Execution", _text(receipt.get("status"))), ("Receipt", _text(receipt.get("receipt_id"))),
                            ("Read-back", " ".join(x for x in (_text(check.get("state")), _text(check.get("detail"))) if x)
                             or (sn.read_back_text(a) if receipt else ""))) if v]
        url = _web(record["open"]["url"])
        card.native = [{"label": record["open"]["label"], "url": url}] if url else []
        card.native_note = f"{spec.display_name} can also decide this in its own interface." if url else ""
        return card

    def _qa_card(self, person: Person, spec: Any, r: Mapping[str, Any]) -> Card:
        from agentic_os.products import qa as qamod  # noqa: PLC0415
        record = qamod.approval_card(spec, r)
        rid, state = str(r.get("id", "")), str(r.get("state", ""))
        card = Card(source=spec.id, id=rid, title=record["title"], application=spec.display_name, state=state,
                    state_words=_STATE_WORDS.get(state, state), created_at=r.get("created_at") or None,
                    expiry_note="the product holds the run until it is decided; a changed estimate voids the approval")
        self._requested_by(card, spec.id, rid, _text(r.get("requested_by")))
        card.changes = [(k.replace("_", " "), _text(r.get(k))) for k in ("plan", "profile", "target_id") if r.get(k)]
        card.changes.append(("consequence", record["consequence"]))
        card.affected = [{"label": f"Test target {r.get('target_id', '')}"}]
        card.waiting = state == "awaiting_approval"
        if card.waiting:
            card.digest = record["digest"]
            card.blocked = person.refusal(PRODUCT_PERMISSION)
            card.decisions = [] if card.blocked else ["approve", "reject"]
        else:
            card.outcome = [("State", card.state_words)] + [(k, _text(r.get(k))) for k in ("outcome", "verdict") if r.get(k)]
        url = _web(record["open"]["url"])
        card.native = [{"label": record["open"]["label"], "url": url}] if url else []
        card.native_note = f"{spec.display_name} shows the run and its evidence." if url else ""
        return card

    def _product_cards(self, person: Person) -> Tuple[List[Card], List[str]]:
        from agentic_os.products.client import ProductError  # noqa: PLC0415
        cards, problems = [], []
        for spec in (self.hub.specs() if self.hub is not None else []):
            if not self._product_allowed(person, spec.id):
                continue
            try:
                if not self.hub.ready(spec.id):
                    problems.append(f"{spec.display_name} is unavailable; what waits there is not shown")
                    continue
                who = f"projects:{person.principal}"
                if spec.kind == "security":
                    for state, limit in (("AWAITING_APPROVAL", 50), ("APPROVED", 50), ("EXECUTED", 5), ("FAILED", 5), ("REJECTED", 5)):
                        for a in (self.hub.sentinel(spec.id).actions(state=state, limit=limit, actor=who).get("items") or []):
                            cards.append(self._sentinel_card(person, spec, self.hub.with_mode(spec.id, a)))
                elif spec.kind == "quality":
                    for r in (self.hub.qa(spec.id).runs(state="awaiting_approval", limit=50, actor=who).get("items") or []):
                        cards.append(self._qa_card(person, spec, r))
            except ProductError:
                problems.append(f"{spec.display_name} did not answer; what waits there is not shown")
        return cards, problems

    # ── the two things a person does ─────────────────────────────────────────────
    def listing(self, person: Person, *, recent: int = 20) -> Dict[str, Any]:
        waiting, done, problems = [], [], []
        if self.writes is not None:
            for v in self.writes.list(tenant=person.tenant, limit=200, for_approver=person.role != "requester"):
                (waiting if v["state"] == "pending" else done).append(self._write_card(person, v))
        product_cards, problems = self._product_cards(person)
        for c in product_cards:
            (waiting if c.waiting else done).append(c)
        return {"waiting": waiting, "recent": done[:recent], "problems": problems}

    def card(self, person: Person, source: str, record_id: str) -> Card:
        from agentic_os.products.client import ProductError  # noqa: PLC0415
        from agentic_os.products.schemas import ACTION_ID, RUN_ID  # noqa: PLC0415
        missing = DecisionRefused(404, "no such approval")
        if not re.match(r"^[A-Za-z0-9_.-]{1,128}$", record_id):
            raise missing
        if source == WRITES:
            if self.writes is None:
                raise missing
            from agentic_os.governed_writes.service import WriteError  # noqa: PLC0415
            try:
                # as in the API: a requester sees what is proposed and its outcome; the exact values are for those who can decide
                return self._write_card(person, self.writes.get(record_id, tenant=person.tenant, for_approver=person.role != "requester"))
            except WriteError:
                raise missing from None
        spec = next((s for s in (self.hub.specs() if self.hub is not None else []) if s.id == source), None)
        if spec is None or not self._product_allowed(person, spec.id):
            raise missing
        try:
            who = f"projects:{person.principal}"
            if spec.kind == "security" and re.match(ACTION_ID, record_id):
                return self._sentinel_card(person, spec, self.hub.with_mode(spec.id, self.hub.sentinel(spec.id).action(record_id, actor=who)))
            if spec.kind == "quality" and re.match(RUN_ID, record_id) and self.hub.ready(spec.id):
                return self._qa_card(person, spec, self.hub.qa(spec.id).run(record_id, actor=who))
        except ProductError as e:
            raise DecisionRefused(404 if e.status == 404 else 502, "the product did not answer for this record") from None
        raise missing

    def decide(self, person: Person, source: str, record_id: str, *, decision: str, digest: str, attested: bool = False) -> Card:
        card = self.card(person, source, record_id)                 # read fresh: another tenant's does not exist
        if decision not in card.decisions:
            raise DecisionRefused(403 if card.waiting else 409,
                                  card.blocked or (f"this cannot be decided that way now: it is {card.state_words.lower()}"))
        if not digest or digest != card.digest:
            raise DecisionRefused(409, "it changed since you reviewed it; review it again")
        if source == WRITES:
            from agentic_os.governed_writes.service import WriteError  # noqa: PLC0415
            try:
                from agentic_os.mission.principal import principal_of_signed_in  # noqa: PLC0415
                # ``person`` was resolved from this request's signed-in session by the approval pages
                self.writes.decide(record_id, approve=decision == "approve", digest=digest, approver=person.principal, tenant=person.tenant,
                                   attested=attested, principal=principal_of_signed_in(person.principal, person.tenant, method="projects-sign-in"))
            except WriteError as e:
                raise DecisionRefused({"not_found": 404, "conflict": 409, "unavailable": 503}.get(e.code, 403), e.detail) from None
        else:
            from agentic_os.products import approvals  # noqa: PLC0415
            from agentic_os.products.client import ProductError  # noqa: PLC0415
            spec = next(s for s in self.hub.specs() if s.id == source)
            try:
                if spec.kind == "quality":
                    approvals.qa_approval(self.hub, source, record_id, approved=decision == "approve", estimate_digest=digest,
                                          rationale="", principal=person.principal)
                elif decision == "execute":
                    approvals.sentinel_execute(self.hub, source, record_id, digest=digest, principal=person.principal)
                else:
                    approvals.sentinel_decision(self.hub, source, record_id, approved=decision == "approve", digest=digest,
                                                rationale="", principal=person.principal)
            except ProductError as e:
                said = e.as_dict()
                raise DecisionRefused(e.status if e.status in (403, 404, 409) else 502,
                                      f"{spec.display_name} refused: {said.get('detail') or said.get('reason') or e.code}"[:400]) from None
        return self.card(person, source, record_id)


__all__ = ["ApprovalCenter", "Card", "Person", "Proposals", "DecisionRefused", "app_link", "WRITES"]
