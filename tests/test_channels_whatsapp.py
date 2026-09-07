"""WhatsApp Business Cloud API channel adapter — webhook → InteractionEvents, Graph API out.

Offline against real webhook payload shapes; transport injected (no network, no Meta creds)."""
from __future__ import annotations

import json

import pytest

from runtime_contracts import Channel, Modality
from agentic_os.interaction import WhatsAppChannelAdapter, WhatsAppChannelError


def _wh(*messages):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA",
            "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "PNID"},
                "contacts": [{"wa_id": "15551234", "profile": {"name": "Alex"}}],
                "messages": list(messages)}}]}]}


class _FakeTransport:
    def __init__(self, responses):
        self.responses = responses      # list of (status, bytes) in call order, or a dict by url-substr
        self.calls = []
    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if isinstance(self.responses, dict):
            for k, v in self.responses.items():
                if k in url:
                    return v
            return 404, b"{}"
        return self.responses[len(self.calls) - 1]


def test_verify_webhook_handshake():
    a = WhatsAppChannelAdapter(verify_token="v123")
    assert a.verify_webhook("subscribe", "v123", "CHALLENGE") == "CHALLENGE"
    assert a.verify_webhook("subscribe", "wrong", "CHALLENGE") is None


def test_parse_text_message():
    a = WhatsAppChannelAdapter()
    evs = a.parse_webhook(_wh({"from": "15551234", "id": "wamid.1", "timestamp": "1725600000",
                               "type": "text", "text": {"body": "my car won't start"}}))
    assert len(evs) == 1
    e = evs[0]
    assert e.channel is Channel.WHATSAPP and e.modality is Modality.TEXT
    assert e.conversation_id == "wa:15551234" and e.participant_ref == "15551234"
    assert e.text == "my car won't start" and e.interaction_id == "wamid.1"


def test_parse_voice_note_and_image_carry_media_id():
    a = WhatsAppChannelAdapter()
    evs = a.parse_webhook(_wh(
        {"from": "15551234", "id": "wamid.a", "type": "audio",
         "audio": {"id": "MEDIA_AUDIO", "mime_type": "audio/ogg", "voice": True}},
        {"from": "15551234", "id": "wamid.i", "type": "image",
         "image": {"id": "MEDIA_IMG", "mime_type": "image/jpeg", "caption": "dashboard light"}}))
    audio, image = evs
    assert audio.modality is Modality.AUDIO and audio.artifact_ref == "MEDIA_AUDIO"
    assert image.modality is Modality.IMAGE and image.artifact_ref == "MEDIA_IMG"
    assert image.text == "dashboard light"


def test_parse_video_maps_to_document_modality():
    a = WhatsAppChannelAdapter()
    evs = a.parse_webhook(_wh({"from": "15551234", "id": "wamid.v", "type": "video",
                               "video": {"id": "MEDIA_VID", "mime_type": "video/mp4"}}))
    assert evs[0].modality is Modality.DOCUMENT and evs[0].artifact_ref == "MEDIA_VID"  # no VIDEO modality yet


def test_fetch_media_two_step_download():
    ft = _FakeTransport({"/MEDIA_IMG": (200, json.dumps({"url": "https://cdn/x", "mime_type": "image/jpeg"}).encode()),
                         "cdn/x": (200, b"\xff\xd8jpegbytes")})
    a = WhatsAppChannelAdapter(access_token="tok", transport=ft)
    data, mime = a.fetch_media("MEDIA_IMG")
    assert data == b"\xff\xd8jpegbytes" and mime == "image/jpeg"
    assert ft.calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_send_text_posts_to_graph_and_parses_message_id():
    ft = _FakeTransport({"/PNID/messages": (200, json.dumps({"messages": [{"id": "wamid.out"}]}).encode())})
    a = WhatsAppChannelAdapter(phone_number_id="PNID", access_token="tok", transport=ft)
    r = a.send_text("wa:15551234", "Likely a cylinder-2 misfire — avoid driving.")
    assert r.status == "sent" and r.provider_ref == "wamid.out" and r.channel is Channel.WHATSAPP
    sent = json.loads(ft.calls[0]["body"])
    assert sent["to"] == "15551234" and sent["type"] == "text"       # wa: prefix stripped
    assert sent["text"]["body"].startswith("Likely a cylinder-2")


def test_send_failure_returns_failed_receipt():
    ft = _FakeTransport({"/PNID/messages": (400, b'{"error":{"message":"bad"}}')})
    a = WhatsAppChannelAdapter(phone_number_id="PNID", access_token="tok", transport=ft)
    assert a.send_text("wa:15551234", "hi").status == "failed"


def test_outbound_without_token_is_a_clear_error():
    a = WhatsAppChannelAdapter(phone_number_id="PNID")   # no token
    with pytest.raises(WhatsAppChannelError, match="WHATSAPP_ACCESS_TOKEN"):
        a.send_text("wa:1", "hi")


def test_capabilities():
    caps = WhatsAppChannelAdapter().capabilities()
    assert caps["channel"] == "whatsapp" and caps["inbound"] == "webhook" and caps["voice_notes"] is True
