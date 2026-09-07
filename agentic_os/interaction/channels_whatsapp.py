"""WhatsApp Business Cloud API channel adapter — the front door for the car-diagnosis service.

Turns inbound WhatsApp webhooks (text, image, voice note, video, document) into
``runtime_contracts`` ``InteractionEvent``s, and sends replies back via the Graph API. This is the
one net-new piece the WhatsApp multimodal service needs; everything downstream (STT via
``GrokSpeechProvider``, vision via Grok, the diagnostic Mission, the Doris store) already exists.

Webhook-driven (push), so instead of the ``ChannelAdapter`` seam's ``receive()`` it exposes
``parse_webhook(payload)`` for the HTTP handler to call, plus ``verify_webhook`` for Meta's GET
handshake and ``fetch_media`` to pull media bytes (which then go to STT/vision). Media messages carry
the WhatsApp media id on ``artifact_ref``; the caller downloads bytes lazily with ``fetch_media``.

Transport is injectable → fully offline-tested against real webhook payload shapes with no network and
no Meta credentials. Live wiring needs a verified Meta app (phone-number id, access token, a public
HTTPS webhook, and the verify token).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from runtime_contracts import Channel, DeliveryReceipt, InteractionEvent, Modality

DEFAULT_GRAPH = "https://graph.facebook.com/v21.0"

# WhatsApp message type -> InteractionEvent modality. (Modality has no VIDEO yet → DOCUMENT; see note.)
_TYPE_MODALITY = {
    "text": Modality.TEXT, "image": Modality.IMAGE, "audio": Modality.AUDIO,
    "voice": Modality.AUDIO, "video": Modality.DOCUMENT, "document": Modality.DOCUMENT,
    "location": Modality.STRUCTURED, "interactive": Modality.STRUCTURED,
}

# transport(method, url, headers, body) -> (status, bytes)
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str],
                      body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=dict(headers), data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class WhatsAppChannelError(RuntimeError):
    pass


class WhatsAppChannelAdapter:
    """Implements the ChannelAdapter seam for WhatsApp Business Cloud API."""

    def __init__(self, *, phone_number_id: str = "", access_token: str = "", verify_token: str = "",
                 graph_url: str = DEFAULT_GRAPH, transport: Optional[Transport] = None) -> None:
        self.phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self._token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self._verify_token = verify_token or os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.graph_url = graph_url.rstrip("/")
        self._transport = transport or _urllib_transport

    def capabilities(self) -> Mapping[str, Any]:
        return {"channel": Channel.WHATSAPP.value, "provider": "whatsapp-business-cloud",
                "modalities": [m.value for m in
                               (Modality.TEXT, Modality.AUDIO, Modality.IMAGE, Modality.DOCUMENT)],
                "inbound": "webhook", "voice_notes": True, "images": True, "video": True}

    # ---- inbound ----

    def verify_webhook(self, mode: str, token: str, challenge: str) -> Optional[str]:
        """Meta's GET verification handshake — echo the challenge iff the verify token matches."""
        if mode == "subscribe" and token and token == self._verify_token:
            return challenge
        return None

    def parse_webhook(self, payload: Mapping[str, Any]) -> List[InteractionEvent]:
        """Turn a WhatsApp webhook body into InteractionEvents (one per inbound message).

        ``conversation_id`` is ``wa:<sender>`` (the app maps it to a case); media messages carry the
        WhatsApp media id on ``artifact_ref`` and text/caption on ``text``.
        """
        events: List[InteractionEvent] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    events.append(self._message_to_event(msg))
        return [e for e in events if e is not None]

    def _message_to_event(self, msg: Mapping[str, Any]) -> Optional[InteractionEvent]:
        sender = msg.get("from", "")
        mtype = msg.get("type", "")
        modality = _TYPE_MODALITY.get(mtype, Modality.STRUCTURED)
        text, artifact_ref = "", ""
        if mtype == "text":
            text = (msg.get("text") or {}).get("body", "")
        elif mtype in ("image", "audio", "voice", "video", "document"):
            media = msg.get(mtype) or {}
            artifact_ref = media.get("id", "")          # WhatsApp media id → fetch_media later
            text = media.get("caption", "")
        elif mtype == "interactive":
            inter = msg.get("interactive") or {}
            text = json.dumps(inter)
        elif mtype == "location":
            text = json.dumps(msg.get("location") or {})
        return InteractionEvent(
            interaction_id=msg.get("id", ""), conversation_id=f"wa:{sender}",
            channel=Channel.WHATSAPP, modality=modality, participant_ref=sender,
            artifact_ref=artifact_ref, text=text, timestamp=msg.get("timestamp", ""),
            provenance="whatsapp")

    def fetch_media(self, media_id: str) -> Tuple[bytes, str]:
        """Download media bytes for a media id (two-step: get URL, then download)."""
        st, body = self._transport("GET", f"{self.graph_url}/{media_id}", self._auth_headers(), None)
        if st != 200:
            raise WhatsAppChannelError(f"media lookup {media_id} failed HTTP {st}")
        meta = json.loads(body)
        url, mime = meta.get("url", ""), meta.get("mime_type", "application/octet-stream")
        st, data = self._transport("GET", url, self._auth_headers(), None)
        if st != 200:
            raise WhatsAppChannelError(f"media download {media_id} failed HTTP {st}")
        return data, mime

    # ---- outbound ----

    def _auth_headers(self) -> Dict[str, str]:
        if not self._token:
            raise WhatsAppChannelError("no WhatsApp access token — set WHATSAPP_ACCESS_TOKEN")
        return {"Authorization": f"Bearer {self._token}"}

    def _send(self, payload: Dict[str, Any]) -> DeliveryReceipt:
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        st, body = self._transport("POST", f"{self.graph_url}/{self.phone_number_id}/messages",
                                   headers, json.dumps(payload).encode())
        to = payload.get("to", "")
        if st not in (200, 201):
            return DeliveryReceipt(interaction_id="", channel=Channel.WHATSAPP, status="failed",
                                   provider_ref=body[:120].decode("utf-8", "ignore"))
        resp = json.loads(body) if body else {}
        mid = ((resp.get("messages") or [{}])[0]).get("id", "")
        return DeliveryReceipt(interaction_id=mid, channel=Channel.WHATSAPP, status="sent",
                               provider_ref=mid)

    def send_text(self, conversation_id: str, text: str) -> DeliveryReceipt:
        return self._send({"messaging_product": "whatsapp", "to": _wa_to(conversation_id),
                           "type": "text", "text": {"body": text}})

    def send_audio(self, conversation_id: str, media: Any) -> DeliveryReceipt:
        # media.artifact_ref is an uploaded WhatsApp media id (upload via /media first)
        return self._send({"messaging_product": "whatsapp", "to": _wa_to(conversation_id),
                           "type": "audio", "audio": {"id": getattr(media, "artifact_ref", str(media))}})

    def send_file(self, conversation_id: str, media: Any) -> DeliveryReceipt:
        return self._send({"messaging_product": "whatsapp", "to": _wa_to(conversation_id),
                           "type": "document", "document": {"id": getattr(media, "artifact_ref", str(media))}})

    def acknowledge(self, interaction_id: str) -> None:
        # mark-as-read is best-effort; never break the flow on it
        try:
            self._send({"messaging_product": "whatsapp", "status": "read",
                        "message_id": interaction_id})
        except Exception:  # noqa: BLE001
            pass


def _wa_to(conversation_id: str) -> str:
    """Our conversation ids are ``wa:<number>``; the Graph API wants the bare number."""
    return conversation_id[3:] if conversation_id.startswith("wa:") else conversation_id
