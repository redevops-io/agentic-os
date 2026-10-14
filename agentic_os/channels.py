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
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


_SECRET_RE = re.compile(r"(bot\d+:[A-Za-z0-9_-]+|Bearer\s+\S+|Basic\s+\S+|Bot\s+\S+|xox[a-z]-[\w-]+|AC[0-9a-fA-F]{32}|[0-9a-f]{32})")


def redact(text: str) -> str:
    """Strip anything token-shaped from an error string before it is stored or logged."""
    return _SECRET_RE.sub("[redacted]", str(text))


@dataclass
class SendResult:
    """Outcome of one channel send. ``permanent`` is only meaningful when ``ok`` is False: a 4xx (except 408/429)
    or a provider-level rejection will not succeed on retry; 5xx / 429 / timeouts / network errors will."""
    ok: bool
    status: int = 0
    message_id: str = ""
    error: str = ""
    permanent: bool = False


def classify_status(status: int) -> bool:
    """True when an HTTP status is a PERMANENT failure (do not retry)."""
    if status == 0 or status in (408, 425, 429) or status >= 500:
        return False
    return 400 <= status < 500


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None,
               timeout: float = 15.0, form: bool = False) -> tuple[int, dict]:
    """POST (or GET) JSON — or form-urlencoded when ``form`` (Twilio) — never raising. Returns
    (status, body|{"error":...}); status 0 means timeout/network failure. Error text is redacted."""
    ctype = "application/json"
    if payload is None:
        data = None
    elif form:
        data = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        body.setdefault("error", f"http {e.code}")
        return e.code, body
    except Exception as e:  # noqa: BLE001 - timeouts, DNS, TLS; never echo the URL (it can carry a token)
        return 0, {"error": redact(type(e).__name__)}


def _result(status: int, body: dict, id_of) -> SendResult:
    """Normalise (status, body) into a SendResult; ``id_of(body)`` extracts the provider message id."""
    if 200 <= status < 300:
        return SendResult(True, status, str(id_of(body) or ""))
    return SendResult(False, status, "", redact(str(body.get("error") or f"http {status}"))[:200],
                      permanent=classify_status(status))


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

    def send(self, text: str, reply_to: str | None = None) -> SendResult:
        to = reply_to or self.chat_id
        if not (self.token and to):
            return SendResult(False, 0, error="not configured", permanent=True)
        st, body = _http_json(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              {"chat_id": to, "text": text}, timeout=10.0)
        r = _result(st, body, lambda b: (b.get("result") or {}).get("message_id"))
        if r.ok and body.get("ok") is False:
            return SendResult(False, st, error="telegram rejected", permanent=True)
        return r

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

    def send(self, text: str, reply_to: str | None = None) -> SendResult:
        if not self.enabled:
            return SendResult(False, 0, error="not configured", permanent=True)
        st, body = _http_json("https://slack.com/api/chat.postMessage",
                              {"channel": reply_to or self.channel, "text": text},
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=10.0)
        if 200 <= st < 300 and body.get("ok") is False:     # Slack reports most failures as 200 + ok:false
            err = str(body.get("error") or "slack error")
            transient = err in ("ratelimited", "internal_error", "service_unavailable", "fatal_error", "request_timeout")
            return SendResult(False, st, error=redact(err)[:200], permanent=not transient)
        return _result(st, body, lambda b: b.get("ts"))

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

    def send(self, text: str, reply_to: str | None = None) -> SendResult:
        chan = reply_to or self.channel_id
        if not (self.token and chan):
            return SendResult(False, 0, error="not configured", permanent=True)
        st, body = _http_json(f"https://discord.com/api/v10/channels/{chan}/messages",
                              {"content": text}, headers=self._headers(), timeout=10.0)
        return _result(st, body, lambda b: b.get("id"))

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

    def send(self, text: str, reply_to: str | None = None) -> SendResult:
        if not self.enabled:
            return SendResult(False, 0, error="not configured", permanent=True)
        import base64
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        # Twilio's Messages API takes application/x-www-form-urlencoded, not JSON.
        st, body = _http_json(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                              {"To": reply_to or self.to, "From": self.from_, "Body": text},
                              headers={"Authorization": f"Basic {auth}"}, timeout=10.0, form=True)
        return _result(st, body, lambda b: b.get("sid"))

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
        line += "\nreview in the control plane (replying here does not approve)"
        self.send(line)  # NB: replying does not approve anything — approval is only via the authenticated surface

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
