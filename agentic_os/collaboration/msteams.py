"""Microsoft Teams collaboration adapter (plan §6.3) — over Microsoft Graph.

Implements the common `CollaborationProvider` against Microsoft Graph (``/teams/{team}/channels/{channel}/
messages`` + replies, reactions, user/channel lookup). Credential-gated (an OAuth bearer token) and offline-
testable via the injected transport. The channel ref is the composite ``"{teamId}/{channelId}"`` (or
``"chat/{chatId}"`` for a group chat); the adapter splits it to build the Graph URL.

Per plan §19.3, Teams is the surface that rides the M365 Agents SDK / A2A + remote MCP for *agent* delegation;
this adapter is the messaging baseline that contract. The A2A/external-agent path is a separate
`ExternalAgentProvider` (plan §9), not this connector.
"""
from __future__ import annotations

from typing import Callable, Optional

from .contracts import Attachment, InboundEvent, InteractionResult, OutboundMessage

Transport = Callable[[str, str, str, Optional[dict]], dict]

_GRAPH = "https://graph.microsoft.com/v1.0"


def _http(method: str, url: str, token: str, payload: Optional[dict]) -> dict:
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed Graph host
        body = r.read().decode("utf-8")
        return json.loads(body) if body else {}


class TeamsCollaborationProvider:
    provider = "msteams"

    def __init__(self, access_token: str = "", *, transport: Transport = _http):
        self._token = access_token
        self._t = transport

    def entitled(self) -> bool:
        return bool(self._token)

    def _split(self, channel: str) -> "tuple[str, str]":
        team, _, chan = channel.partition("/")
        return team, chan

    def _msg_base(self, channel: str) -> str:
        team, chan = self._split(channel)
        if team == "chat":
            return f"{_GRAPH}/chats/{chan}/messages"
        return f"{_GRAPH}/teams/{team}/channels/{chan}/messages"

    # ── inbound ──────────────────────────────────────────────────────────────────────────────────────────
    def normalize_event(self, raw: dict) -> InboundEvent:
        """Normalize a Graph chatMessage resource (or change-notification carrying one) to an InboundEvent."""
        m = raw.get("resourceData") or raw.get("message") or raw
        ident = m.get("channelIdentity", {}) or {}
        frm = (m.get("from", {}) or {}).get("user", {}) or {}
        body = (m.get("body", {}) or {}).get("content", "") or ""
        mentions = tuple(str(((mn.get("mentioned", {}) or {}).get("user", {}) or {}).get("id", ""))
                         for mn in (m.get("mentions") or []) if mn)
        atts = tuple(Attachment(name=a.get("name", ""), url=a.get("contentUrl", ""), mime=a.get("contentType", ""))
                     for a in (m.get("attachments") or []))
        channel = f"{ident.get('teamId','')}/{ident.get('channelId','')}" if ident else m.get("chatId", "")
        return InboundEvent(
            provider=self.provider, workspace=ident.get("teamId", "") or m.get("tenantId", ""),
            channel=channel, actor=str(frm.get("id", "")), text=body,
            thread=str(m.get("replyToId", "") or ""), message_id=str(m.get("id", "")),
            mentions=tuple(x for x in mentions if x), attachments=atts, ts=m.get("createdDateTime", ""), raw=raw)

    def read_thread(self, channel: str, thread: str, *, limit: int = 50) -> list[InboundEvent]:
        url = f"{self._msg_base(channel)}/{thread}/replies?$top={limit}"
        resp = self._t("GET", url, self._token, None)
        return [self.normalize_event({"message": {**m, "channelIdentity": dict(zip(('teamId', 'channelId'),
                self._split(channel)))}}) for m in (resp.get("value") or [])]

    # ── outbound / actions ───────────────────────────────────────────────────────────────────────────────
    def post_message(self, msg: OutboundMessage) -> str:
        url = self._msg_base(msg.channel)
        if msg.thread:
            url = f"{url}/{msg.thread}/replies"
        body = {"body": {"contentType": "html" if msg.blocks else "text", "content": msg.text}}
        return str(self._t("POST", url, self._token, body).get("id", ""))

    def reply_thread(self, channel: str, thread: str, text: str) -> str:
        return self.post_message(OutboundMessage(channel=channel, text=text, thread=thread))

    def update_message(self, channel: str, message_id: str, text: str) -> str:
        url = f"{self._msg_base(channel)}/{message_id}"
        self._t("PATCH", url, self._token, {"body": {"contentType": "text", "content": text}})
        return message_id

    def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool:
        url = f"{self._msg_base(channel)}/{message_id}/setReaction"
        self._t("POST", url, self._token, {"reactionType": emoji.strip(":")})
        return True

    def upload_file(self, channel: str, name: str, content: bytes, *, thread: str = "") -> str:
        # Teams files go through the channel's SharePoint drive; scaffold returns the hosted-content ref. TODO:
        # drive upload + attachment linkage. Kept a no-network stub for the contract.
        return ""

    def resolve_user(self, handle: str) -> str:
        if not handle or "@" not in handle:
            return handle
        return str(self._t("GET", f"{_GRAPH}/users/{handle}", self._token, None).get("id", ""))

    def resolve_channel(self, name: str) -> str:
        if "/" in name:                                   # already "team/channel"
            return name
        return name

    def open_interaction(self, channel: str, prompt: str, *, actions: tuple = ()) -> InteractionResult:
        """Post an Adaptive Card with Action.Submit buttons; the callback returns as an inbound event the
        workflow's human-gate node consumes. Scaffold posts the prompt + card."""
        acts = actions or ("Approve", "Reject")
        card = {"type": "AdaptiveCard", "version": "1.4",
                "body": [{"type": "TextBlock", "text": prompt, "wrap": True}],
                "actions": [{"type": "Action.Submit", "title": a, "data": {"action": a}} for a in acts]}
        try:
            url = self._msg_base(channel)
            body = {"body": {"contentType": "html", "content": prompt},
                    "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                     "content": card}]}
            mid = str(self._t("POST", url, self._token, body).get("id", ""))
            return InteractionResult(ok=True, message_id=mid)
        except Exception as e:  # noqa: BLE001
            return InteractionResult(ok=False, detail=str(e))
