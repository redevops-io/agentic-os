"""Quick Capture — a durable intake, not another way to create Projects, Missions or approvals.

    Capture : something happened, someone asked for something, or the user needs to remember something.
    Today   : something now requires the user's attention.            (a projection — see agentic_os.today)
    Mission : an action or investigation needs governed execution.
    Project : related work needs durable organisation and history.

A :class:`CaptureItem` keeps the user's original words intact and changes only through an append-only event
history. Classification is deterministic and rule-based (no model is called, and the result says so); what
the system *understood* is an annotation on the capture, never a replacement for it. Routing is explicit:

    "Remind me to call John Tuesday"        -> a scheduled reminder (appears in Today when it is due)
    "Taylor still hasn't sent the passport" -> linked as evidence to the matching case, which then surfaces
    "Send the quote to Acme"                -> linked to the matching case (its draft still needs approval)
    "Investigate why invoice 123 is unpaid" -> an investigation candidate, kept in Intake, NOT in Today
    anything unclear / ambiguous            -> Intake, awaiting one clarifying answer

Nothing here sends, approves, executes or creates a Mission. A capture reaches Today only when a real next
action exists and enough evidence ties it to a case.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

SOURCES = ("manual", "sidekick", "email", "whatsapp", "connector")
STATUSES = ("new", "needs_clarification", "classified", "archived")
MAX_CONTENT = 2000
MAX_PER_SESSION = 200
DEDUP_WINDOW_S = 24 * 3600.0
MATCH_THRESHOLD = 0.6
AMBIGUITY_MARGIN = 0.1
_DAY = 86400.0

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "on", "in", "at", "is", "it", "my", "me", "i", "we",
         "they", "he", "she", "this", "that", "has", "have", "had", "not", "yet", "still", "send", "remind"}


class CaptureError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s#-]", "", (s or "").lower())).strip()


def _words(s: str) -> List[str]:
    return re.findall(r"[a-z0-9#-]+", (s or "").lower())


# ── deterministic understanding ───────────────────────────────────────────────────────────
def parse_due(text: str, now: float) -> Optional[float]:
    """'tuesday', 'tomorrow', 'tonight', 'in 3 days', 'next week' -> a UTC timestamp (09:00 unless relative), or None."""
    t = (text or "").lower()
    day0 = now - (now % _DAY)                                   # today 00:00 UTC
    m = re.search(r"\bin\s+(\d{1,3})\s*(minute|min|hour|hr|day|week)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now + n * {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": _DAY, "week": 7 * _DAY}[unit]
    if re.search(r"\btomorrow\b", t):
        return day0 + _DAY + 9 * 3600
    if re.search(r"\b(today|tonight)\b", t):
        due = day0 + 17 * 3600
        return due if due > now else day0 + _DAY + 9 * 3600
    if re.search(r"\bnext week\b", t):
        return day0 + 7 * _DAY + 9 * 3600
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t) or re.search(rf"\b{name[:3]}\b", t):
            today_wd = time.gmtime(now).tm_wday
            ahead = (i - today_wd) % 7 or 7                     # strictly in the future
            return day0 + ahead * _DAY + 9 * 3600
    return None


def classify(text: str, *, now: float, known_entities: Sequence[str] = ()) -> dict:
    """Rule-based intent + entities + due. Returns {intent, confidence, entities, due, topics, reasons, method}."""
    t = (text or "").strip()
    low = t.lower()
    words = set(_words(low))
    entities = [e for e in known_entities if re.search(rf"\b{re.escape(e.lower())}\b", low)]
    reasons: List[str] = []
    out: Dict[str, Any] = {"entities": entities, "due": None, "method": "rules (no model)", "topics": sorted(words - _STOP)}
    m = re.match(r"^\s*(please\s+)?remind me\s+(to|about|that)?\s*(.+)$", low)
    if m:
        body = re.sub(r"^\s*(please\s+)?remind me\s+(to|about|that)?\s*", "", t, flags=re.I).strip()
        due = parse_due(low, now)
        out.update(intent="reminder", confidence=0.9 if due else 0.55, due=due, reminder_text=body)
        reasons.append('starts with "remind me"' + ("" if due else "; no time given"))
    elif re.match(r"^\s*(please\s+)?(send|email|text|message|whatsapp|forward)\b", low):
        out.update(intent="draft_action", confidence=0.8)
        reasons.append("an instruction to send something")
    elif re.match(r"^\s*(please\s+)?(investigate|find out|look into|figure out)\b", low) or re.match(r"^\s*why\b", low):
        out.update(intent="investigation", confidence=0.8)
        reasons.append("asks why / to investigate")
    elif re.search(r"\b(still|hasn'?t|haven'?t|not yet|never|missing|no reply|didn'?t|did not|waiting (for|on)|"
                   r"asked for|received|sent me)\b", low) and (entities or words & {"passport", "visa", "documents",
                                                                                   "docs", "invoice", "quote", "payment"}):
        out.update(intent="case_update", confidence=0.85 if entities else 0.65)
        reasons.append("reports the state of something outstanding")
    else:
        out.update(intent="unclear", confidence=0.2)
        reasons.append("no recognised pattern")
    if entities:
        reasons.append("mentions " + ", ".join(entities))
    out["reasons"] = reasons
    return out


def match_cases(text: str, cases: Sequence[tuple], classification: dict) -> List[dict]:
    """Score each known case against a note. entity (0.6) + topic (0.4). Highest first, score >= 0 only."""
    ents = {e.lower() for e in classification.get("entities", [])}
    words = set(_words(text)) - _STOP
    out = []
    for item_id, c in cases:
        names = {w for t in c.targets for w in _words(t["label"])} | {w for t in c.identity_candidates for w in _words(t["label"])}
        ent = 1.0 if ents & names else 0.0
        topic = 1.0 if words & set(c.topics) else 0.0
        score = round(0.6 * ent + 0.4 * topic, 2)
        if score > 0:
            out.append({"case_id": item_id, "label": c.title, "score": score, "entity": bool(ent), "topic": bool(topic)})
    return sorted(out, key=lambda m: (-m["score"], m["case_id"]))


# ── the capture object ────────────────────────────────────────────────────────────────────
@dataclass
class CaptureItem:
    id: str
    tenant_id: str
    actor_id: str
    source: str
    content: str                          # the ORIGINAL words — never rewritten
    captured_at: float
    observed_at: float
    dedup_key: str
    attachments: tuple = ()
    source_refs: tuple = ()
    status: str = "new"
    classification: Optional[dict] = None
    suggested_action: Optional[dict] = None
    question: Optional[dict] = None
    linked_entity_refs: List[str] = field(default_factory=list)
    linked_project_id: str = ""
    linked_mission_id: Optional[str] = None
    evidence_refs: List[str] = field(default_factory=list)
    routing: Optional[dict] = None
    version: int = 1
    events: List[dict] = field(default_factory=list)

    def view(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "id", "tenant_id", "actor_id", "source", "content", "captured_at", "observed_at", "status",
            "classification", "suggested_action", "question", "linked_entity_refs", "linked_project_id",
            "linked_mission_id", "evidence_refs", "routing", "dedup_key", "version")}
        d["attachments"], d["source_refs"] = list(self.attachments), list(self.source_refs)
        d["events"] = list(self.events)
        return d


class Intake:
    """One tenant's capture store, routing into a :class:`agentic_os.today.Today`. In memory (sample), bounded."""

    def __init__(self, today: Any, *, actor: str = "user:operator", clock: Callable[[], float] = time.time) -> None:
        self.today, self.actor, self.clock = today, actor, clock
        self._items: Dict[str, CaptureItem] = {}
        self._lock = threading.RLock()

    def _event(self, item: CaptureItem, kind: str, text: str) -> None:
        item.events.append({"at": self.clock(), "kind": kind, "text": text, "version": item.version})
        item.version += 1

    def _entities(self) -> List[str]:
        names = set()
        for _, c in self.today.known_cases():
            for t in list(c.targets) + list(c.identity_candidates):
                for w in re.findall(r"[A-Za-z][A-Za-z'-]+", t["label"]):
                    if len(w) > 2 and w.lower() not in _STOP:
                        names.add(w)
        return sorted(names)

    def capture(self, content: str, *, source: str = "manual", observed_at: Optional[float] = None,
                attachments: Sequence[Any] = (), source_refs: Sequence[str] = ()) -> dict:
        text = (content or "").strip()
        if not text:
            raise CaptureError(422, "empty", "write something to capture")
        if len(text) > MAX_CONTENT:
            raise CaptureError(413, "too_long", f"a capture is limited to {MAX_CONTENT} characters")
        if source not in SOURCES:
            raise CaptureError(422, "bad_source", f"source must be one of {', '.join(SOURCES)}")
        if attachments:
            raise CaptureError(422, "attachments_unsupported", "attachments are not supported yet")
        with self._lock:
            now = self.clock()
            key = hashlib.sha256(f"{self.today.tenant}|{self.actor}|{source}|{_norm_text(text)}".encode()).hexdigest()[:24]
            prior = next((i for i in self._items.values() if i.dedup_key == key and now - i.captured_at < DEDUP_WINDOW_S
                          and i.status != "archived"), None)
            if prior is not None:                              # same words, same place, same day: not a second capture
                self._event(prior, "duplicate", "Captured again — kept the original")
                return {**prior.view(), "duplicate": True}
            if len(self._items) >= MAX_PER_SESSION:
                raise CaptureError(429, "too_many", "too many captures in this session")
            item = CaptureItem(id="cap_" + hashlib.sha256(f"{key}|{now}|{len(self._items)}".encode()).hexdigest()[:14],
                               tenant_id=self.today.tenant, actor_id=self.actor, source=source, content=text,
                               captured_at=now, observed_at=observed_at if observed_at is not None else now,
                               dedup_key=key, attachments=(), source_refs=tuple(source_refs),
                               linked_project_id=self.today.project)
            self._items[item.id] = item
            self._event(item, "captured", f"Captured from {source}")
            item.classification = classify(text, now=now, known_entities=self._entities())
            self._event(item, "classified", f"Understood as {item.classification['intent']} "
                                            f"({item.classification['method']})")
            self._route(item)
            return {**item.view(), "duplicate": False}

    # ── routing ─────────────────────────────────────────────────────────────────────────
    def _route(self, item: CaptureItem) -> None:
        cl = item.classification or {}
        intent = cl.get("intent")
        now = self.clock()
        if intent == "reminder":
            if cl.get("due") is None:
                return self._ask(item, "When should I remind you?", [], "reminder")
            r = self.today.add_reminder(capture_id=item.id, text=cl.get("reminder_text") or item.content, due=cl["due"])
            item.status, item.linked_entity_refs = "classified", [r["item_id"]]
            item.suggested_action = {"kind": "reminder", "due": cl["due"]}
            item.routing = {"kind": "reminder", "item_id": r["item_id"], "due": cl["due"], "in_today_now": cl["due"] <= now}
            self._event(item, "routed", "Scheduled a reminder")
        elif intent == "investigation":
            item.status = "classified"
            item.suggested_action = {"kind": "investigate", "label": "Investigate: " + item.content}
            item.routing = {"kind": "investigation_candidate", "in_today_now": False}
            self._event(item, "routed", "Kept as an investigation candidate (not in Today)")
        elif intent in ("case_update", "draft_action"):
            matches = match_cases(item.content, self.today.known_cases(), cl)
            good = [m for m in matches if m["score"] >= MATCH_THRESHOLD]
            if not good:
                return self._ask(item, "Which case is this about?" if matches else
                                 "I couldn't tie this to an existing case. Who or what is it about?",
                                 matches[:4], "match")
            if len(good) > 1 and good[0]["score"] - good[1]["score"] <= AMBIGUITY_MARGIN:
                return self._ask(item, "This could be more than one case — which one?", good[:4], "match")
            self._link(item, good[0]["case_id"])
        else:
            self._ask(item, "I wasn't sure what to do with this. Is it a reminder, an update on a case, or something to send?",
                      [], "unclear")

    def _ask(self, item: CaptureItem, text: str, options: Sequence[dict], why: str) -> None:
        item.status = "needs_clarification"
        item.question = {"text": text, "why": why, "options": [{"case_id": o["case_id"], "label": o["label"]} for o in options]}
        item.routing = {"kind": "intake", "in_today_now": False}
        self._event(item, "needs_clarification", text)

    def _link(self, item: CaptureItem, case_id: str) -> None:
        got = self.today.attach_evidence(case_id, capture_id=item.id, text=item.content, observed_at=item.observed_at)
        item.status, item.question = "classified", None
        item.linked_entity_refs = [case_id]
        item.evidence_refs = ["capture:" + item.id]
        intent = (item.classification or {}).get("intent")
        item.suggested_action = {"kind": "review_case", "label": "Review the draft and approve it" if intent == "draft_action"
                                 else "Review the suggested follow-up", "approval_required": True}
        item.routing = {"kind": "case", "item_id": case_id, "title": got["title"], "in_today_now": True}
        self._event(item, "routed", f"Linked to “{got['title']}” as evidence")

    # ── clarification / housekeeping ────────────────────────────────────────────────────
    def resolve(self, capture_id: str, case_id: str) -> dict:
        with self._lock:
            item = self._get(capture_id)
            if item.status != "needs_clarification" or not item.question or not item.question["options"]:
                raise CaptureError(409, "nothing_to_resolve", "this capture is not waiting on a case choice")
            if case_id not in {o["case_id"] for o in item.question["options"]}:
                raise CaptureError(422, "not_an_option", "choose one of the suggested cases")
            self._event(item, "resolved", "You chose the case")
            self._link(item, case_id)
            return item.view()

    def archive(self, capture_id: str) -> dict:
        with self._lock:
            item = self._get(capture_id)
            item.status = "archived"
            self._event(item, "archived", "Archived")
            return item.view()

    def _get(self, capture_id: str) -> CaptureItem:
        item = self._items.get(capture_id)
        if item is None:
            raise CaptureError(404, "not_found", "no such capture")
        return item

    def get(self, capture_id: str) -> dict:
        with self._lock:
            return self._get(capture_id).view()

    def list(self, *, status: Optional[str] = None) -> List[dict]:
        with self._lock:
            items = [i for i in self._items.values() if i.status != "archived" and (status is None or i.status == status)]
            return [i.view() for i in sorted(items, key=lambda i: -i.captured_at)]
