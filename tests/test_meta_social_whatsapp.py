"""Meta social + WhatsApp — FB/IG publishing (Graph API) + WhatsApp on the CollaborationProvider contract.

Offline: injected transports stand in for the Meta Graph / WhatsApp Cloud API; no test hits Meta. There is no
official Meta Muse MCP — these are direct Graph-API adapters (the supported path).
"""
from __future__ import annotations

from agentic_os.collaboration import (
    CollaborationProvider, InboundEvent, OutboundMessage, WhatsAppCollaborationProvider,
)
from agentic_os.connections.registry import default_registry
from agentic_os.social import MetaGraphPublisher


class _Fake:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, token, payload):
        self.calls.append((method, url, payload))
        best, pos = {}, -1
        for key, resp in self.responses.items():
            p = url.find(key)
            if p > pos:
                best, pos = resp, p
        return best


# ── registry ─────────────────────────────────────────────────────────────────────────────────────────────
def test_meta_and_whatsapp_registered():
    reg = default_registry()
    assert reg["facebook"].domain == "social" and reg["instagram"].domain == "social"
    assert reg["whatsapp"].domain == "collaboration"
    assert "instagram_content_publish" in reg["instagram"].scopes


# ── Facebook / Instagram publishing ──────────────────────────────────────────────────────────────────────
def test_facebook_page_post():
    pub = MetaGraphPublisher(access_token="t", transport=_Fake({"/feed": {"id": "page_1:post_9"}}))
    r = pub.publish_facebook("page_1", "Launch day!", link="https://redevops.io")
    assert r.ok and r.platform == "facebook" and r.post_id == "page_1:post_9"


def test_instagram_two_step_publish():
    fake = _Fake({"/media_publish": {"id": "ig_media_42"}, "/media": {"id": "creation_7"}})
    pub = MetaGraphPublisher(access_token="t", transport=fake)
    r = pub.publish_instagram("ig_1", "https://cdn/x.jpg", caption="hello")
    assert r.ok and r.platform == "instagram" and r.post_id == "ig_media_42"
    # it created a container first, then published it
    assert any("/media" in u and "/media_publish" not in u for _, u, _ in fake.calls)
    assert any("/media_publish" in u for _, u, _ in fake.calls)


def test_publish_requires_token():
    assert MetaGraphPublisher().publish_facebook("p", "x").ok is False
    assert MetaGraphPublisher().publish_instagram("ig", "u").ok is False


def test_instagram_container_failure_is_reported():
    pub = MetaGraphPublisher(access_token="t",
                             transport=_Fake({"/media": {"error": {"message": "bad image_url"}}}))
    r = pub.publish_instagram("ig_1", "notaurl")
    assert r.ok is False and "bad image_url" in r.error


# ── WhatsApp on the collaboration contract ───────────────────────────────────────────────────────────────
def test_whatsapp_satisfies_contract_and_gates():
    assert isinstance(WhatsAppCollaborationProvider(access_token="t", phone_number_id="1"), CollaborationProvider)
    assert not WhatsAppCollaborationProvider(access_token="t").entitled()          # needs phone_number_id too
    assert WhatsAppCollaborationProvider(access_token="t", phone_number_id="1").entitled()


def test_whatsapp_normalize_inbound_webhook():
    raw = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN1"},
        "messages": [{"from": "15551230000", "id": "wamid.X", "timestamp": "1700000000",
                      "type": "text", "text": {"body": "need an update"},
                      "context": {"id": "wamid.PREV"}}]}}]}]}
    ev = WhatsAppCollaborationProvider(access_token="t", phone_number_id="PN1").normalize_event(raw)
    assert isinstance(ev, InboundEvent) and ev.provider == "whatsapp"
    assert ev.channel == "15551230000" and ev.actor == "15551230000" and ev.text == "need an update"
    assert ev.thread == "wamid.PREV" and ev.message_id == "wamid.X" and ev.workspace == "PN1"


def test_whatsapp_send_reply_and_reaction():
    fake = _Fake({"/PN1/messages": {"messages": [{"id": "wamid.OUT"}]}})
    wa = WhatsAppCollaborationProvider(access_token="t", phone_number_id="PN1", transport=fake)
    assert wa.post_message(OutboundMessage(channel="15551230000", text="hi")) == "wamid.OUT"
    assert fake.calls[-1][2]["messaging_product"] == "whatsapp" and fake.calls[-1][2]["to"] == "15551230000"
    wa.reply_thread("15551230000", "wamid.PREV", "replying")
    assert fake.calls[-1][2]["context"]["message_id"] == "wamid.PREV"   # reply uses context
    assert wa.add_reaction("15551230000", "wamid.OUT", ":+1:") is True
    assert fake.calls[-1][2]["type"] == "reaction"


def test_whatsapp_interactive_approval():
    fake = _Fake({"/PN1/messages": {"messages": [{"id": "wamid.BTN"}]}})
    wa = WhatsAppCollaborationProvider(access_token="t", phone_number_id="PN1", transport=fake)
    res = wa.open_interaction("15551230000", "Approve outreach?", actions=("Approve", "Reject"))
    assert res.ok and res.message_id == "wamid.BTN"
    assert fake.calls[-1][2]["interactive"]["type"] == "button"
