"""Today — the outcome-first "what needs me?" feed, as an AttentionItem projection.

CONTROL_SURFACES_UX plan §3.3. An AttentionItem is a *projection* over records that already exist; it is
not a second system of record:

  * the Priority Engine's surfaced/deferred decisions (``/priorities``),
  * the cross-runtime attention queue (approvals, reconnects, clarifications: ``/attention``),
  * governed "cases" — domain-language work (a missing visa document, an unconfirmed appointment, an
    unanswered quote) whose next action is an external message.

Identity is stable (a hash of tenant + project + source key) and duplicates collapse deterministically.

The one write path, :meth:`Today.approve`, is the existing governed external-action path and nothing new:
the exact request (recipients, content, attempt) is content-addressed; the approval binds that digest
(:class:`ExternalApprovalBridge`: attestation required, single use, post-approval mutation finds no
approval); the send runs through the governed operator and its provider claim is independently verified.
Five states are kept apart on purpose — suggested, approved, sent, delivery verified, business outcome
observed — because a sent reminder is not a confirmed appointment.

Honesty: the case data here is a labelled SAMPLE and the adapter is the in-repo fake; nothing leaves the
process. A deployment swaps the case source and adapter, not the contract.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from agentic_os.overlays import Principal

STATES = ("needs_action", "waiting", "scheduled", "running", "verified", "failed", "dismissed")
LIFECYCLE = ("suggested", "approved", "sent", "delivery_verified", "outcome_observed")
_STATE_ORDER = {s: i for i, s in enumerate(STATES)}
BATCH_ACTIONS = ("snooze", "dismiss", "assign", "categorize")
MAX_BATCH = 50
UNDO_TTL_S = 600.0
_DAY = 86400.0


class TodayError(Exception):
    """A refused request: ``status`` is the HTTP status the route maps it to."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


# ── identity + dedup ──────────────────────────────────────────────────────────────────
def stable_id(tenant: str, project: str, key: str) -> str:
    return "att_" + hashlib.sha256(f"{tenant}|{project}|{key}".encode()).hexdigest()[:16]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _tier(score: float) -> str:
    return "high" if score >= 0.9 else "medium" if score >= 0.5 else "low"


def _blank_lifecycle(**on: bool) -> Dict[str, bool]:
    return {k: bool(on.get(k, False)) for k in LIFECYCLE}


def _item(*, tenant: str, project: str, key: str, dedupe: str, title: str, reason: str, state: str,
          score: float, label: str, executable: bool, source_refs: Sequence[str], confidence: Optional[float],
          now: float, **extra: Any) -> dict:
    base = {
        "id": stable_id(tenant, project, key), "tenant_id": tenant, "project_id": project, "mission_id": None,
        "subject_refs": [], "user_title": title, "reason": reason, "state": state,
        "priority": {"tier": _tier(score), "score": round(score, 3), "reason": reason, "policy_version": "static-0"},
        "owner": None,
        "next_action": {"capability_id": None, "plain_language_label": label, "preview_ref": None,
                        "executable": executable},
        "deadline": None, "source_refs": list(source_refs), "evidence_refs": [], "observed_at": None,
        "known_at": now, "confidence": confidence, "approval_ref": None, "policy_decision_ref": None,
        "last_receipt_ref": None, "verification_ref": None, "outcome_ref": None, "stale": False, "epoch": 0, "category": "",
        "lifecycle": _blank_lifecycle(suggested=True), "kind": "signal", "dedupe_key": dedupe,
    }
    base.update(extra)
    return base


# ── projections over the existing surfaces ────────────────────────────────────────────
def project_priorities(prio: Optional[dict], *, tenant: str, project: str, now: float) -> List[dict]:
    out: List[dict] = []
    for bucket, state in (("surfaced", "needs_action"), ("deferred", "scheduled")):
        for d in (prio or {}).get(bucket, []):
            subject, action = d.get("subject", ""), d.get("proposed_action", "")
            out.append(_item(
                tenant=tenant, project=project, key=f"priority:{d.get('candidate_id') or subject}",
                dedupe=_norm(subject), title=subject, reason=d.get("rationale", ""), state=state,
                score=float(d.get("priority", 0.0)), label=action, executable=False,
                source_refs=[d.get("candidate_id", "")], confidence=d.get("confidence"), now=now,
                source_app=d.get("source_app", ""), risk_tier=d.get("risk_tier", ""),
                requires_approval=bool(d.get("requires_approval"))))
    return out


def project_attention(att: Optional[Sequence[dict]], *, tenant: str, project: str, now: float) -> List[dict]:
    out: List[dict] = []
    for a in att or []:
        out.append(_item(
            tenant=tenant, project=project, key=f"attention:{a.get('id')}", dedupe=_norm(a.get("reason", "")),
            title=a.get("title", ""), reason=a.get("reason", "") or a.get("consequence", ""),
            state="needs_action", score=float(a.get("priority", 0)) / 100.0, label=a.get("title", ""),
            executable=False, source_refs=[a.get("source_ref", "") or a.get("id", "")], confidence=None,
            now=now, consequence=a.get("consequence", ""), attention_kind=a.get("kind", "")))
    return out


def merge_items(items: Sequence[dict]) -> List[dict]:
    """Collapse duplicates by ``dedupe_key`` (keep the highest-priority one, union the source refs), then a
    deterministic order: state, priority, id."""
    best: Dict[str, dict] = {}
    for it in items:
        k = it.get("dedupe_key") or it["id"]
        cur = best.get(k)
        if cur is None:
            best[k] = dict(it)
            continue
        keep, other = (it, cur) if it["priority"]["score"] > cur["priority"]["score"] else (cur, it)
        merged = dict(keep)
        merged["source_refs"] = sorted({r for r in keep["source_refs"] + other["source_refs"] if r})
        best[k] = merged
    return sorted(best.values(), key=lambda i: (_STATE_ORDER[i["state"]], -i["priority"]["score"], i["id"]))


