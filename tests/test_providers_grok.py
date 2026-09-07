"""Grok (xAI) cloud SpeechProvider — STT + TTS, bring-your-own XAI_API_KEY, no local GPU.

Offline tests inject a fake transport (no network, no key). A live smoke round-trip runs only when
XAI_API_KEY is set. Endpoint shapes verified live 2026-09-06: /v1/tts wants {model,voice,text,language}
and returns MP3; /v1/stt (model grok-stt, multipart) returns {text,language,words[]}.
"""
from __future__ import annotations

import json
import os

import pytest

from runtime_contracts import MediaArtifact, Modality, TranscriptArtifact
from agentic_os.interaction import GrokSpeechProvider, SpeechProviderError


class _FakeTransport:
    """Records calls and returns canned (status, bytes) per URL suffix."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        for suffix, (status, data) in self.responses.items():
            if url.endswith(suffix):
                return status, data
        return 404, b'{"error":"no stub"}'


def _stt_ok():
    return (200, json.dumps({"text": "P zero three zero two", "language": "en",
                             "words": [{"text": "P0302", "start": 0.1, "end": 0.6}]}).encode())


def test_tts_builds_correct_request_and_returns_audio():
    ft = _FakeTransport({"/tts": (200, b"ID3\x03\x00fake-mp3-bytes")})
    p = GrokSpeechProvider(api_key="k", transport=ft)
    art, audio = p.synthesize("check engine light is on", language="en")
    assert audio.startswith(b"ID3")                    # mp3 bytes returned
    assert isinstance(art, MediaArtifact) and art.modality is Modality.AUDIO
    assert art.media_type == "audio/mpeg" and art.media_hash.startswith("sha256:")
    assert art.bytes_len == len(audio)
    # request shape: /v1/tts, JSON with the fields xAI requires
    call = ft.calls[0]
    assert call["url"].endswith("/v1/tts")
    sent = json.loads(call["body"])
    assert sent == {"model": "grok-voice-latest", "voice": "eve",
                    "text": "check engine light is on", "language": "en"}
    assert call["headers"]["Authorization"] == "Bearer k"


def test_stt_parses_words_into_transcript_segments():
    ft = _FakeTransport({"/stt": _stt_ok()})
    p = GrokSpeechProvider(api_key="k", transport=ft)
    tr = p.transcribe_audio(b"fake-audio", media_type="audio/mpeg", language="en")
    assert isinstance(tr, TranscriptArtifact)
    assert tr.asr_model == "grok-stt" and tr.language == "en"
    assert tr.text == "P0302"                          # from word-level tokens
    assert tr.segments[0].start_ms == 100 and tr.segments[0].end_ms == 600
    assert tr.content_hash.startswith("rcv1:")
    # request: multipart to /v1/stt with model=grok-stt
    call = ft.calls[0]
    assert call["url"].endswith("/v1/stt")
    assert b'name="model"' in call["body"] and b"grok-stt" in call["body"]
    assert b'name="file"' in call["body"] and b"fake-audio" in call["body"]


def test_transcribe_from_media_uri(tmp_path):
    audio = tmp_path / "clip.mp3"; audio.write_bytes(b"bytes-on-disk")
    ft = _FakeTransport({"/stt": _stt_ok()})
    p = GrokSpeechProvider(api_key="k", transport=ft)
    media = MediaArtifact(artifact_id="a1", modality=Modality.AUDIO, media_hash="sha256:x",
                          media_type="audio/mpeg", uri=str(audio))
    tr = p.transcribe(media)
    assert tr.source_media_hash == "sha256:x" and b"bytes-on-disk" in ft.calls[0]["body"]


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(SpeechProviderError, match="XAI_API_KEY"):
        GrokSpeechProvider(transport=_FakeTransport({})).synthesize_bytes("hi")


def test_http_error_is_surfaced():
    ft = _FakeTransport({"/tts": (403, b'{"error":"Team is not authorized"}')})
    p = GrokSpeechProvider(api_key="k", transport=ft)
    with pytest.raises(SpeechProviderError, match="403"):
        p.synthesize_bytes("hi")


def test_capabilities_advertise_byo_key_and_models():
    caps = GrokSpeechProvider(api_key="k").capabilities()
    assert caps["provider"] == "xai-grok-voice" and caps["byo_key"] == "XAI_API_KEY"
    assert caps["stt_model"] == "grok-stt" and caps["tts_model"] == "grok-voice-latest"


@pytest.mark.skipif(not os.environ.get("XAI_API_KEY"), reason="needs live XAI_API_KEY")
def test_live_round_trip_tts_then_stt():
    p = GrokSpeechProvider()                            # real transport, key from env
    _, audio = p.synthesize("hello world", language="en")
    assert audio and len(audio) > 1000
    tr = p.transcribe_audio(audio, media_type="audio/mpeg", language="en")
    assert "hello" in tr.text.lower()
