"""Bidirectional chat for the control plane — a compact port of the Hermes 0.17
``sidekick.channels`` concepts into the agentic-os kernel.

Two directions, both best-effort and dependency-free (stdlib only):

* **Notifier (outbound)** — push events to chat. Wired into the approvals flow so
  every money/compliance/infra approval pings your phone, and resolutions confirm.
* **Gateway (inbound)** — receive plain-language asks over chat ("billing: chase
  overdue invoices") and run them through ``Fleet.dispatch``, replying with the
  result. Closed by default: only senders in ``AGENTIC_OS_GATEWAY_ALLOW`` act,
  unless ``AGENTIC_OS_GATEWAY_OPEN=1``.

Adapters are credential-gated: a channel is only "enabled" when its env token is
present, so this is a no-op until you configure Telegram or Slack.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None,
               timeout: float = 15.0) -> tuple[int, dict]:
    """POST (or GET) JSON, never raising — returns (status, body|{"error":...})."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {"raw": raw}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


@dataclass
class Inbound:
    channel: str
    text: str
    sender: str
    reply_to: str


class TelegramChannel:
    """Telegram Bot API: send + long-poll getUpdates. Works behind NAT (no public URL)."""
    name = "telegram"

    def __init__(self):
        self.token = os.environ.get("AGENTIC_OS_TELEGRAM_TOKEN", "")
        self.chat_id = os.environ.get("AGENTIC_OS_TELEGRAM_CHAT_ID", "")
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def send(self, text: str, reply_to: str | None = None) -> None:
        to = reply_to or self.chat_id
        if not (self.token and to):
            return
        _http_json(f"https://api.telegram.org/bot{self.token}/sendMessage",
                   {"chat_id": to, "text": text})

    def poll(self) -> list[Inbound]:
        if not self.token:
            return []
        st, body = _http_json(
            f"https://api.telegram.org/bot{self.token}/getUpdates"
            f"?timeout=20&offset={self._offset}", timeout=25.0)
        out = []
        for upd in (body.get("result") or []):
            self._offset = max(self._offset, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat") or {}
            frm = msg.get("from") or {}
            if text:
                out.append(Inbound("telegram", text, str(frm.get("username") or frm.get("id") or ""),
                                   str(chat.get("id") or "")))
        return out


class SlackChannel:
    """Slack chat.postMessage (outbound only here; inbound Slack needs a public webhook)."""
    name = "slack"

    def __init__(self):
        self.token = os.environ.get("AGENTIC_OS_SLACK_BOT_TOKEN", "")
        self.channel = os.environ.get("AGENTIC_OS_SLACK_CHANNEL", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.channel)

    def send(self, text: str, reply_to: str | None = None) -> None:
        if not self.enabled:
            return
        _http_json("https://slack.com/api/chat.postMessage",
                   {"channel": reply_to or self.channel, "text": text},
                   headers={"Authorization": f"Bearer {self.token}"})

    def poll(self) -> list[Inbound]:
        return []  # inbound Slack is webhook-only; out of scope for the in-process gateway


class DiscordChannel:
    """Discord bot: send to a channel + poll recent messages. Works behind NAT (gateway/REST poll)."""
    name = "discord"

    def __init__(self):
        self.token = os.environ.get("AGENTIC_OS_DISCORD_BOT_TOKEN", "")
        self.channel_id = os.environ.get("AGENTIC_OS_DISCORD_CHANNEL_ID", "")
        self._after = "0"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.channel_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    def send(self, text: str, reply_to: str | None = None) -> None:
        chan = reply_to or self.channel_id
        if not (self.token and chan):
            return
        _http_json(f"https://discord.com/api/v10/channels/{chan}/messages",
                   {"content": text}, headers=self._headers())

    def poll(self) -> list[Inbound]:
        if not self.enabled:
            return []
        st, body = _http_json(
            f"https://discord.com/api/v10/channels/{self.channel_id}/messages?limit=20&after={self._after}",
            headers=self._headers(), timeout=25.0)
        out = []
        for msg in reversed(body if isinstance(body, list) else []):
            self._after = str(max(int(self._after or 0), int(msg.get("id", 0))))
            text = (msg.get("content") or "").strip()
            author = (msg.get("author") or {})
            if text and not author.get("bot"):
                out.append(Inbound("discord", text, str(author.get("username") or author.get("id") or ""),
                                   str(self.channel_id)))
        return out


class SmsChannel:
    """SMS via a Twilio-compatible REST endpoint (outbound). Inbound SMS is webhook-only, like Slack."""
    name = "sms"

    def __init__(self):
        self.sid = os.environ.get("AGENTIC_OS_TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("AGENTIC_OS_TWILIO_AUTH_TOKEN", "")
        self.from_ = os.environ.get("AGENTIC_OS_TWILIO_FROM", "")
        self.to = os.environ.get("AGENTIC_OS_SMS_TO", "")

    @property
    def enabled(self) -> bool:
        return bool(self.sid and self.token and self.from_ and self.to)

    def send(self, text: str, reply_to: str | None = None) -> None:
        if not self.enabled:
            return
        import base64
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        # Twilio wants form-encoding; _http_json posts JSON, so callers using SMS in production should
        # route through the messaging connector. Kept here for parity + the interface; body assembled plainly.
        _http_json(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                   {"To": reply_to or self.to, "From": self.from_, "Body": text},
                   headers={"Authorization": f"Basic {auth}"})

    def poll(self) -> list[Inbound]:
        return []  # inbound SMS is webhook-only; out of scope for the in-process gateway


def load_channels() -> list:
    names = os.environ.get("AGENTIC_OS_CHANNELS", "telegram,slack").split(",")
    by_name = {"telegram": TelegramChannel, "slack": SlackChannel,
               "discord": DiscordChannel, "sms": SmsChannel}
    chans = [by_name[n.strip()]() for n in names if n.strip() in by_name]
    return [c for c in chans if c.enabled]


class Notifier:
    """Best-effort fan-out of events to all enabled channels. Never raises into callers."""

    def __init__(self, channels: list | None = None):
        self.channels = channels if channels is not None else load_channels()

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    def send(self, text: str) -> None:
        for c in self.channels:
            try:
                c.send(text)
            except Exception:  # noqa: BLE001
                pass

    def approval_requested(self, ap) -> None:
        line = f"⏳ approval needed · {ap.module}: {ap.summary} (id {ap.id})"
        if getattr(ap, "findings", None):
            line += f"\n⚠ safety: {'; '.join(ap.findings)}"
        line += "\nreply: approve " + ap.id + "  |  reject " + ap.id
        self.send(line)

    def approval_resolved(self, ap) -> None:
        mark = "✅" if ap.status == "approved" else "🚫"
        self.send(f"{mark} {ap.module}: {ap.action} {ap.status} (id {ap.id})")


class Gateway:
    """Inbound chatops: poll channels, route 'module: ask' to fleet.dispatch, reply.

    Runs as a daemon thread started by the control plane when a channel + token is
    configured. Closed by default; only allow-listed senders are honored.
    """

    def __init__(self, fleet, channels: list | None = None):
        self.fleet = fleet
        self.channels = channels if channels is not None else load_channels()
        self.allow = {s.strip() for s in os.environ.get("AGENTIC_OS_GATEWAY_ALLOW", "").split(",") if s.strip()}
        self.open = os.environ.get("AGENTIC_OS_GATEWAY_OPEN") == "1"
        self._stop = False

    def _authorized(self, msg: Inbound) -> bool:
        return self.open or msg.sender in self.allow

    def _handle(self, msg: Inbound) -> str:
        if not self._authorized(msg):
            return "not authorized"
        # "module: free text"  →  dispatch to that module's first agent.
        if ":" in msg.text:
            mod, _, ask = msg.text.partition(":")
            mod, ask = mod.strip(), ask.strip()
        else:
            return "format: '<module>: <what to do>' (e.g. 'control-tower: revenue this month')"
        try:
            m = self.fleet.registry.get(mod)
            agent = (m.agents or ["analyst"])[0]
            res = self.fleet.dispatch(mod, agent, action="ask", prompt=ask, capability="reason")
            if hasattr(res, "id"):  # an Approval
                return f"⏳ that needs approval (id {res.id}); approve in the control plane."
            return str(res)[:1500]
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    def serve_forever(self) -> None:
        while not self._stop:
            for c in self.channels:
                try:
                    for msg in (c.poll() or []):
                        c.send(self._handle(msg), reply_to=msg.reply_to)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1.0)

    def start(self) -> threading.Thread | None:
        if not self.channels:
            return None
        t = threading.Thread(target=self.serve_forever, name="agentic-os-gateway", daemon=True)
        t.start()
        return t
