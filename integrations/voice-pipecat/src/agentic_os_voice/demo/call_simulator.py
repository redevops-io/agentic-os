"""Deterministic call simulator: feeds fixture 'audio' and transcripts into a Pipecat pipeline and injects interruptions.

A fixture timeline is a list of steps. Nothing is random and nothing waits on wall-clock sleeps for correctness: every
wait is an event the pipeline itself sets (bot idle, bot speaking, a tool call inside the mock CRM).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

from pipecat.frames.frames import (
    InputAudioRawFrame, ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame, TranscriptionFrame)

from .fakes import SAMPLE_RATE, fixture_audio

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class CallSimulator:
    fixture: dict
    worker: Any                                   # the Pipecat PipelineWorker
    llm: Any                                      # ScriptedLLM (its .idle event marks 'nothing pending')
    tts: Any                                      # FakeTTS (its .speaking event marks 'bot is talking')
    host: Dict[str, Callable[..., Awaitable[Any]]] = field(default_factory=dict)   # host-side operations a step may name
    log: List[str] = field(default_factory=list)

    async def _user_turn(self, text: str, utterance_id: str, *, as_audio: bool) -> None:
        await self.worker.queue_frame(ProposedUserStartedSpeakingFrame())
        if as_audio:
            await self.worker.queue_frame(InputAudioRawFrame(audio=fixture_audio(utterance_id), sample_rate=SAMPLE_RATE, num_channels=1))
        else:
            await self.worker.queue_frame(TranscriptionFrame(text, "caller", "2026-01-01T00:00:00.000Z"))
        await self.worker.queue_frame(ProposedUserStoppedSpeakingFrame())

    async def _wait_idle(self, timeout: float = 20.0) -> None:
        # the bot has no inference pending AND is not mid-sentence; two ticks let queued frames land
        for _ in range(3):
            await asyncio.wait_for(self.llm.idle.wait(), timeout)
            await asyncio.sleep(0.05)
            while self.tts.speaking.is_set():
                await asyncio.sleep(0.01)

    async def _inference_after(self, before: int, timeout: float = 20.0) -> None:
        # a user turn must have produced a model inference before 'idle' means anything again
        async def wait() -> None:
            while self.llm.inferences <= before:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout)

    async def run(self) -> None:
        for step in self.fixture["timeline"]:
            op = step["do"]
            before = self.llm.inferences
            if os.environ.get("VOICE_DEMO_TRACE"):
                print("SIM", step, "inferences", before, flush=True)
            self.log.append(op + (":" + step.get("id", step.get("name", "")) if ("id" in step or "name" in step) else ""))
            if op == "wait_idle":
                await self._wait_idle()
            elif op in ("say", "say_text"):
                uid = step["id"]
                await self._user_turn(self.fixture["utterances"][uid], uid, as_audio=(op == "say"))
                if step.get("expect_inference", True):
                    await self._inference_after(before)
            elif op == "hold_next_speech":
                self.tts.hold_next = True
            elif op == "barge_in_while_bot_speaks":
                await asyncio.wait_for(self.tts.speaking.wait(), 20)
                uid = step["id"]
                await self._user_turn(self.fixture["utterances"][uid], uid, as_audio=True)
                await self._inference_after(before)
            elif op == "host":
                await self.host[step["name"]](**step.get("args", {}))
            elif op in self.host:                       # a named host-side operation (verification, a test hook, ...)
                await self.host[op](**step.get("args", {}))
            elif op == "end":
                await self._wait_idle()
                return
            else:
                raise ValueError(f"unknown step {op!r}")
