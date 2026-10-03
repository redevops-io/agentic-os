"""WhatsApp collaboration adapter — over the WhatsApp Business Cloud API (Meta Graph).

There is no official Meta MCP for WhatsApp; the Cloud API (Graph) is the supported path. WhatsApp is a messaging
surface, so it implements the common `CollaborationProvider`: outbound messages/replies/reactions/interactive
prompts to a recipient, and inbound webhook events normalized to the shared `InboundEvent`. Credential-gated (a
WABA access token + the sender phone-number id) and offline-testable via an injected transport.

The `channel` in this contract is the RECIPIENT phone number (E.164); the sender is the configured
`phone_number_id`. A `thread` is the message id to reply in context of (WhatsApp has no thread objects).
"""
from __future__ import annotations

from typing import Callable, Optional

from .contracts import Attachment, InboundEvent, InteractionResult, OutboundMessage

Transport = Callable[[str, str, Optional[dict], Optional[dict]], dict]

_GRAPH = "https://graph.facebook.com/v21.0"


def _http(method: str, url: str, token: str, payload: Optional[dict]) -> dict:
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed Graph host
        body = r.read().decode("utf-8")
        return json.loads(body) if body else {}


class WhatsAppCollaborationProvider:
    provider = "whatsapp"

    def __init__(self, access_token: str = "", phone_number_id: str = "", *, transport: Transport = _http):
        self._token = access_token
        self._pnid = phone_number_id
        self._t = transport

    def entitled(self) -> bool:
        return bool(self._token and self._pnid)

    def _send(self, payload: dict) -> str:
        payload = {"messaging_product": "whatsapp", **payload}
        resp = self._t("POST", f"{_GRAPH}/{self._pnid}/messages", self._token, payload)
        msgs = resp.get("messages") or []
        return msgs[0].get("id", "") if msgs else ""

    # ── inbound ──────────────────────────────────────────────────────────────────────────────────────────
    def normalize_event(self, raw: dict) -> InboundEvent:
        """Normalize a WhatsApp webhook (entry[].changes[].value.messages[]) to an InboundEvent."""
        value = {}
        try:
            value = raw["entry"][0]["changes"][0]["value"]
        except (KeyError, IndexError, TypeError):
            value = raw.get("value", raw) if isinstance(raw, dict) else {}
        msgs = value.get("messages") or []
        m = msgs[0] if msgs else {}
        body = (m.get("text", {}) or {}).get("body", "") or ""
        atts = ()
        for kind in ("image", "document", "audio", "video"):
            if kind in m:
                a = m[kind] or {}
                atts = (Attachment(name=a.get("filename", kind), url=a.get("id", ""),
                                   mime=a.get("mime_type", "")),)
                break
        ctx = (m.get("context", {}) or {}).get("id", "")
        return InboundEvent(
            provider=self.provider,
            workspace=(value.get("metadata", {}) or {}).get("phone_number_id", self._pnid),
            channel=m.get("from", ""), actor=m.get("from", ""), text=body,
            thread=ctx, message_id=m.get("id", ""), attachments=atts, ts=m.get("timestamp", ""), raw=raw)

    def read_thread(self, channel: str, thread: str, *, limit: int = 50) -> list[InboundEvent]:
        return []   # the Cloud API has no server-side thread/history read; conversations are webhook-driven

    # ── outbound / actions ───────────────────────────────────────────────────────────────────────────────
    def post_message(self, msg: OutboundMessage) -> str:
        payload: dict = {"to": msg.channel, "type": "text", "text": {"body": msg.text}}
        if msg.thread:
            payload["context"] = {"message_id": msg.thread}
        return self._send(payload)

    def reply_thread(self, channel: str, thread: str, text: str) -> str:
        return self.post_message(OutboundMessage(channel=channel, text=text, thread=thread))

    def update_message(self, channel: str, message_id: str, text: str) -> str:
        return ""   # WhatsApp messages are immutable — no edit

    def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool:
        self._send({"to": channel, "type": "reaction",
                    "reaction": {"message_id": message_id, "emoji": emoji.strip(":")}})
        return True

    def upload_file(self, channel: str, name: str, content: bytes, *, thread: str = "") -> str:
        return ""   # media upload is a separate /media 2-step flow; scaffold stub keeps the contract no-network

    def resolve_user(self, handle: str) -> str:
        return handle   # WhatsApp identities are E.164 phone numbers

    def resolve_channel(self, name: str) -> str:
        return name

    def open_interaction(self, channel: str, prompt: str, *, actions: tuple = ()) -> InteractionResult:
        """Send an interactive reply-button message; the tap returns as an inbound event the workflow's
        human-gate node consumes."""
        acts = actions or ("Approve", "Reject")
        buttons = [{"type": "reply", "reply": {"id": f"act_{a}", "title": a}} for a in acts[:3]]
        try:
            mid = self._send({"to": channel, "type": "interactive",
                              "interactive": {"type": "button", "body": {"text": prompt},
                                              "action": {"buttons": buttons}}})
            return InteractionResult(ok=bool(mid), message_id=mid)
        except Exception as e:  # noqa: BLE001
            return InteractionResult(ok=False, detail=str(e))
