"""Telegram Bot API channel adapter — the go-live-now front door for the car-diagnosis service.

Telegram needs no business verification (unlike WhatsApp), so with a bot token it works immediately.
Turns inbound Telegram updates (text, photo, voice note, video, audio, document) into
``runtime_contracts`` ``InteractionEvent``s, and replies via the Bot API; everything downstream (Grok
STT/vision, the diagnostic Mission, the Doris store) is unchanged — it's the same ``ChannelAdapter``
seam as WhatsApp.

Supports both delivery modes: ``poll()`` (long-poll ``getUpdates`` — zero infra, ideal to start) and
``parse_update()`` for a webhook handler once a public HTTPS endpoint exists. Media messages carry the
Telegram ``file_id`` on ``artifact_ref``; ``fetch_file`` downloads the bytes (→ STT/vision).

Transport is injectable → fully offline-tested with a fake token; live wiring reads ``REDEVOPS_BOT_TOKEN``.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from runtime_contracts import Channel, DeliveryReceipt, InteractionEvent, Modality

DEFAULT_API = "https://api.telegram.org"

# transport(method, url, headers, body) -> (status, bytes)
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str],
                      body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=dict(headers), data=body)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TelegramChannelError(RuntimeError):
    pass


class TelegramChannelAdapter:
    """Implements the ChannelAdapter seam for the Telegram Bot API."""

    def __init__(self, *, token: str = "", api_url: str = DEFAULT_API,
                 transport: Optional[Transport] = None) -> None:
        self._token = token or os.environ.get("REDEVOPS_BOT_TOKEN", "")
        self.api_url = api_url.rstrip("/")
        self._transport = transport or _urllib_transport
        self._offset: Optional[int] = None

    def _base(self) -> str:
        if not self._token:
            raise TelegramChannelError("no Telegram token — set REDEVOPS_BOT_TOKEN")
        return f"{self.api_url}/bot{self._token}"

    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(params).encode()
        st, data = self._transport("POST", f"{self._base()}/{method}",
                                   {"Content-Type": "application/json"}, body)
        resp = json.loads(data) if data else {}
        if st != 200 or not resp.get("ok"):
            raise TelegramChannelError(f"{method} failed HTTP {st}: {str(resp.get('description'))[:120]}")
        return resp

    def capabilities(self) -> Mapping[str, Any]:
        return {"channel": Channel.TELEGRAM.value, "provider": "telegram-bot-api",
                "modalities": [m.value for m in
                               (Modality.TEXT, Modality.AUDIO, Modality.IMAGE, Modality.DOCUMENT)],
                "inbound": "poll+webhook", "voice_notes": True, "images": True, "video": True}

    def get_me(self) -> Dict[str, Any]:
        return self._call("getMe", {}).get("result", {})

    # ---- inbound ----

    def poll(self, *, timeout: int = 0, limit: int = 100) -> List[InteractionEvent]:
        """Long-poll getUpdates; auto-advances the offset so each update is returned once."""
        params: Dict[str, Any] = {"timeout": timeout, "limit": limit,
                                  "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        result = self._call("getUpdates", params).get("result", [])
        events: List[InteractionEvent] = []
        for upd in result:
            self._offset = int(upd["update_id"]) + 1
            ev = self.parse_update(upd)
            if ev is not None:
                events.append(ev)
        return events

    def parse_update(self, update: Mapping[str, Any]) -> Optional[InteractionEvent]:
        """One Telegram Update (poll item or webhook body) → an InteractionEvent, or None."""
        msg = update.get("message") or update.get("edited_message") or update.get("channel_post")
        if not msg:
            return None
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        sender = str((msg.get("from") or {}).get("id", chat_id))
        modality, artifact_ref, text = Modality.STRUCTURED, "", ""
        if "text" in msg:
            modality, text = Modality.TEXT, msg["text"]
        elif "voice" in msg:
            modality, artifact_ref = Modality.AUDIO, msg["voice"].get("file_id", "")
        elif "audio" in msg:
            modality, artifact_ref = Modality.AUDIO, msg["audio"].get("file_id", "")
        elif "photo" in msg:
            photos = msg["photo"] or [{}]
            modality, artifact_ref = Modality.IMAGE, photos[-1].get("file_id", "")  # largest size
            text = msg.get("caption", "")
        elif "video" in msg:
            modality, artifact_ref = Modality.DOCUMENT, msg["video"].get("file_id", "")  # no VIDEO modality yet
            text = msg.get("caption", "")
        elif "document" in msg:
            modality, artifact_ref = Modality.DOCUMENT, msg["document"].get("file_id", "")
            text = msg.get("caption", "")
        else:
            return None
        return InteractionEvent(
            interaction_id=f"tg:{chat_id}:{msg.get('message_id','')}",
            conversation_id=f"tg:{chat_id}", channel=Channel.TELEGRAM, modality=modality,
            participant_ref=sender, artifact_ref=artifact_ref, text=text,
            timestamp=str(msg.get("date", "")), provenance="telegram")

    def fetch_file(self, file_id: str) -> Tuple[bytes, str]:
        """Download a Telegram file (two-step: getFile → download from the file path)."""
        path = self._call("getFile", {"file_id": file_id}).get("result", {}).get("file_path", "")
        if not path:
            raise TelegramChannelError(f"getFile {file_id}: no file_path")
        st, data = self._transport("GET", f"{self.api_url}/file/bot{self._token}/{path}", {}, None)
        if st != 200:
            raise TelegramChannelError(f"file download {file_id} failed HTTP {st}")
        mime = "audio/ogg" if path.endswith(".oga") else ("image/jpeg" if path.endswith((".jpg", ".jpeg"))
                                                          else "application/octet-stream")
        return data, mime

    # ---- outbound ----

    def send_text(self, conversation_id: str, text: str) -> DeliveryReceipt:
        try:
            r = self._call("sendMessage", {"chat_id": _chat(conversation_id), "text": text})
        except TelegramChannelError as e:
            return DeliveryReceipt(interaction_id="", channel=Channel.TELEGRAM, status="failed",
                                   provider_ref=str(e)[:120])
        mid = str((r.get("result") or {}).get("message_id", ""))
        return DeliveryReceipt(interaction_id=mid, channel=Channel.TELEGRAM, status="sent",
                               provider_ref=mid)

    def send_audio(self, conversation_id: str, media: Any) -> DeliveryReceipt:
        ref = getattr(media, "artifact_ref", str(media))
        r = self._call("sendVoice", {"chat_id": _chat(conversation_id), "voice": ref})
        return DeliveryReceipt(interaction_id=str((r.get("result") or {}).get("message_id", "")),
                               channel=Channel.TELEGRAM, status="sent")

    def send_file(self, conversation_id: str, media: Any) -> DeliveryReceipt:
        ref = getattr(media, "artifact_ref", str(media))
        r = self._call("sendDocument", {"chat_id": _chat(conversation_id), "document": ref})
        return DeliveryReceipt(interaction_id=str((r.get("result") or {}).get("message_id", "")),
                               channel=Channel.TELEGRAM, status="sent")

    def acknowledge(self, interaction_id: str) -> None:
        return None  # Telegram has no per-message read receipt for bots

    # ---- webhook mode (optional; same Update shape as poll) ----

    def set_webhook(self, url: str) -> Dict[str, Any]:
        return self._call("setWebhook", {"url": url, "allowed_updates": ["message"]})

    def delete_webhook(self) -> Dict[str, Any]:
        return self._call("deleteWebhook", {})


def _chat(conversation_id: str) -> str:
    """Our conversation ids are ``tg:<chat_id>``; the Bot API wants the bare chat id."""
    return conversation_id[3:] if conversation_id.startswith("tg:") else conversation_id
