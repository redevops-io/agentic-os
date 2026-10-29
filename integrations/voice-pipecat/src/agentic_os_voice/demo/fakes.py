"""Fake speech-to-text, scripted LLM and fake text-to-speech for the synthetic demo.

None of these calls anything. The fake STT turns fixture 'audio' (silent PCM with a fixture id in its first bytes) into
the fixture's transcript. The LLM is a real Pipecat ``LLMService`` (so Pipecat's real function-call machinery,
interruption handling and timeouts run) whose only 'model' is a deterministic script. The fake TTS emits silent audio
and records what it would have said.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, FunctionCallFromLLM, Frame, InputAudioRawFrame, InterruptionFrame,
    LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame,
    TTSSpeakFrame, TTSStartedFrame, TTSStoppedFrame, TTSTextFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

AUDIO_MAGIC = b"FIXTURE:"
SAMPLE_RATE = 16000


def fixture_audio(utterance_id: str, ms: int = 200) -> bytes:
    """Silent 16-bit mono PCM whose first bytes carry the fixture id. There is no speech in it."""
    head = AUDIO_MAGIC + utterance_id.encode() + b";"
    body = b"\x00" * max(0, (SAMPLE_RATE * 2 * ms // 1000) - len(head))
    return head + body


class FakeSTT(FrameProcessor):
    """Fixture audio -> fixture transcript. Frames that are not fixture audio pass through."""

    def __init__(self, transcripts: Dict[str, str], **kw):
        super().__init__(**kw)
        self._transcripts = transcripts

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and bytes(frame.audio[:8]) == AUDIO_MAGIC:
            uid = bytes(frame.audio).split(b";", 1)[0][len(AUDIO_MAGIC):].decode()
            await self.push_frame(TranscriptionFrame(self._transcripts[uid], "caller", "2026-01-01T00:00:00.000Z"))
            return
        await self.push_frame(frame, direction)


# ── scripted LLM ────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Say:
    text: str


@dataclass
class SayResult:
    """Say the sentence the bridge attached to the last tool result. The only way this demo's bot talks about an outcome."""
    tool: str


@dataclass
class Call:
    name: str
    arguments: Dict[str, Any]


@dataclass
class View:
    """What the script may look at: the last message, the tool results so far, and the tools advertised right now."""
    last_user: str = ""
    last_tool: str = ""
    last_result: Dict[str, Any] = field(default_factory=dict)
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    advertised: List[str] = field(default_factory=list)
    turn: int = 0


Script = Callable[[View], List[Any]]


def _content_text(m: Dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return ""


class ScriptedLLM(LLMService):
    """A deterministic stand-in for a model. ``script(view)`` returns the actions for this inference: Say / SayResult / Call."""

    def __init__(self, script: Script, on_inference: Optional[Callable[[View], None]] = None, **kw):
        super().__init__(settings=LLMSettings(model="scripted", system_instruction=None, temperature=None, max_tokens=None, top_p=None,
                                              top_k=None, frequency_penalty=None, presence_penalty=None, seed=None,
                                              filter_incomplete_user_turns=False, user_turn_completion_config=None), **kw)
        self._script = script
        self._on_inference = on_inference
        self._turn = 0
        self.idle = asyncio.Event()           # set after an inference that made no tool call (the bot has nothing pending)
        self.inferences = 0
        self.last_view: Optional[View] = None

    def _view(self, ctx: Any) -> View:
        msgs = ctx.get_messages()
        names: Dict[str, str] = {}
        results: Dict[str, Dict[str, Any]] = {}
        v = View(turn=self._turn)
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            for tc in m.get("tool_calls") or []:
                names[tc.get("id", "")] = (tc.get("function") or {}).get("name", "")
            if role == "user":
                v.last_user, v.last_tool = _content_text(m), ""
            elif role in ("tool", "developer"):
                try:
                    res = json.loads(_content_text(m))
                except ValueError:
                    continue
                if not isinstance(res, dict):
                    continue
                call_id = m.get("tool_call_id") or res.get("tool_call_id", "")
                if res.get("type") == "async_tool":
                    # Pipecat's asynchronous-tool protocol: a 'running' placeholder now, the real result in a later developer message
                    if res.get("status") != "finished":
                        continue
                    try:
                        res = json.loads(res.get("result") or "{}")
                    except ValueError:
                        res = {}
                    if not isinstance(res, dict):
                        continue
                elif role == "developer":
                    continue
                name = names.get(call_id, "")
                results[name] = res
                v.last_tool, v.last_result = name, res
        v.results = results
        tools = getattr(ctx, "tools", None)
        v.advertised = [t.name for t in getattr(tools, "standard_tools", [])] if tools is not None and hasattr(tools, "standard_tools") else []
        return v

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.inferences += 1
        self.idle.clear()
        view = self._view(frame.context)
        self.last_view = view
        if self._on_inference:
            self._on_inference(view)
        self._turn += 1
        actions = self._script(view)
        calls: List[FunctionCallFromLLM] = []
        await self.push_frame(LLMFullResponseStartFrame())
        for i, a in enumerate(actions):
            if isinstance(a, SayResult):
                a = Say(str(view.results.get(a.tool, {}).get("say", "")))
            if isinstance(a, Say) and a.text:
                await self.push_frame(LLMTextFrame(a.text))
            elif isinstance(a, Call):
                calls.append(FunctionCallFromLLM(function_name=a.name, tool_call_id=f"call_{self._turn}_{i}", arguments=a.arguments,
                                                 context=frame.context))
        await self.push_frame(LLMFullResponseEndFrame())
        if calls:
            await self.run_function_calls(calls)
        else:
            self.idle.set()


# ── fake TTS + audio out ───────────────────────────────────────────────────────────────────────────────────────
class FakeTTS(FrameProcessor):
    """Text in, silent audio out. ``spoken`` lists what was fully 'said'; ``interrupted`` lists what a barge-in cut short."""

    def __init__(self, chunk_delay_s: float = 0.02, chunks: int = 3, **kw):
        super().__init__(**kw)
        self._buf: List[str] = []
        self._delay, self._chunks = chunk_delay_s, chunks
        self.spoken: List[str] = []
        self.interrupted: List[str] = []
        self.speaking = asyncio.Event()
        self.hold_next = False             # the next sentence is 'spoken' until a barge-in cuts it (deterministic interruption point)

    async def _say(self, text: str) -> None:
        hold, self.hold_next = self.hold_next, False          # the hold applies to this sentence only
        await self.push_frame(TTSStartedFrame())
        await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.speaking.set()
        try:
            await self.push_frame(TTSTextFrame(text, aggregated_by="sentence"))   # `text_type` from pipecat 1.13; the pin is 1.12.0
            for _ in range(self._chunks):
                await self.push_frame(TTSAudioRawFrame(b"\x00" * 640, SAMPLE_RATE, 1))
                await asyncio.sleep(self._delay)
            if hold:
                await asyncio.Event().wait()       # only an interruption (CancelledError) ends this sentence
            self.spoken.append(text)
        except asyncio.CancelledError:
            self.interrupted.append(text)
            raise
        finally:
            self.speaking.clear()
            await self.push_frame(TTSStoppedFrame())
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._buf.clear()
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMTextFrame):
            self._buf.append(frame.text)
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text, self._buf = " ".join(self._buf).strip(), []
            await self.push_frame(frame, direction)
            if text:
                await self._say(text)
        elif isinstance(frame, TTSSpeakFrame):
            await self._say(frame.text)
        else:
            await self.push_frame(frame, direction)
