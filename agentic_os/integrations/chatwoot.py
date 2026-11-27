"""Chatwoot conversation sensor — inbound commercial intent (Revenue plan §6, §7, §21).

A thin, READ-ONLY client over a real Chatwoot core plus a scan that turns open conversations into revenue
leakage: each conversation's opening customer message is classified (`intent.classify_intent`), and a
QUOTE_REQUEST with no matching open quotation surfaces as an UNANSWERED_QUOTE_INTENT leakage (§6). Read-only
by design — nothing here posts a reply (a public reply is drafted by the approval-gated execution adapter,
never a sensor).

Self-skips cleanly (unreachable / unauthorized → empty), unit-testable with a stub, and live against a real
Chatwoot with `CHATWOOT_API_URL` + `CHATWOOT_API_TOKEN` + `CHATWOOT_ACCOUNT_ID` (`chatwoot_from_env`). The
auth header + `/api/v1/accounts/{id}` idioms mirror `apps/support/core.py`, the support agent's client.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from agentic_os.revenue.contact_intake import ContactUpsertProposal, propose_contact_from_classification
from agentic_os.revenue.intent import classify_intent
from agentic_os.revenue.leakage import RevenueLeakage, unanswered_quote_intent


def _first_inbound_text(conv: dict) -> str:
    """The customer's opening message (message_type 0 == incoming) — what the intent is read from."""
    for m in conv.get("messages") or []:
        if m.get("message_type") == 0 and (m.get("content") or "").strip():
            return m["content"].strip()
    last = conv.get("last_non_activity_message") or {}
    return (last.get("content") or "").strip()


def _sender(conv: dict) -> dict:
    meta = conv.get("meta") or {}
    return meta.get("sender") or {}


def _subject(conv: dict) -> str:
    """A stable subject for the leakage: the contact's name/email, else the conversation id."""
    sender = _sender(conv)
    return str(sender.get("name") or sender.get("email") or f"conversation:{conv.get('id', '')}")


class ChatwootReadError(Exception):
    """A read of Chatwoot failed. The text names what was read and the status, never a body or a credential."""