# ── governed cases ────────────────────────────────────────────────────────────────────
@dataclass
class _Case:
    key: str
    domain: str
    title: str
    reason: str
    deadline: Optional[float]
    owner: str
    channel: str
    targets: List[dict]                  # {id, label, address, consent, why_skipped?}
    subject: str
    body: str
    follow_up_s: float
    outcome_label: str
    evidence: List[dict]
    connector_ok: bool = True
    identity_candidates: List[dict] = field(default_factory=list)
    score: float = 0.8
    category: str = ""                   # user label set by batch triage (reversible)
    topics: List[str] = field(default_factory=list)      # words that make a note about this case (capture matching)
    surfaced: bool = True                # a dormant case is known work that does NOT need attention yet
    # mutable ledger
    attempt: int = 0
    phase: str = "suggested"             # suggested | sent | failed | unknown | outcome | dismissed
    unknown_key: str = ""                # idempotency key of an approved send whose outcome is unconfirmed
    unknown_digest: str = ""             # the reviewed payload digest that approval was bound to
    snoozed_until: float = 0.0
    follow_up_at: float = 0.0
    sent_digest: str = ""
    receipt: Optional[dict] = None
    verification: str = ""
    failure: str = ""
    approval_ref: str = ""
    decision_ref: str = ""
    sends: int = 0                       # provider submissions actually made (no-duplicate-send evidence)
    events: List[dict] = field(default_factory=list)


@dataclass
class _Reminder:
    """An internal reminder created from a capture ("remind me to call John Tuesday"). No external action."""
    text: str
    due: float
    capture_id: str
    created: float
    done: bool = False
    snoozed_until: float = 0.0
    owner: str = "You"
    category: str = ""
    events: List[dict] = field(default_factory=list)


def _seed_cases(now: float) -> List[_Case]:
    ev = lambda kind, label, ago, ref: {"kind": kind, "label": label, "observed_at": now - ago, "ref": ref}  # noqa: E731
    return [
        _Case("travel-taylor-visa", "travel", "Visa documents missing — Taylor trip",
              "Departure in 8 days; the passport scan and visa form have not been received.",
              now + 8 * _DAY, "You", "whatsapp",
              [{"id": "taylor", "label": "Taylor Brooks", "address": "+1 555 0101", "consent": True}],
              "", "Hi Taylor — to finish your visa we still need a clear passport scan and the signed visa "
              "form. Could you send them by Friday? Thanks!", 3 * _DAY, "Documents received and validated",
              [ev("message", "WhatsApp: \"I'll send the docs soon\"", 2 * 3600, "wa:msg:7712"),
               ev("record", "Booking TRIP-88: visa documents 0 of 2 received", 600, "booking:TRIP-88")], score=0.95,
              topics=["passport", "visa", "documents", "docs", "form", "scan"]),
        # Known work that needs nothing yet. It reaches Today only when evidence arrives (e.g. a capture).
        _Case("travel-lee-lisbon-visa", "travel", "Visa documents missing — Jordan Lee, Lisbon trip",
              "Departure in 12 days; the passport scan has not been received.", now + 12 * _DAY, "You", "whatsapp",
              [{"id": "jordan", "label": "Jordan Lee", "address": "+1 555 0177", "consent": True}],
              "", "Hi Jordan — for your Lisbon visa we still need a clear passport scan. Could you send it this "
              "week? Thanks!", 3 * _DAY, "Documents received and validated",
              [ev("record", "Booking TRIP-91: visa documents 1 of 2 received", 3 * _DAY, "booking:TRIP-91")],
              score=0.85, topics=["passport", "visa", "documents", "docs", "form", "scan", "lisbon"], surfaced=False),
        _Case("clinic-unconfirmed", "clinic", "3 patients haven't confirmed tomorrow's appointment",
              "Confirmation is due by 6pm; unconfirmed slots can't be refilled after that.",
              now + 0.4 * _DAY, "Front desk", "sms",
              [{"id": "p1", "label": "A. Novak", "address": "+1 555 0142", "consent": True},
               {"id": "p2", "label": "R. Idris", "address": "+1 555 0143", "consent": True},
               {"id": "p3", "label": "M. Ortega", "address": "+1 555 0144", "consent": False,
                "why_skipped": "No reminder consent on file"}],
              "", "Reminder: you have an appointment tomorrow. Reply YES to confirm or call us to reschedule.",
              2 * 3600, "Confirmation reply observed",
              [ev("record", "Schedule: 3 of 14 appointments unconfirmed", 900, "sched:2026-10-22")], score=0.9,
              topics=["appointment", "confirm", "confirmed", "reminder"]),
        _Case("quote-acme-followup", "sales", "Quote hasn't been answered — Acme",
              "Quote #Q-310 was sent 6 days ago with no reply; buyers usually decide within a week.",
              None, "You", "email",
              [{"id": "acme", "label": "Dana Whitfield (Acme)", "address": "dana@acme.example", "consent": True}],
              "Following up on quote Q-310",
              "Hi Dana — checking in on quote Q-310. Happy to adjust scope or walk through it on a call.",
              4 * _DAY, "Reply or order observed",
              [ev("record", "Quote Q-310 sent, no reply (6 days)", 6 * _DAY, "quote:Q-310"),
               ev("policy", "Follow-up allowed: no discount change", 60, "policy:followup")], score=0.7,
              topics=["quote", "q-310", "proposal", "pricing"]),
        _Case("invoice-1042-reminder", "receivables", "Invoice #1042 is past due — customer hasn't replied",
              "Net-30 passed 5 days ago. The WhatsApp connection expired, so a reminder can't be sent yet.",
              None, "You", "whatsapp",
              [{"id": "c1", "label": "Kestrel Ltd accounts", "address": "+44 20 5550 1042", "consent": True}],
              "", "Hi — a friendly reminder that invoice #1042 is now past due. Could you let us know the "
              "payment date?", 2 * _DAY, "Payment reconciled",
              [ev("record", "Invoice #1042 overdue by 5 days", 300, "invoice:1042")], connector_ok=False, score=0.6,
              topics=["invoice", "1042", "payment", "overdue"]),
        _Case("invoice-1050-ambiguous", "receivables", "Reminder for Sam Rivera — two customers share this name",
              "Choose which Sam Rivera before anything is sent.", None, "You", "email",
              [{"id": "sam", "label": "Sam Rivera", "address": "", "consent": True}],
              "Payment reminder", "Hi Sam — a quick reminder about your open invoice.", 3 * _DAY,
              "Payment reconciled", [ev("record", "Two contacts match \"Sam Rivera\"", 120, "identity:sam")],
              identity_candidates=[
                  {"id": "sam-a", "label": "Sam Rivera", "email": "sam@harbor.example", "detail": "Harbor Co · invoice #1050"},
                  {"id": "sam-b", "label": "Sam Rivera", "email": "sam.r@northwind.example", "detail": "Northwind · no open invoice"}],
              score=0.5),
    ]


