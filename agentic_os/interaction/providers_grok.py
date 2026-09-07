"""Grok (xAI) cloud speech — the default, no-local-GPU SpeechProvider (bring-your-own-key).

Local voice agents (NeMo) need a GPU, so they don't fit the one-click agentic-apps stack. xAI's voice
API does: it runs entirely in the cloud, keyed by the user's ``XAI_API_KEY`` — the same key the stack
already uses for the Grok LLM. This implements the ``runtime_contracts`` ``SpeechProvider`` seam over
the two REST endpoints (verified live 2026-09-06):

  * STT  ``POST https://api.x.ai/v1/stt``   (multipart) model ``grok-stt``  → ``{text, language, words[]}``
  * TTS  ``POST https://api.x.ai/v1/tts``   (json)      model ``grok-voice-latest``, voice ``eve`` →
          audio bytes (MP3). Requires ``text`` + ``language``.

(xAI also offers a realtime speech-to-speech WebSocket at ``wss://api.x.ai/v1/realtime`` — a fuller,
lower-latency path modelled by a future provider; this REST STT/TTS pair is the simple, robust default.)

Transport is a single injectable callable so the whole thing is unit-tested offline with no network and
no key; a live smoke test runs only when ``XAI_API_KEY`` is set.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from runtime_contracts import MediaArtifact, Modality, SpeakerRef, SpeechSegment, TranscriptArtifact

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_STT_MODEL = "grok-stt"
DEFAULT_TTS_MODEL = "grok-voice-latest"
DEFAULT_VOICE = "eve"


class SpeechProviderError(RuntimeError):
    """A speech call failed (missing key, HTTP error, bad response)."""


# A transport takes (method, url, headers, body_bytes) and returns (status, response_bytes).
Transport = Callable[[str, str, Mapping[str, str], bytes], Tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=dict(headers), data=body)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # surface the body so callers see xAI's error
        return e.code, e.read()


def _multipart(fields: Mapping[str, str], *, file_field: str, filename: str,
               file_bytes: bytes, file_type: str) -> Tuple[bytes, str]:
    """Encode a multipart/form-data body (urllib has no built-in encoder)."""
    boundary = f"----rdo{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    for k, v in fields.items():
        parts += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{k}"'.encode(),
                  b"", str(v).encode()]
    parts += [f"--{boundary}".encode(),
              f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
              f"Content-Type: {file_type}".encode(), b"", file_bytes]
    parts += [f"--{boundary}--".encode(), b""]
    return crlf.join(parts), f"multipart/form-data; boundary={boundary}"


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


class GrokSpeechProvider:
    """xAI cloud STT+TTS behind the SpeechProvider seam. Bring-your-own ``XAI_API_KEY``."""

    def __init__(self, *, api_key: Optional[str] = None, api_key_env: str = "XAI_API_KEY",
                 base_url: str = DEFAULT_BASE_URL, stt_model: str = DEFAULT_STT_MODEL,
                 tts_model: str = DEFAULT_TTS_MODEL, voice: str = DEFAULT_VOICE,
                 language: str = "en", transport: Optional[Transport] = None) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.voice = voice
        self.language = language
        self._transport = transport or _urllib_transport

    def _key(self) -> str:
        key = self._api_key or os.environ.get(self._api_key_env, "")
        if not key:
            raise SpeechProviderError(
                f"no xAI API key — set {self._api_key_env} (bring your own key)")
        return key

    def capabilities(self) -> Mapping[str, Any]:
        return {"provider": "xai-grok-voice", "transport": "rest",
                "stt_model": self.stt_model, "tts_model": self.tts_model, "voice": self.voice,
                "modalities": [Modality.AUDIO.value, Modality.TEXT.value],
                "byo_key": self._api_key_env, "realtime_ws": f"{self.base_url}/realtime"}

    # ---- STT: audio -> TranscriptArtifact ----------------------------------------------------------

    def transcribe_audio(self, audio: bytes, *, media_type: str = "audio/mpeg",
                         language: Optional[str] = None, source_media_hash: str = "",
                         artifact_id: str = "") -> TranscriptArtifact:
        fields: Dict[str, str] = {"model": self.stt_model}
        if language:
            fields["language"] = language
        body, ctype = _multipart(fields, file_field="file", filename="audio",
                                 file_bytes=audio, file_type=media_type)
        headers = {"Authorization": f"Bearer {self._key()}", "Content-Type": ctype}
        status, resp = self._transport("POST", f"{self.base_url}/stt", headers, body)
        if status != 200:
            raise SpeechProviderError(f"stt failed HTTP {status}: {resp[:200].decode('utf-8', 'ignore')}")
        d = json.loads(resp)
        words = d.get("words") or []
        segs = tuple(SpeechSegment(text=w.get("text", ""),
                                   start_ms=int(float(w.get("start", 0)) * 1000),
                                   end_ms=int(float(w.get("end", 0)) * 1000))
                     for w in words) or (SpeechSegment(text=d.get("text", "")),)
        return TranscriptArtifact(
            artifact_id=artifact_id or f"grok-stt:{_sha256(audio)[7:19]}",
            source_media_hash=source_media_hash or _sha256(audio),
            segments=segs, asr_model=self.stt_model, asr_version="latest",
            language=d.get("language", language or self.language))

    def transcribe(self, media: MediaArtifact, *, audio_bytes: Optional[bytes] = None,
                   language: Optional[str] = None) -> TranscriptArtifact:
        """Transcribe a MediaArtifact. Pass ``audio_bytes`` directly, or the artifact's ``uri`` must be
        a local file path the provider can read."""
        if audio_bytes is None:
            if not media.uri:
                raise SpeechProviderError("no audio: pass audio_bytes= or set media.uri to a file path")
            with open(media.uri, "rb") as fh:
                audio_bytes = fh.read()
        return self.transcribe_audio(audio_bytes, media_type=media.media_type or "audio/mpeg",
                                     language=language, source_media_hash=media.media_hash,
                                     artifact_id=f"grok-stt:{media.artifact_id}")

    # ---- TTS: text -> audio -----------------------------------------------------------------------

    def synthesize_bytes(self, text: str, *, voice: Optional[str] = None,
                         language: Optional[str] = None) -> bytes:
        payload = json.dumps({"model": self.tts_model, "voice": voice or self.voice,
                              "text": text, "language": language or self.language}).encode()
        headers = {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"}
        status, resp = self._transport("POST", f"{self.base_url}/tts", headers, payload)
        if status != 200:
            raise SpeechProviderError(f"tts failed HTTP {status}: {resp[:200].decode('utf-8', 'ignore')}")
        return resp

    def synthesize(self, text: str, *, voice: Optional[str] = None,
                   language: Optional[str] = None) -> Tuple[MediaArtifact, bytes]:
        """Synthesize speech → (MediaArtifact metadata, audio bytes). Returns the bytes alongside the
        artifact because the contract's MediaArtifact references audio by hash, never inlines it."""
        audio = self.synthesize_bytes(text, voice=voice, language=language)
        h = _sha256(audio)
        art = MediaArtifact(artifact_id=f"grok-tts:{h[7:19]}", modality=Modality.AUDIO,
                            media_hash=h, media_type="audio/mpeg", bytes_len=len(audio))
        return art, audio
