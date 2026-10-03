"""Slack collaboration adapter (plan §6.2) — promote Slack from a template label to a real provider.

Implements the common `CollaborationProvider` over the Slack Web API (``chat.postMessage``, ``conversations.*``,
``reactions.add``, ``files.upload``). Credential-gated (a bot token) and fully offline-testable: the HTTP call
goes through an injected ``transport(method, url, token, payload) -> dict`` so tests drive canned responses and
no test hits Slack. Inbound Events API payloads normalize to the shared ``InboundEvent``.
"""
from __future__ import annotations

from typing import Callable, Optional

from .contracts import Attachment, InboundEvent, InteractionResult, OutboundMessage

# transport(method, url, token, payload) -> parsed json dict
Transport = Callable[[str, str, str, Optional[dict]], dict]

_API = "https://slack.com/api"


def _http(method: str, url: str, token: str, payload: Optional[dict]) -> dict:
    """Default live transport (stdlib). Tests inject a fake instead."""
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed Slack host
        return json.loads(r.read().decode("utf-8") or "{}")


class SlackCollaborationProvider:
    provider = "slack"

    def __init__(self, bot_token: str = "", *, transport: Transport = _http):
        self._token = bot_token
        self._t = transport

    def entitled(self) -> bool:
        return bool(self._token)

    def _call(self, api_method: str, payload: dict) -> dict:
        resp = self._t("POST", f"{_API}/{api_method}", self._token, payload)
        if not resp.get("ok", False):
            raise SlackError(resp.get("error", "unknown_error"))
        return resp

    # ── inbound ──────────────────────────────────────────────────────────────────────────────────────────
    def normalize_event(self, raw: dict) -> InboundEvent:
        """Normalize a Slack Events API envelope (``{team_id, event: {...}}``) to an InboundEvent."""
        ev = raw.get("event", raw) or {}
        text = ev.get("text", "") or ""
        mentions = tuple(part[2:-1] for part in text.split() if part.startswith("<@") and part.endswith(">"))
        files = tuple(Attachment(name=f.get("name", ""), url=f.get("url_private", ""), mime=f.get("mimetype", ""))
                      for f in (ev.get("files") or []))
        return InboundEvent(
            provider=self.provider, workspace=raw.get("team_id", "") or ev.get("team", ""),
            channel=ev.get("channel", ""), actor=ev.get("user", ""), text=text,
            thread=ev.get("thread_ts", "") or "", message_id=ev.get("ts", ""),
            mentions=mentions, attachments=files, ts=ev.get("ts", ""), raw=raw)

    def read_thread(self, channel: str, thread: str, *, limit: int = 50) -> list[InboundEvent]:
        resp = self._call("conversations.replies", {"channel": channel, "ts": thread, "limit": limit})
        out = []
        for m in resp.get("messages", []) or []:
            out.append(self.normalize_event({"team_id": "", "event": {**m, "channel": channel}}))
        return out

    # ── outbound / actions ───────────────────────────────────────────────────────────────────────────────
    def post_message(self, msg: OutboundMessage) -> str:
        payload = {"channel": msg.channel, "text": msg.text}
        if msg.thread:
            payload["thread_ts"] = msg.thread
        if msg.blocks:
            payload["blocks"] = list(msg.blocks)
        return self._call("chat.postMessage", payload).get("ts", "")

    def reply_thread(self, channel: str, thread: str, text: str) -> str:
        return self.post_message(OutboundMessage(channel=channel, text=text, thread=thread))

    def update_message(self, channel: str, message_id: str, text: str) -> str:
        return self._call("chat.update", {"channel": channel, "ts": message_id, "text": text}).get("ts", "")

    def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool:
        return self._call("reactions.add", {"channel": channel, "timestamp": message_id,
                                            "name": emoji.strip(":")}).get("ok", False)

    def upload_file(self, channel: str, name: str, content: bytes, *, thread: str = "") -> str:
        payload = {"channels": channel, "filename": name, "content": content.decode("utf-8", "replace")}
        if thread:
            payload["thread_ts"] = thread
        return (self._call("files.upload", payload).get("file", {}) or {}).get("id", "")

    # ── resolution + interaction ─────────────────────────────────────────────────────────────────────────
    def resolve_user(self, handle: str) -> str:
        if handle.startswith("U"):                       # already a Slack user id
            return handle
        resp = self._call("users.lookupByEmail", {"email": handle})
        return (resp.get("user", {}) or {}).get("id", "")

    def resolve_channel(self, name: str) -> str:
        if name.startswith("C") or name.startswith("G"):  # already a channel id
            return name
        resp = self._call("conversations.list", {"limit": 1000})
        want = name.lstrip("#")
        for ch in resp.get("channels", []) or []:
            if ch.get("name") == want:
                return ch.get("id", "")
        return ""

    def open_interaction(self, channel: str, prompt: str, *, actions: tuple = ()) -> InteractionResult:
        """Post an interactive approval prompt (Block Kit actions). The button callback is delivered back as an
        inbound interaction the workflow's human-gate node consumes; here we just post and return its id."""
        buttons = [{"type": "button", "text": {"type": "plain_text", "text": a}, "action_id": f"act_{a}"}
                   for a in (actions or ("Approve", "Reject"))]
        blocks = ({"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
                  {"type": "actions", "elements": buttons})
        try:
            ts = self.post_message(OutboundMessage(channel=channel, text=prompt, blocks=blocks))
            return InteractionResult(ok=True, message_id=ts)
        except SlackError as e:
            return InteractionResult(ok=False, detail=str(e))


class SlackError(RuntimeError):
    """A Slack API ``ok:false`` response (carries the Slack error code)."""
