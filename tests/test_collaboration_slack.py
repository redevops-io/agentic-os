"""Phase 2 — Slack collaboration adapter + the registry promotion (plan §6.2, §18 step 5).

Offline: an injected transport returns canned Slack Web API responses, so no test hits Slack. Pins the shared
InboundEvent normalization, the outbound action calls, credential gating, error mapping, and that Slack is now a
first-class connection provider (not just a Projects UI template label).
"""
from __future__ import annotations

import pytest

from agentic_os.collaboration import (
    CollaborationProvider, InboundEvent, OutboundMessage, SlackCollaborationProvider,
)
from agentic_os.collaboration.slack import SlackError
from agentic_os.connections.registry import default_registry


class _FakeSlack:
    """Records calls; returns canned responses keyed by api method."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, token, payload):
        api = url.rsplit("/", 1)[-1]
        self.calls.append((api, payload))
        return self.responses.get(api, {"ok": True})


def test_slack_is_a_registered_provider_not_just_a_template():
    reg = default_registry()
    assert "slack" in reg and reg["slack"].domain == "collaboration"
    # the other two chat surfaces are registered for the same contract
    assert "msteams" in reg and "google_chat" in reg


def test_entitlement_requires_a_token():
    assert not SlackCollaborationProvider().entitled()
    assert SlackCollaborationProvider(bot_token="xoxb-1").entitled()


def test_satisfies_collaboration_contract():
    assert isinstance(SlackCollaborationProvider(bot_token="t"), CollaborationProvider)


def test_normalize_event_to_shared_shape():
    raw = {"team_id": "T123", "event": {"type": "message", "channel": "C1", "user": "U9",
                                        "text": "hey <@UBOT> please review", "ts": "1700000000.1",
                                        "thread_ts": "1699999999.0",
                                        "files": [{"name": "brief.pdf", "url_private": "https://x/f",
                                                   "mimetype": "application/pdf"}]}}
    ev = SlackCollaborationProvider(bot_token="t").normalize_event(raw)
    assert isinstance(ev, InboundEvent) and ev.provider == "slack" and ev.workspace == "T123"
    assert ev.channel == "C1" and ev.actor == "U9" and ev.thread == "1699999999.0"
    assert ev.mentions == ("UBOT",) and ev.attachments[0].name == "brief.pdf"


def test_post_and_reply_build_correct_calls():
    fake = _FakeSlack({"chat.postMessage": {"ok": True, "ts": "1700000001.5"}})
    s = SlackCollaborationProvider(bot_token="t", transport=fake)
    ts = s.post_message(OutboundMessage(channel="C1", text="hello"))
    assert ts == "1700000001.5" and fake.calls[0][0] == "chat.postMessage"
    s.reply_thread("C1", "1699999999.0", "in thread")
    assert fake.calls[1][1]["thread_ts"] == "1699999999.0"  # reply carries the thread ts


def test_add_reaction_and_resolve_channel():
    fake = _FakeSlack({"reactions.add": {"ok": True},
                       "conversations.list": {"ok": True, "channels": [{"name": "ops", "id": "C77"}]}})
    s = SlackCollaborationProvider(bot_token="t", transport=fake)
    assert s.add_reaction("C1", "170.0", ":eyes:") is True
    assert s.resolve_channel("#ops") == "C77"
    assert s.resolve_channel("C999") == "C999"  # already an id → pass-through


def test_open_interaction_posts_approval_buttons():
    fake = _FakeSlack({"chat.postMessage": {"ok": True, "ts": "ts-appr"}})
    s = SlackCollaborationProvider(bot_token="t", transport=fake)
    res = s.open_interaction("C1", "Approve outreach to Acme?", actions=("Approve", "Reject"))
    assert res.ok and res.message_id == "ts-appr"
    assert fake.calls[0][1].get("blocks")  # posted with Block Kit action buttons


def test_api_error_maps_to_slack_error():
    fake = _FakeSlack({"chat.postMessage": {"ok": False, "error": "channel_not_found"}})
    s = SlackCollaborationProvider(bot_token="t", transport=fake)
    with pytest.raises(SlackError, match="channel_not_found"):
        s.post_message(OutboundMessage(channel="bad", text="x"))