@dataclass
class ChatwootClient:
    base_url: str
    api_token: str
    account_id: str = "1"
    timeout: float = 10.0

    def _headers(self) -> dict:
        return {"api_access_token": self.api_token, "Content-Type": "application/json"}

    def _acct_base(self) -> str:
        return f"{self.base_url}/api/v1/accounts/{self.account_id}"

    def connected(self) -> bool:
        import httpx
        try:
            r = httpx.get(f"{self._acct_base()}/conversations", headers=self._headers(),
                          params={"status": "open"}, timeout=4.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _read(self, path: str, params: dict) -> dict:
        import httpx
        try:
            r = httpx.get(f"{self._acct_base()}{path}", headers=self._headers(), params=params, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise ChatwootReadError(f"{path}: could not be reached ({type(e).__name__})") from None
        if r.status_code >= 400:
            raise ChatwootReadError(f"{path}: the application answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise ChatwootReadError(f"{path}: the answer could not be read") from None

    def read_conversations(self, *, status: str = "all", max_pages: int = 200) -> List[dict]:
        """Every conversation in a status (``all`` includes resolved), following pagination. RAISES on a failed page, and when the collection is longer than ``max_pages``
        pages; ``conversations`` returns what it had and hides the failure."""
        out: List[dict] = []
        for page in range(1, max_pages + 1):
            payload = ((self._read("/conversations", {"status": status, "page": page}).get("data") or {}).get("payload")) or []
            out.extend(payload)
            if not payload:
                return out
        raise ChatwootReadError(f"/conversations: more than {max_pages} pages; the read was cut")

    def read_messages(self, conversation_id: int, *, max_pages: int = 500) -> List[dict]:
        """Every message of a conversation, oldest first, private notes included and marked (``private``). Pages backwards with ``before``; RAISES on a failed page."""
        out: List[dict] = []
        before: Optional[int] = None
        for _ in range(max_pages):
            params = {"before": before} if before else {}
            payload = self._read(f"/conversations/{int(conversation_id)}/messages", params).get("payload") or []
            if not payload:
                break
            out.extend(payload)
            oldest = min(int(m["id"]) for m in payload)
            if before is not None and oldest >= before:
                break
            before = oldest
        else:
            raise ChatwootReadError(f"/conversations/{conversation_id}/messages: more than {max_pages} pages; the read was cut")
        seen, uniq = set(), []
        for m in out:
            if m["id"] not in seen:
                seen.add(m["id"])
                uniq.append(m)
        return sorted(uniq, key=lambda m: (int(m.get("created_at") or 0), int(m["id"])))

    def conversations(self, *, status: str = "open", max_pages: int = 20) -> List[dict]:
        """Conversations in a status, following pagination. Empty on any failure (self-skip)."""
        import httpx
        if not self.api_token:
            return []
        out: List[dict] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                for page in range(1, max_pages + 1):
                    r = client.get(f"{self._acct_base()}/conversations", headers=self._headers(),
                                   params={"status": status, "page": page})
                    if r.status_code >= 400:
                        break
                    payload = (r.json().get("data", {}) or {}).get("payload", []) or []
                    out.extend(payload)
                    if not payload:
                        break
        except Exception:  # noqa: BLE001
            return out
        return out


def scan_unanswered_quotes(client: ChatwootClient, *, now_ms: int,
                           has_open_quote: Optional[Callable[[str], bool]] = None,
                           min_confidence: float = 0.6, status: str = "open") -> List[RevenueLeakage]:
    """Read open conversations and surface QUOTE_REQUESTs with no matching open quotation as leakage.

    `has_open_quote(subject) -> bool` cross-references the quoting system (ERPNext) so an already-quoted
    request isn't re-flagged; without it, no open quote is assumed (everything actionable is surfaced).
    """
    check = has_open_quote or (lambda _s: False)
    out: List[RevenueLeakage] = []
    for conv in client.conversations(status=status):
        text = _first_inbound_text(conv)
        if not text:
            continue
        ic = classify_intent(text, source="chatwoot", source_ref=str(conv.get("id", "")),
                             observed_at_ms=now_ms)
        if not ic.is_actionable_quote or ic.confidence < min_confidence:
            continue
        subject = _subject(conv)
        leak = unanswered_quote_intent(
            subject, intent=ic.intent.value, has_open_quote=check(subject), confidence=ic.confidence,
            observation_refs=(f"chatwoot:conversation:{conv.get('id', '')}",))
        if leak is not None:
            out.append(leak)
    return out


def scan_inbound_contacts(client: ChatwootClient, *, now_ms: int, status: str = "open",
                          min_confidence: float = 0.6) -> List[ContactUpsertProposal]:
    """Read open conversations and propose a Contact upsert for each sender with a real email and an
    actionable commercial-intent opening message (Stage E's `Inbound event -> intent classification ->
    validated Contact upsert` hop, 2026-11-16). Still read-only: this only proposes — the caller hands
    proposals to the approval-gated `integrations.twenty_operator`, which is the only thing that writes.
    """
    out: List[ContactUpsertProposal] = []
    for conv in client.conversations(status=status):
        text = _first_inbound_text(conv)
        sender = _sender(conv)
        email = str(sender.get("email") or "").strip()
        name = str(sender.get("name") or "").strip()
        if not text or not email:
            continue
        ic = classify_intent(text, source="chatwoot", source_ref=str(conv.get("id", "")), observed_at_ms=now_ms)
        if ic.confidence < min_confidence:
            continue
        proposal = propose_contact_from_classification(ic, name=name, email=email)
        if proposal is not None:
            out.append(proposal)
    return out


def chatwoot_from_env() -> Optional[ChatwootClient]:
    url = os.environ.get("CHATWOOT_API_URL", "").rstrip("/")
    token = os.environ.get("CHATWOOT_API_TOKEN", "")
    account = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")
    return ChatwootClient(base_url=url, api_token=token, account_id=account) if (url and token) else None
