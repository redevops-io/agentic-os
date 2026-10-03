"""Phase 2 — Teams + Google Chat collaboration adapters (plan §6.3/§6.4) on the SAME contract.

Offline: injected transports return canned responses; no test hits Microsoft Graph or the Google Chat API.
Proves channel portability (§6.5) — the same CollaborationProvider contract and normalized InboundEvent across
all three surfaces, so workflow logic is independent of the channel.
"""
from __future__ import annotations

from agentic_os.collaboration import (
    CollaborationProvider, GoogleChatCollaborationProvider, InboundEvent, OutboundMessage,
    TeamsCollaborationProvider,
)


class _Fake:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, token, payload):
        self.calls.append((method, url, payload))
        # match the key that appears furthest right in the URL, so "/replies" wins over the "/messages" base
        best, best_pos = None, -1
        for key in self.responses:
            pos = url.find(key)
            if pos > best_pos:
                best, best_pos = key, pos
        return self.responses[best] if best is not None else {}


# ── Microsoft Teams ──────────────────────────────────────────────────────────────────────────────────────
def test_teams_satisfies_contract_and_gates_on_token():
    assert isinstance(TeamsCollaborationProvider(access_token="t"), CollaborationProvider)
    assert not TeamsCollaborationProvider().entitled()
    assert TeamsCollaborationProvider(access_token="t").entitled()


def test_teams_normalize_and_post():
    raw = {"message": {"id": "19:abc", "createdDateTime": "2026-10-03T09:00:00Z",
                       "channelIdentity": {"teamId": "T1", "channelId": "C1"},
                       "from": {"user": {"id": "U9", "displayName": "Alex"}},
                       "body": {"content": "please review"},
                       "mentions": [{"mentioned": {"user": {"id": "UBOT"}}}]}}
    t = TeamsCollaborationProvider(access_token="tok")
    ev = t.normalize_event(raw)
    assert isinstance(ev, InboundEvent) and ev.provider == "msteams"
    assert ev.channel == "T1/C1" and ev.actor == "U9" and ev.mentions == ("UBOT",)

    fake = _Fake({"/teams/T1/channels/C1/messages": {"id": "19:new"}})
    t2 = TeamsCollaborationProvider(access_token="tok", transport=fake)
    assert t2.post_message(OutboundMessage(channel="T1/C1", text="hi")) == "19:new"
    # reply targets the message's /replies
    fake.responses["/replies"] = {"id": "19:reply"}
    assert t2.reply_thread("T1/C1", "19:abc", "in thread") == "19:reply"
    assert "/19:abc/replies" in fake.calls[-1][1]


def test_teams_group_chat_routes_to_chats_endpoint():
    fake = _Fake({"/chats/42/messages": {"id": "m1"}})
    t = TeamsCollaborationProvider(access_token="tok", transport=fake)
    t.post_message(OutboundMessage(channel="chat/42", text="dm"))
    assert "/chats/42/messages" in fake.calls[-1][1]


# ── Google Chat ──────────────────────────────────────────────────────────────────────────────────────────
def test_gchat_satisfies_contract_and_gates_on_token():
    assert isinstance(GoogleChatCollaborationProvider(access_token="t"), CollaborationProvider)
    assert not GoogleChatCollaborationProvider().entitled()


def test_gchat_normalize_and_post_thread():
    raw = {"type": "MESSAGE", "space": {"name": "spaces/AAA"},
           "message": {"name": "spaces/AAA/messages/111", "text": "hi <bot>", "createTime": "2026-10-03T09:00:00Z",
                       "sender": {"name": "users/U9"}, "thread": {"name": "spaces/AAA/threads/TTT"},
                       "annotations": [{"type": "USER_MENTION", "userMention": {"user": {"name": "users/UBOT"}}}]}}
    g = GoogleChatCollaborationProvider(access_token="tok")
    ev = g.normalize_event(raw)
    assert ev.provider == "google_chat" and ev.channel == "spaces/AAA" and ev.actor == "users/U9"
    assert ev.thread == "spaces/AAA/threads/TTT" and ev.mentions == ("users/UBOT",)

    fake = _Fake({"/spaces/AAA/messages": {"name": "spaces/AAA/messages/222"}})
    g2 = GoogleChatCollaborationProvider(access_token="tok", transport=fake)
    mid = g2.reply_thread("spaces/AAA", "spaces/AAA/threads/TTT", "reply")
    assert mid == "spaces/AAA/messages/222"
    assert fake.calls[-1][2]["thread"]["name"] == "spaces/AAA/threads/TTT"  # reply carries the thread
    assert "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in fake.calls[-1][1]


def test_gchat_resolve_helpers():
    g = GoogleChatCollaborationProvider(access_token="t")
    assert g.resolve_channel("AAA") == "spaces/AAA" and g.resolve_channel("spaces/BBB") == "spaces/BBB"
    assert g.resolve_user("U1") == "users/U1" and g.resolve_user("users/U2") == "users/U2"


# ── channel portability (§6.5): same contract across all three ──────────────────────────────────────────
def test_all_three_surfaces_share_one_contract():
    from agentic_os.collaboration import SlackCollaborationProvider
    for p in (SlackCollaborationProvider(bot_token="t"), TeamsCollaborationProvider(access_token="t"),
              GoogleChatCollaborationProvider(access_token="t")):
        assert isinstance(p, CollaborationProvider)
        assert p.normalize_event({}).provider == p.provider   # normalize never raises on an empty payload
