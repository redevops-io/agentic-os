"""Historical outcome migration (moat plan WP7 / moat M2).

When a customer leaves Zendesk / Intercom / Klaviyo for the open stack, the objective is not feature parity — it is
preserving their historical *Experience*: the resolved decisions and their verified outcomes. These parsers
normalize customer-owned exports into a neutral `HistoricalOutcome` and seed the EvidenceValueStore's outcome
corpus, so the Runtime starts with real prior outcomes instead of a cold start.

Imported rows are NOT paid lookups: they carry provider `import:<source>`, cost 0, and evidence_requested=False, so
they never distort the paid-evidence metrics (§9) while still contributing verified outcomes for learning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from runtime_contracts.protocol import Capability, EvidenceValueRecord

from .value_store import EvidenceValueStore

# Normalized outcome vocabulary. "beneficial" matches EvidenceValueStore.summary()'s beneficial set.
BENEFICIAL = "beneficial"
ADVERSE = "adverse"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class HistoricalOutcome:
    """A single prior decision + its verified outcome, imported from a source system."""
    source: str                       # zendesk | intercom | klaviyo
    external_id: str
    tenant: str
    subject: str = ""                 # customer / contact / domain the decision was about
    decision: str = ""                # what was decided (route, macro, segment, …), when recoverable
    action: str = ""                  # what was done (resolved, converted, churned, …)
    outcome: str = NEUTRAL            # beneficial | adverse | neutral
    occurred_at: str = ""
    capability: Optional[Capability] = None   # capability class this experience informs, if any

    def to_value_record(self) -> EvidenceValueRecord:
        cap = self.capability or Capability.PERSON_ENRICHMENT
        return EvidenceValueRecord(
            decision_case_id=f"import:{self.source}:{self.external_id}",
            capability=cap,
            provider=f"import:{self.source}",
            evidence_requested=False,        # imported experience, not a lookup we paid for
            evidence_received=True,
            cost=0.0,
            decision_before="",
            decision_after=self.decision,
            action=self.action,
            verified_outcome=self.outcome,
        )


def _norm(v: object) -> str:
    return str(v).strip() if v is not None else ""


# ── Zendesk ──────────────────────────────────────────────────────────────────────────────────────────────
def from_zendesk(records: Iterable[dict], *, tenant: str) -> list[HistoricalOutcome]:
    """Zendesk ticket export. Outcome from CSAT when present, else resolution status."""
    out: list[HistoricalOutcome] = []
    for r in records:
        status = _norm(r.get("status")).lower()
        rating = ((r.get("satisfaction_rating") or {}).get("score") or "").lower()
        if rating == "good":
            outcome = BENEFICIAL
        elif rating == "bad":
            outcome = ADVERSE
        elif status in ("solved", "closed"):
            outcome = BENEFICIAL
        else:
            outcome = NEUTRAL
        tags = r.get("tags") or []
        out.append(HistoricalOutcome(
            source="zendesk", external_id=_norm(r.get("id")), tenant=tenant,
            subject=_norm((r.get("via") or {}).get("source", {}).get("from", {}).get("address")
                          or r.get("requester_id")),
            decision=_norm(tags[0]) if tags else "",
            action="resolved" if status in ("solved", "closed") else (status or "open"),
            outcome=outcome, occurred_at=_norm(r.get("created_at"))))
    return out


# ── Intercom ─────────────────────────────────────────────────────────────────────────────────────────────
def from_intercom(records: Iterable[dict], *, tenant: str) -> list[HistoricalOutcome]:
    """Intercom conversation export. Outcome from the 1–5 conversation rating, else close state."""
    out: list[HistoricalOutcome] = []
    for r in records:
        state = _norm(r.get("state")).lower()
        rating = (r.get("conversation_rating") or {}).get("rating")
        if isinstance(rating, (int, float)):
            outcome = BENEFICIAL if rating >= 4 else ADVERSE if rating <= 2 else NEUTRAL
        elif state == "closed":
            outcome = BENEFICIAL
        else:
            outcome = NEUTRAL
        contacts = (r.get("contacts") or {}).get("contacts") or r.get("contacts") or []
        subject = ""
        if isinstance(contacts, list) and contacts:
            subject = _norm(contacts[0].get("email") or contacts[0].get("id"))
        out.append(HistoricalOutcome(
            source="intercom", external_id=_norm(r.get("id")), tenant=tenant, subject=subject,
            decision=_norm((r.get("custom_attributes") or {}).get("route")),
            action="closed" if state == "closed" else (state or "open"),
            outcome=outcome, occurred_at=_norm(r.get("created_at"))))
    return out


# ── Klaviyo ──────────────────────────────────────────────────────────────────────────────────────────────
# Metric names that indicate a beneficial vs adverse marketing outcome. Overridable per tenant.
KLAVIYO_BENEFICIAL = frozenset({"placed order", "ordered product", "started checkout", "clicked email"})
KLAVIYO_ADVERSE = frozenset({"unsubscribed", "marked email as spam", "bounced email"})


def from_klaviyo(records: Iterable[dict], *, tenant: str,
                 beneficial: frozenset[str] = KLAVIYO_BENEFICIAL,
                 adverse: frozenset[str] = KLAVIYO_ADVERSE) -> list[HistoricalOutcome]:
    """Klaviyo event export. Outcome from the event metric name against the beneficial/adverse sets."""
    out: list[HistoricalOutcome] = []
    for r in records:
        metric = _norm(r.get("metric") or (r.get("attributes") or {}).get("metric")).lower()
        if metric in beneficial:
            outcome = BENEFICIAL
        elif metric in adverse:
            outcome = ADVERSE
        else:
            outcome = NEUTRAL
        profile = r.get("profile") or {}
        out.append(HistoricalOutcome(
            source="klaviyo", external_id=_norm(r.get("id")), tenant=tenant,
            subject=_norm(profile.get("email") or r.get("email")),
            decision=_norm(r.get("campaign") or r.get("flow")),
            action=metric or "event",
            outcome=outcome, occurred_at=_norm(r.get("timestamp") or r.get("datetime")),
            capability=Capability.PERSON_ENRICHMENT))
    return out


def seed_value_store(store: EvidenceValueStore, outcomes: Iterable[HistoricalOutcome]) -> int:
    """Append imported historical outcomes to the value store's outcome corpus. Returns the count written."""
    n = 0
    for o in outcomes:
        store.append(o.to_value_record())
        n += 1
    return n
