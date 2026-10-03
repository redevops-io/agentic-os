"""Google Chat collaboration adapter (plan §6.4) — over the Google Chat REST API.

Implements the common `CollaborationProvider` against the Chat API (``/v1/spaces/{space}/messages`` + threads,
reactions). Credential-gated (an OAuth bearer token) and offline-testable via the injected transport. The
channel ref is a Chat space resource name (``"spaces/AAAA"``); threads are thread resource names
(``"spaces/AAAA/threads/BBBB"``). Reuses the existing Google OAuth account representation (plan §6.4).
"""
from __future__ import annotations

from typing import Callable, Optional

from .contracts import Attachment, InboundEvent, InteractionResult, OutboundMessage

Transport = Callable[[str, str, str, Optional[dict]], dict]

_CHAT = "https://chat.googleapis.com/v1"


def _http(method: str, url: str, token: str, payload: Optional[dict]) -> dict:
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed Chat host
        body = r.read().decode("utf-8")
        return json.loads(body) if body else {}


class GoogleChatCollaborationProvider:
    provider = "google_chat"

    def __init__(self, access_token: str = "", *, transport: Transport = _http):
        self._token = access_token
        self._t = transport

    def entitled(self) -> bool:
        return bool(self._token)

    # ── inbound ──────────────────────────────────────────────────────────────────────────────────────────
    def normalize_event(self, raw: dict) -> InboundEvent:
        """Normalize a Chat event (``{type:'MESSAGE', space, message, user}``) to an InboundEvent."""
        m = raw.get("message", raw) or {}
        space = (m.get("space", {}) or raw.get("space", {}) or {}).get("name", "")
        sender = m.get("sender", {}) or raw.get("user", {}) or {}
        thread = (m.get("thread", {}) or {}).get("name", "")
        mentions = tuple(str(((a.get("userMention", {}) or {}).get("user", {}) or {}).get("name", ""))
                         for a in (m.get("annotations") or []) if a.get("type") == "USER_MENTION")
        atts = tuple(Attachment(name=a.get("name", ""), url=(a.get("attachmentDataRef", {}) or {}).get(
            "resourceName", ""), mime=a.get("contentType", "")) for a in (m.get("attachment") or []))
        return InboundEvent(
            provider=self.provider, workspace=space.split("/")[-1] if space else "",
            channel=space, actor=sender.get("name", ""), text=m.get("text", "") or "",
            thread=thread, message_id=m.get("name", ""), mentions=tuple(x for x in mentions if x),
            attachments=atts, ts=m.get("createTime", ""), raw=raw)

    def read_thread(self, channel: str, thread: str, *, limit: int = 50) -> list[InboundEvent]:
        # list messages in the space, filtered to the thread (Chat has no direct thread-read; filter client-side)
        resp = self._t("GET", f"{_CHAT}/{channel}/messages?pageSize={limit}", self._token, None)
        out = []
        for m in resp.get("messages", []) or []:
            if not thread or (m.get("thread", {}) or {}).get("name") == thread:
                out.append(self.normalize_event({"message": {**m, "space": {"name": channel}}}))
        return out

    # ── outbound / actions ───────────────────────────────────────────────────────────────────────────────
    def post_message(self, msg: OutboundMessage) -> str:
        url = f"{_CHAT}/{msg.channel}/messages"
        body: dict = {"text": msg.text}
        if msg.thread:
            url += "?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            body["thread"] = {"name": msg.thread}
        if msg.blocks:
            body["cardsV2"] = list(msg.blocks)
        return str(self._t("POST", url, self._token, body).get("name", ""))

    def reply_thread(self, channel: str, thread: str, text: str) -> str:
        return self.post_message(OutboundMessage(channel=channel, text=text, thread=thread))

    def update_message(self, channel: str, message_id: str, text: str) -> str:
        self._t("PATCH", f"{_CHAT}/{message_id}?updateMask=text", self._token, {"text": text})
        return message_id

    def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool:
        url = f"{_CHAT}/{message_id}/reactions"
        self._t("POST", url, self._token, {"emoji": {"unicode": emoji.strip(":")}})
        return True

    def upload_file(self, channel: str, name: str, content: bytes, *, thread: str = "") -> str:
        # Chat media upload is a separate /media endpoint; scaffold stub keeps the contract no-network. TODO.
        return ""

    def resolve_user(self, handle: str) -> str:
        return handle if handle.startswith("users/") else (f"users/{handle}" if handle else "")

    def resolve_channel(self, name: str) -> str:
        return name if name.startswith("spaces/") else (f"spaces/{name}" if name else "")

    def open_interaction(self, channel: str, prompt: str, *, actions: tuple = ()) -> InteractionResult:
        """Post a card with buttons (cardsV2); the button callback returns as an inbound event the workflow's
        human-gate node consumes. Scaffold posts the prompt + card."""
        acts = actions or ("Approve", "Reject")
        card = {"cardId": "approval", "card": {"sections": [{"widgets": [
            {"textParagraph": {"text": prompt}},
            {"buttonList": {"buttons": [{"text": a, "onClick": {"action": {"function": f"act_{a}"}}}
                                        for a in acts]}}]}]}}
        try:
            url = f"{_CHAT}/{channel}/messages"
            mid = str(self._t("POST", url, self._token, {"text": prompt, "cardsV2": [card]}).get("name", ""))
            return InteractionResult(ok=True, message_id=mid)
        except Exception as e:  # noqa: BLE001
            return InteractionResult(ok=False, detail=str(e))