class Today:
    """One tenant/project's Today feed plus the governed ledger for its cases (in memory, sample data)."""

    def __init__(self, *, tenant: str = "demo", project: str = "customer-ops",
                 clock: Callable[[], float] = time.time, adapter: Any = None, sample_cases: bool = True) -> None:
        from agentic_os.agent_gateway.external.approval_bridge import ExternalApprovalBridge
        from agentic_os.agent_gateway.external.fake_adapter import FakeExternalAgentAdapter
        from agentic_os.agent_gateway.external.operator import ExternalAgentOperator
        self.tenant, self.project, self.clock = tenant, project, clock
        self._lock = threading.RLock()
        self._bridge = ExternalApprovalBridge()
        self._operator = ExternalAgentOperator(adapter or FakeExternalAgentAdapter())
        self.simulated = bool(getattr(self._operator.adapter, "simulated", False))   # nothing real can have been sent
        self._cases: Dict[str, _Case] = {}
        self._reminders: Dict[str, _Reminder] = {}
        self._intake: Any = None
        self._undo: Dict[str, dict] = {}                 # batch undo tokens (single use, expiring)
        self._seen: Dict[str, float] = {}                # notification keys already acknowledged
        self._prefs: Dict[str, Any] = {"enabled": True, "quiet_start": None, "quiet_end": None, "tz_offset_minutes": 0}
        # sample_cases=False is the FAIL-CLOSED mode for a real deployment: no sample work, nothing executable —
        # the feed is only the read-only projection of real signals until a governed case source + authenticated
        # tenant/actor binding exist. (The only adapter here is the in-repo fake; it can never reach a provider.)
        self.sample_cases = sample_cases
        for c in (_seed_cases(clock()) if sample_cases else []):
            self._cases[stable_id(tenant, project, "case:" + c.key)] = c
            self._log(c, "suggested", "Suggested from connected channels")

    # ── helpers ─────────────────────────────────────────────────────────────────────
    def _log(self, c: _Case, kind: str, text: str) -> None:
        c.events.append({"at": self.clock(), "kind": kind, "text": text})

    def _case(self, item_id: str) -> _Case:
        c = self._cases.get(item_id)
        if c is not None and not c.surfaced:
            c = None                                  # dormant work is not an item until something surfaces it
        if c is None:
            if not self.sample_cases:
                raise TodayError(501, "unsupported",
                                 "no governed case source is configured for this deployment; Today is read-only")
            raise TodayError(404, "not_found", "no such work item")
        return c

    def _select(self, c: _Case, targets: Optional[Sequence[str]]) -> tuple:
        wanted = list(targets) if targets else [t["id"] for t in c.targets]
        known = {t["id"]: t for t in c.targets}
        bad = [w for w in wanted if w not in known]
        if bad:
            raise TodayError(422, "unknown_target", f"unknown recipient(s): {', '.join(bad)}")
        picked = [known[w] for w in wanted if known[w]["consent"]]
        skipped = [{"id": known[w]["id"], "label": known[w]["label"], "reason": known[w].get("why_skipped", "")}
                   for w in wanted if not known[w]["consent"]]
        return picked, skipped

    def _identity(self, c: _Case, identity_id: Optional[str]) -> Optional[dict]:
        if not c.identity_candidates:
            return None
        if not identity_id:
            return None
        found = next((i for i in c.identity_candidates if i["id"] == identity_id), None)
        if found is None:
            raise TodayError(422, "unknown_identity", "that contact is not one of the matches")
        return found

    def _request(self, c: _Case, picked: Sequence[dict], identity: Optional[dict], item_id: str):
        from agentic_os.agent_gateway.external.contracts import (
            AgentIdentity, AgentPermissionScope, AgentTaskRequest)
        recipients = [{"id": t["id"], "address": t["address"]} for t in picked]
        if identity:
            recipients = [{"id": identity["id"], "address": identity["email"]}]
        principal = Principal(id="user:operator", kind="user", roles=("operator",), tenant=self.tenant)
        cap = "personal_agent.connected_app_action"
        return AgentTaskRequest(
            identity=AgentIdentity(provider="fake-external-agent", adapter_version="v1", instance_id="today",
                                   principal=principal),
            capability=cap, goal=f"send a {c.channel} message to {len(recipients)} recipient(s): {c.title}",
            inputs={"_outcome": "succeed" if c.connector_ok else "fail"},
            bounded_context={"case": c.key, "channel": c.channel, "recipients": recipients,
                             "subject": c.subject, "body": c.body, "attempt": c.attempt},
            permission_scope=AgentPermissionScope(ask_before_capabilities=(cap,)),
            project_id=self.project, mission_id=f"case:{c.key}", idempotency_key=f"{item_id}:{c.attempt}")

    def _state(self, c: _Case, now: float) -> tuple:
        """(state, reason, resurfaced) derived from the ledger + the clock."""
        if c.phase == "dismissed":
            return "dismissed", c.reason, False
        if c.phase == "outcome":
            return "verified", f"{c.outcome_label}.", False
        if c.phase == "failed":
            return "failed", c.failure, False
        if c.phase == "unknown":      # approved and submitted, outcome unconfirmed: not done, not failed
            return "waiting", c.failure, False
        if c.snoozed_until > now:
            return "scheduled", f"Snoozed until {time.strftime('%a %H:%M', time.gmtime(c.snoozed_until))} UTC.", False
        if c.phase == "sent":
            if now >= c.follow_up_at:
                return "needs_action", "No response yet — time to follow up.", True
            # the reason it was suggested ("Choose which Sam Rivera before anything is sent") is stale the moment it is sent
            return "waiting", self._status_line(c, "waiting"), False
        return "needs_action", c.reason, False

    def _lifecycle(self, c: _Case) -> Dict[str, bool]:
        sent = c.phase in ("sent", "outcome")
        return _blank_lifecycle(
            suggested=True, approved=bool(c.approval_ref) and c.phase != "failed", sent=sent,
            delivery_verified=sent and c.verification == "verified", outcome_observed=c.phase == "outcome")

    def _view(self, c: _Case, item_id: str, now: float) -> dict:
        state, reason, resurfaced = self._state(c, now)
        lc = self._lifecycle(c)
        label = {"sent": "Follow up", "outcome": "Done", "unknown": "Check whether it was sent"}.get(
            c.phase, f"Send {c.channel} message" if c.phase != "failed" else "Fix the connection, then retry")
        it = _item(tenant=self.tenant, project=self.project, key="case:" + c.key, dedupe=_norm(c.title),
                   title=c.title, reason=reason, state=state, score=c.score, label=label, executable=True,
                   source_refs=[e["ref"] for e in c.evidence], confidence=None, now=now,
                   kind="case", domain=c.domain, channel=c.channel, resurfaced=resurfaced,
                   deadline=c.deadline, owner=c.owner, lifecycle=lc, simulated=True, category=c.category,
                   epoch=int(max(c.snoozed_until, c.follow_up_at)) + 1000 * c.attempt,
                   evidence_refs=[e["ref"] for e in c.evidence], observed_at=max(e["observed_at"] for e in c.evidence),
                   subject_refs=[t["id"] for t in c.targets],
                   approval_ref=c.approval_ref or None, policy_decision_ref=c.decision_ref or None,
                   last_receipt_ref=(c.receipt or {}).get("receipt_id") or None,
                   verification_ref=c.sent_digest if c.verification == "verified" else None,
                   outcome_ref=("outcome:" + c.key) if c.phase == "outcome" else None,
                   status_line=self._status_line(c, state))
        it["id"] = item_id
        it["outcome_unknown"] = c.phase == "unknown"
        it["next_action"]["capability_id"] = "personal_agent.connected_app_action"
        return it

    def _status_line(self, c: _Case, state: str) -> str:
        if c.phase == "suggested":
            return "Drafted — nothing has been sent."
        if c.phase in ("failed", "unknown"):
            return c.failure
        if c.phase == "sent":
            ok = c.verification == "verified"
            return ("Sent and provider-confirmed — waiting for the customer; no reply or outcome yet."
                    if ok else "Sent, but delivery could not be confirmed.")
        if c.phase == "outcome":
            return f"{c.outcome_label}."
        return ""

    # ── the feed ────────────────────────────────────────────────────────────────────
    def feed(self, priorities: Optional[dict] = None, attention: Optional[Sequence[dict]] = None) -> dict:
        with self._lock:
            now = self.clock()
            items = project_priorities(priorities, tenant=self.tenant, project=self.project, now=now)
            items += project_attention(attention, tenant=self.tenant, project=self.project, now=now)
            items += [self._view(c, i, now) for i, c in self._cases.items() if c.surfaced]
            items += [self._reminder_view(r, i, now) for i, r in self._reminders.items()]
            merged = merge_items(items)
            counts = {s: 0 for s in STATES}
            for it in merged:
                counts[it["state"]] += 1
            n = counts["needs_action"]
            return {"project_id": self.project, "generated_at": now,
                    "basis": "sample" if self.sample_cases else (priorities or {}).get("basis", "live"),
                    "simulated_actions": self.sample_cases,
                    "headline": (f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your decision."
                                 if n else "Nothing needs you right now."),
                    "counts": counts, "items": merged,
                    "handled_automatically": (priorities or {}).get("handled_automatically", 0),
                    "abstained": (priorities or {}).get("abstained", 0)}

    # ── inspect ─────────────────────────────────────────────────────────────────────
    def item(self, item_id: str, *, targets: Optional[Sequence[str]] = None,
             identity_id: Optional[str] = None) -> dict:
        with self._lock:
            r = self._reminders.get(item_id)
            if r is not None:
                now = self.clock()
                return {**self._reminder_view(r, item_id, now), "why": "You asked to be reminded.",
                        "evidence": [{"kind": "capture", "label": f"Your note: \"{r.text}\"", "observed_at": r.created,
                                      "ref": "capture:" + r.capture_id}], "action": None, "history": list(r.events),
                        "receipt": None}
            c = self._case(item_id)
            now = self.clock()
            view = self._view(c, item_id, now)
            picked, skipped = self._select(c, targets)
            ident = self._identity(c, identity_id)
            blocked = ""
            if c.identity_candidates and ident is None:
                blocked = "Choose which contact this is before anything can be sent."
            elif not picked and not ident:
                blocked = "No eligible recipients (consent policy)."
            digest = None
            if not blocked and c.phase in ("suggested", "failed"):
                digest = self._request(c, picked, ident, item_id).intent_digest()
            sendable = c.phase in ("suggested", "failed") and not blocked
            to = ([{"id": ident["id"], "label": ident["label"], "address": ident["email"]}] if ident else
                  [{"id": t["id"], "label": t["label"], "address": t["address"]} for t in picked])
            view.update({
                # "why this matters" explains the CURRENT situation: the original reason while it is still a suggestion, the live
                # state once it has been sent / failed / closed (a UI agent caught it still saying "choose which contact before
                # anything is sent" on an item that had been sent)
                "why": c.reason if c.phase == "suggested" else view["reason"], "evidence": c.evidence,
                "action": {
                    "kind": "send_message", "channel": c.channel, "to": to, "skipped": skipped,
                    "subject": c.subject, "body": c.body, "payload_digest": digest, "attempt": c.attempt,
                    "will_happen": f"Sends {len(to)} {c.channel} message{'s' if len(to) != 1 else ''} to the "
                                   "recipients above." if to else "",
                    "nothing_sent_yet": c.phase in ("suggested", "failed"),
                    "reconcile_required": c.phase == "unknown",
                    "policy": {"decision": "requires_approval", "reason": "External message — approval is required."},
                    "blocked_reason": blocked,
                    "disambiguate": [{k: v for k, v in i.items()} for i in c.identity_candidates],
                    "can_approve": sendable and digest is not None},
                "history": list(c.events),
                "follow_up": {"at": c.follow_up_at or None, "after_seconds": c.follow_up_s},
                "receipt": c.receipt})
            return view

    # ── approve → send → verify ─────────────────────────────────────────────────────
    def approve(self, item_id: str, *, expected_digest: str, targets: Optional[Sequence[str]] = None,
                identity_id: Optional[str] = None, approver: str = "user:operator") -> dict:
        with self._lock:
            if item_id in self._reminders:
                raise TodayError(409, "not_executable", "a reminder has nothing to send")
            c = self._case(item_id)
            now = self.clock()
            if c.phase in ("sent", "outcome"):        # idempotent: a retry/double-click never re-sends
                if expected_digest != c.sent_digest:
                    raise TodayError(409, "stale_approval", "this item was already acted on with different content")
                return {**self.item(item_id), "replayed": True, "receipt": c.receipt}
            if c.phase == "dismissed":
                raise TodayError(409, "dismissed", "this item was dismissed")
            if c.phase == "unknown":
                raise TodayError(409, "reconcile_required",
                                 "the last send may have gone out — check whether it was delivered before approving again")
            picked, _ = self._select(c, targets)
            ident = self._identity(c, identity_id)
            if c.identity_candidates and ident is None:
                raise TodayError(409, "identity_ambiguous", "choose which contact this is before sending")
            if not picked and not ident:
                raise TodayError(422, "no_eligible_recipients", "no recipient passes the consent policy")
            req = self._request(c, picked, ident, item_id)
            digest = req.intent_digest()
            if digest != expected_digest:
                raise TodayError(409, "stale_approval",
                                 "the content or recipients changed since you reviewed it — review again")
            principal = req.identity.principal
            approval_id = self._bridge.present(req)
            decision = self._bridge.approve(approval_id, principal=principal, by=approver,
                                            attestation="projects-session")
            authz = self._bridge.authorize(req, principal=principal)        # single use
            c.approval_ref, c.decision_ref = approval_id, authz.decision_id
            self._log(c, "approved", f"Approved by {approver} (bound to {digest[:19]}…)")
            c.sends += 1                          # the submission is attempted from here on, whatever comes back
            try:
                out = self._operator.operator.invoke(
                    req.capability,
                    {"_outcome": req.inputs["_outcome"], "decision_id": decision.decision_id, "goal": req.goal,
                     "bounded_context": dict(req.bounded_context), "project_id": req.project_id,
                     "mission_id": req.mission_id}, req.idempotency_key)
            except Exception as exc:              # noqa: BLE001 — after the approval is consumed ANY error is ambiguous
                out = {"outcome_unknown": True, "verification": "unknown", "receipt": {},
                       "error": type(exc).__name__}
            receipt = out.get("receipt", {})
            c.receipt, c.verification = receipt, out.get("verification", "")
            status = receipt.get("status", "")
            if out.get("outcome_unknown"):
                # may have gone out: keep the SAME idempotency key (attempt unchanged) and require reconcile
                c.phase, c.unknown_key, c.unknown_digest = "unknown", req.idempotency_key, digest
                c.failure = ("Not confirmed — it may have been sent. Check delivery before trying again, "
                             "so the same message is not sent twice.")
                self._log(c, "unknown", c.failure)
            elif status == "SUCCEEDED":
                c.phase, c.sent_digest = "sent", digest
                c.follow_up_at = now + c.follow_up_s
                self._log(c, "sent", "Sent through the governed path")
                self._log(c, "verified" if c.verification == "verified" else "unverified",
                          "Provider confirmed delivery (independently verified)" if c.verification == "verified"
                          else "Delivery could not be verified")
            else:
                c.attempt += 1                        # a retry needs a fresh review + fresh digest + new approval
                c.phase = "failed"
                c.failure = ("Not sent — the connection needs attention." if status == "FAILED"
                             else "Not confirmed sent — check before retrying to avoid a duplicate.")
                self._log(c, "failed", c.failure)
            return {**self.item(item_id), "replayed": False, "receipt": receipt}

    def reconcile(self, item_id: str, *, manual: Optional[str] = None, by: str = "user:operator") -> dict:
        """Settle an approved send whose outcome is unconfirmed, from a read, before anything can be retried.

        Without ``manual`` the provider is asked (lookup by idempotency key + independent probe): found → sent,
        absent → failed (a retry is then safe and gets a fresh key), no answer → still unknown. ``manual`` is a
        person's own statement ("delivered" | "not_delivered"), recorded with who said it."""
        with self._lock:
            c = self._case(item_id)
            if c.phase != "unknown":
                raise TodayError(409, "nothing_to_reconcile", "this item has no unconfirmed send")
            now = self.clock()
            if manual is not None:
                if manual not in ("delivered", "not_delivered"):
                    raise TodayError(422, "bad_manual", "manual must be 'delivered' or 'not_delivered'")
                if not by or by == "user:operator" and not self.simulated:
                    raise TodayError(403, "manual_reconcile_needs_an_identified_approver",
                                     "a manual reconciliation must be recorded by an identified, authorised person")
                outcome = "found" if manual == "delivered" else "absent"
                why, verification = f"{by} confirmed manually: {manual.replace('_', ' ')}", "unverified"
            else:
                res = self._operator.reconcile(c.unknown_key)
                outcome, why, verification = res["outcome"], res.get("reason", ""), res.get("verification", "")
            if outcome == "found":
                c.phase, c.sent_digest = "sent", c.unknown_digest
                c.follow_up_at = now + c.follow_up_s
                c.verification = "verified" if verification == "verified" else ""
                self._log(c, "reconciled", f"Confirmed it was sent ({why})")
            elif outcome == "absent":
                c.attempt += 1                    # now known not to have gone out: a fresh review + key is safe
                c.phase, c.unknown_key, c.unknown_digest = "failed", "", ""
                c.failure = "Confirmed not sent — you can review and send again."
                self._log(c, "reconciled", f"Confirmed it was not sent ({why})")
            else:
                self._log(c, "reconcile_unknown", f"Still could not confirm ({why})")
            return {**self.item(item_id), "reconcile_outcome": outcome}

    @property
    def intake(self):
        from agentic_os.capture import Intake
        if self._intake is None:
            self._intake = Intake(self, clock=self.clock)
        return self._intake

    # ── intake hooks (agentic_os.capture routes into these; Today stays a projection) ──
    def known_cases(self) -> List[tuple]:
        """(item_id, case) for every case, surfaced or dormant — what a note can be matched against."""
        with self._lock:
            return list(self._cases.items())

    def attach_evidence(self, item_id: str, *, capture_id: str, text: str, observed_at: float) -> dict:
        """Link a capture to a case as evidence and make the case visible. Never sends or approves anything."""
        with self._lock:
            c = self._cases.get(item_id)
            if c is None:
                raise TodayError(404, "not_found", "no such case")
            ref = "capture:" + capture_id
            if not any(e["ref"] == ref for e in c.evidence):
                c.evidence.append({"kind": "capture", "label": f"Your note: \"{text}\"", "observed_at": observed_at, "ref": ref})
            if not c.surfaced:
                c.surfaced = True
                c.reason = f"You noted this is outstanding. {c.reason}"
                self._log(c, "surfaced", "Brought into Today by your note")
            self._log(c, "evidence", "Added your note as evidence")
            return {"item_id": item_id, "title": c.title, "state": self._state(c, self.clock())[0]}

    def add_reminder(self, *, capture_id: str, text: str, due: float) -> dict:
        with self._lock:
            now = self.clock()
            rid = stable_id(self.tenant, self.project, "reminder:" + capture_id)
            if rid not in self._reminders:
                r = _Reminder(text=text, due=due, capture_id=capture_id, created=now)
                r.events.append({"at": now, "kind": "created", "text": "Reminder set from your note"})
                self._reminders[rid] = r
            return {"item_id": rid, "due": due}

    def _reminder_view(self, r: _Reminder, item_id: str, now: float) -> dict:
        if r.done:
            state, reason = "dismissed", "Done."
        elif r.snoozed_until > now:
            state, reason = "scheduled", "Snoozed."
        elif now < r.due:
            state, reason = "scheduled", f"Reminder for {time.strftime('%a %d %b %H:%M', time.gmtime(r.due))} UTC."
        else:
            state, reason = "needs_action", "Your reminder is due."
        it = _item(tenant=self.tenant, project=self.project, key="reminder:" + r.capture_id, dedupe="reminder" + _norm(r.text),
                   title=f"Reminder: {r.text}", reason=reason, state=state, score=0.6, label="Mark done",
                   executable=False, source_refs=["capture:" + r.capture_id], confidence=None, now=now,
                   kind="reminder", deadline=r.due, owner=r.owner, simulated=True, observed_at=r.created,
                   category=r.category, epoch=int(max(r.snoozed_until, r.due)),
                   evidence_refs=["capture:" + r.capture_id])
        it["id"] = item_id
        it["lifecycle"] = _blank_lifecycle(suggested=True)
        return it

    # ── triage ──────────────────────────────────────────────────────────────────────
    def snooze(self, item_id: str, *, seconds: float) -> dict:
        if not (60 <= seconds <= 30 * _DAY):
            raise TodayError(422, "bad_snooze", "snooze must be between 1 minute and 30 days")
        with self._lock:
            r = self._reminders.get(item_id)
            if r is not None:
                r.snoozed_until = self.clock() + seconds
                return self.item(item_id)
            c = self._case(item_id)
            c.snoozed_until = self.clock() + seconds
            self._log(c, "snoozed", f"Snoozed for {int(seconds // 60)} minutes")
            return self.item(item_id)

    def dismiss(self, item_id: str) -> dict:
        with self._lock:
            r = self._reminders.get(item_id)
            if r is not None:
                r.done = True
                r.events.append({"at": self.clock(), "kind": "done", "text": "Marked done"})
                return self.item(item_id)
            c = self._case(item_id)
            if c.phase in ("sent", "outcome"):
                raise TodayError(409, "already_sent", "a sent item can't be dismissed; wait for the outcome")
            if c.phase == "unknown":
                raise TodayError(409, "reconcile_required", "the last send may have gone out; reconcile it first")
            c.phase = "dismissed"
            self._log(c, "dismissed", "Dismissed")
            return self.item(item_id)

    # ── batch triage: reversible actions only ────────────────────────────────────────
    def _touch_fields(self, item_id: str) -> tuple:
        """(object, field names a batch may change) for a case or reminder; refuses anything without a ledger."""
        r = self._reminders.get(item_id)
        if r is not None:
            return r, ("done", "snoozed_until", "owner", "category")
        c = self._cases.get(item_id)
        if c is not None and c.surfaced:
            return c, ("phase", "snoozed_until", "owner", "category")
        raise TodayError(409, "not_changeable", "this isn't an item that can be changed here (live signals are "
                                                "resolved where they originate)")

    def batch(self, action: str, item_ids: Sequence[str], *, seconds: Optional[float] = None,
              assignee: Optional[str] = None, category: Optional[str] = None) -> dict:
        """Snooze / dismiss / assign / categorize many items at once. NEVER approval: an external send, refund or
        payment is reviewed one exact payload at a time. Per-item results; one undo token covers what succeeded."""
        if not self.sample_cases:
            raise TodayError(501, "unsupported", "no governed case source is configured for this deployment; Today is read-only")
        if action not in BATCH_ACTIONS:
            raise TodayError(422, "batch_not_allowed",
                             "only reversible actions (snooze, dismiss, assign, categorize) can be done in a batch; "
                             "approvals are reviewed one item at a time")
        ids = list(dict.fromkeys(item_ids or []))
        if not ids:
            raise TodayError(422, "empty_batch", "select at least one item")
        if len(ids) > MAX_BATCH:
            raise TodayError(422, "too_many", f"a batch is limited to {MAX_BATCH} items")
        if action == "snooze" and not (seconds is not None and 60 <= seconds <= 30 * _DAY):
            raise TodayError(422, "bad_snooze", "snooze must be between 1 minute and 30 days")
        label = None
        if action in ("assign", "categorize"):
            raw = assignee if action == "assign" else category
            label = (raw or "").strip()
            limit = 60 if action == "assign" else 40
            if not label or len(label) > limit or any(ord(ch) < 32 for ch in label):
                raise TodayError(422, "bad_label", f"a {action} label must be 1-{limit} printable characters")
        with self._lock:
            now = self.clock()
            results, snaps = [], []
            for iid in ids:
                try:
                    obj, fields_ = self._touch_fields(iid)
                    before = {f: getattr(obj, f) for f in fields_}
                    if action == "snooze":
                        self.snooze(iid, seconds=seconds)
                    elif action == "dismiss":
                        self.dismiss(iid)
                    elif action == "assign":
                        obj.owner = label
                    else:
                        obj.category = label
                    after = {f: getattr(obj, f) for f in fields_}
                    snaps.append((iid, before, after))
                    results.append({"id": iid, "ok": True})
                except TodayError as e:
                    results.append({"id": iid, "ok": False, "error": e.code, "detail": e.detail})
            token = None
            if snaps:
                token = "undo_" + hashlib.sha256(f"{now}|{action}|{ids}".encode()).hexdigest()[:16]
                self._undo = {k: v for k, v in self._undo.items() if v["expires"] > now}       # drop expired
                self._undo[token] = {"snaps": snaps, "expires": now + UNDO_TTL_S, "action": action}
            ok = sum(1 for r in results if r["ok"])
            return {"action": action, "results": results, "ok_count": ok, "failed_count": len(results) - ok,
                    "undo": {"token": token, "expires_at": now + UNDO_TTL_S} if token else None}

    def undo(self, token: str) -> dict:
        """Reverse a batch. Each field is restored only if it still holds what the batch set — a later change by
        the user (or time) is never overwritten. Single use; expires."""
        with self._lock:
            now = self.clock()
            entry = self._undo.pop(token, None)
            if entry is None or entry["expires"] <= now:
                raise TodayError(404, "undo_unavailable", "this undo has expired or was already used")
            results = []
            for iid, before, after in entry["snaps"]:
                obj = self._reminders.get(iid) or self._cases.get(iid)
                if obj is None:
                    results.append({"id": iid, "restored": False, "reason": "gone"})
                    continue
                if any(getattr(obj, f) != v for f, v in after.items()):
                    results.append({"id": iid, "restored": False, "reason": "changed_since"})
                    continue
                for f, v in before.items():
                    setattr(obj, f, v)
                results.append({"id": iid, "restored": True})
            return {"action": entry["action"], "results": results,
                    "restored_count": sum(1 for r in results if r["restored"])}

    # ── notifications: derived from the feed, never a second workflow ────────────────
    def _quiet_now(self, now: float) -> bool:
        s, e = self._prefs["quiet_start"], self._prefs["quiet_end"]
        if s is None or e is None or s == e:
            return False
        hour = int((now + self._prefs["tz_offset_minutes"] * 60) // 3600) % 24
        return s <= hour < e if s < e else (hour >= s or hour < e)

    def notifications(self, priorities: Optional[dict] = None, attention: Optional[Sequence[dict]] = None) -> dict:
        """What would interrupt the user right now. Only items genuinely in ``needs_action`` count, so a snoozed
        or waiting item is silent until its wake condition makes it actionable again (its key then changes and
        it notifies once). ``summary`` is content-free so it is safe on a lock screen; titles are in-app only."""
        feed = self.feed(priorities, attention)
        now = feed["generated_at"]
        actionable = [i for i in feed["items"] if i["state"] == "needs_action"]
        quiet = self._quiet_now(now)
        pending = []
        for it in actionable:
            key = f"{it['id']}:{it.get('epoch', 0)}:{it['state']}"
            if key in self._seen:
                continue
            pending.append({"key": key, "id": it["id"], "title": it["user_title"], "reason": it["reason"],
                            "kind": it.get("kind", "signal"), "resurfaced": bool(it.get("resurfaced"))})
        n = len(actionable)
        silenced = quiet or not self._prefs["enabled"]
        return {"generated_at": now, "count": n,
                "summary": f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you" if n else "Nothing needs you",
                "pending": [] if silenced else pending, "suppressed": len(pending) if silenced else 0,
                "quiet_now": quiet, "prefs": dict(self._prefs)}

    def ack_notifications(self, keys: Sequence[str]) -> dict:
        if not isinstance(keys, (list, tuple)) or len(keys) > 100 or any(not isinstance(k, str) or len(k) > 120 for k in keys):
            raise TodayError(422, "bad_keys", "acknowledge up to 100 notification keys")
        with self._lock:
            now = self.clock()
            for k in keys:
                self._seen[k] = now
            while len(self._seen) > 1000:                        # bounded: forget the oldest
                self._seen.pop(next(iter(self._seen)))
            return {"acknowledged": len(keys)}

    def set_notification_prefs(self, *, enabled: Optional[bool] = None, quiet_start: Optional[int] = None,
                               quiet_end: Optional[int] = None, tz_offset_minutes: Optional[int] = None,
                               clear_quiet: bool = False) -> dict:
        with self._lock:
            if enabled is not None:
                self._prefs["enabled"] = bool(enabled)
            if tz_offset_minutes is not None:
                if not -840 <= tz_offset_minutes <= 840:
                    raise TodayError(422, "bad_prefs", "tz_offset_minutes must be within +/-14h")
                self._prefs["tz_offset_minutes"] = int(tz_offset_minutes)
            if clear_quiet:
                self._prefs["quiet_start"] = self._prefs["quiet_end"] = None
            elif quiet_start is not None or quiet_end is not None:
                if quiet_start is None or quiet_end is None or not (0 <= quiet_start <= 23 and 0 <= quiet_end <= 23):
                    raise TodayError(422, "bad_prefs", "quiet hours need a start and an end hour, 0-23")
                self._prefs["quiet_start"], self._prefs["quiet_end"] = int(quiet_start), int(quiet_end)
            return dict(self._prefs)

    def observe_outcome(self, item_id: str) -> dict:
        """SIMULATED business outcome (e.g. the customer replied). Only a sent item can have one — a send
        alone never closes work."""
        with self._lock:
            c = self._case(item_id)
            if c.phase != "sent":
                raise TodayError(409, "nothing_sent", "no outcome can be observed before a verified send")
            c.phase = "outcome"
            self._log(c, "outcome", f"{c.outcome_label} (simulated)")
            return self.item(item_id)

    def repair_connection(self, item_id: str) -> None:
        """Python-only (tests / operator tooling): the connector was re-authorised."""
        with self._lock:
            self._case(item_id).connector_ok = True


class TodaySessions:
    """Per-session sandboxes for the anonymous sample: bounded and TTL'd, so visitors never see (or mutate)
    each other's ledger and the process can't be made to grow without limit."""

    def __init__(self, *, max_sessions: int = 200, ttl_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.max, self.ttl, self.clock = max_sessions, ttl_seconds, clock
        self._s: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: str, *, tenant: str, project: str, sample_cases: bool = True) -> Today:
        now = self.clock()
        with self._lock:
            for k in [k for k, (_, ts) in self._s.items() if now - ts > self.ttl]:
                del self._s[k]
            hit = self._s.get(key)
            today = hit[0] if hit else Today(tenant=tenant, project=project, clock=self.clock,
                                             sample_cases=sample_cases)
            self._s[key] = (today, now)
            while len(self._s) > self.max:
                self._s.pop(min(self._s, key=lambda k: self._s[k][1]))
            return today
