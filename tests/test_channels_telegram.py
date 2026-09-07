"""Telegram Bot API channel adapter — updates → InteractionEvents, Bot API out.

Offline against real Telegram Update shapes; transport injected (no network, fake token). A live
smoke (getMe + poll) runs only when REDEVOPS_BOT_TOKEN is set.
"""
from __future__ import annotations

import json
import os

import pytest

from runtime_contracts import Channel, Modality
from agentic_os.interaction import TelegramChannelAdapter, TelegramChannelError


def _msg(**fields):
    base = {"message_id": 7, "from": {"id": 4242, "username": "alex"},
            "chat": {"id": 4242, "type": "private"}, "date": 1725600000}
    base.update(fields)
    return {"update_id": 100, "message": base}


class _FakeTransport:
    def __init__(self, by_method):
        self.by_method = by_method    # dict: method-name-substr -> (status, bytes)
        self.calls = []
    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        for k, v in self.by_method.items():
            if k in url:
                return v
        return 404, b'{"ok":false,"description":"no stub"}'


def _ok(result):
    return (200, json.dumps({"ok": True, "result": result}).encode())


def _adapter(by_method=None):
    ft = _FakeTransport(by_method or {})
    return TelegramChannelAdapter(token="TESTTOKEN", transport=ft), ft


def test_parse_text_message():
    a, _ = _adapter()
    ev = a.parse_update(_msg(text="my car won't start"))
    assert ev.channel is Channel.TELEGRAM and ev.modality is Modality.TEXT
    assert ev.conversation_id == "tg:4242" and ev.participant_ref == "4242"
    assert ev.text == "my car won't start" and ev.interaction_id == "tg:4242:7"


def test_parse_voice_photo_video_carry_file_id():
    a, _ = _adapter()
    voice = a.parse_update(_msg(voice={"file_id": "VOICE1", "mime_type": "audio/ogg", "duration": 5}))
    photo = a.parse_update(_msg(photo=[{"file_id": "small"}, {"file_id": "PHOTO_LARGE"}],
                                caption="dashboard light"))
    video = a.parse_update(_msg(video={"file_id": "VID1"}))
    assert voice.modality is Modality.AUDIO and voice.artifact_ref == "VOICE1"
    assert photo.modality is Modality.IMAGE and photo.artifact_ref == "PHOTO_LARGE"  # largest size
    assert photo.text == "dashboard light"
    assert video.modality is Modality.DOCUMENT and video.artifact_ref == "VID1"


def test_poll_advances_offset_and_returns_events():
    a, ft = _adapter()
    ft.by_method["getUpdates"] = _ok([{"update_id": 100, "message": _msg(text="hi")["message"]}])
    evs = a.poll()
    assert len(evs) == 1 and evs[0].text == "hi"
    # offset advanced to update_id+1
    a.poll()
    sent = json.loads(ft.calls[-1]["body"])
    assert sent["offset"] == 101


def test_fetch_file_two_step():
    a, ft = _adapter({"getFile": _ok({"file_path": "voice/file_1.oga"}),
                      "/file/botTESTTOKEN/voice/file_1.oga": (200, b"OggS-audio-bytes")})
    data, mime = a.fetch_file("VOICE1")
    assert data == b"OggS-audio-bytes" and mime == "audio/ogg"


def test_send_text_and_message_id():
    a, ft = _adapter({"sendMessage": _ok({"message_id": 55})})
    r = a.send_text("tg:4242", "Likely a cylinder-2 misfire — avoid driving.")
    assert r.status == "sent" and r.provider_ref == "55" and r.channel is Channel.TELEGRAM
    sent = json.loads(ft.calls[-1]["body"])
    assert sent["chat_id"] == "4242" and sent["text"].startswith("Likely")   # tg: prefix stripped


def test_send_failure_returns_failed_receipt():
    a, _ = _adapter({"sendMessage": (400, b'{"ok":false,"description":"chat not found"}')})
    assert a.send_text("tg:4242", "hi").status == "failed"


def test_missing_token_is_clear_error(monkeypatch):
    monkeypatch.delenv("REDEVOPS_BOT_TOKEN", raising=False)
    a = TelegramChannelAdapter(token="", transport=_FakeTransport({}))
    with pytest.raises(TelegramChannelError, match="REDEVOPS_BOT_TOKEN"):
        a.get_me()


def test_capabilities():
    caps, _ = _adapter()
    c = caps.capabilities()
    assert c["channel"] == "telegram" and c["voice_notes"] is True and "poll" in c["inbound"]


@pytest.mark.skipif(not os.environ.get("REDEVOPS_BOT_TOKEN"), reason="needs live REDEVOPS_BOT_TOKEN")
def test_live_get_me_and_poll():
    a = TelegramChannelAdapter()      # real token from env
    me = a.get_me()
    assert me.get("username") == "redevops_bot"
    assert isinstance(a.poll(timeout=0), list)   # empty is fine
