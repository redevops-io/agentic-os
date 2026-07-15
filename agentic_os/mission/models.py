"""Thinking-model adapter for the Mission Planner.

The planner is the one place the LLM runs. Production points it at the local, self-hosted
`RedHatAI/Qwen3-Coder-Next-NVFP4` (NVFP4, fits gpu0/48GB, thinking mode) via an OpenAI-compatible
endpoint — the same shape the agent apps already use. Dependency-free (urllib), with an injectable
transport so tests exercise parsing without a GPU.
"""
from __future__ import annotations

import os
import re

from .util import post_json, Transport


class OpenAICompatModel:
    """A `ThinkModel` (has .complete) over an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 thinking: bool = True, temperature: float = 0.2, timeout: float = 90.0,
                 transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.thinking = thinking
        self.temperature = temperature
        self.timeout = timeout
        self.transport = transport

    def complete(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if self.thinking:
            # local vLLM/llama.cpp servers gate reasoning via chat_template_kwargs; harmless if ignored
            body["chat_template_kwargs"] = {"enable_thinking": True}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = post_json(f"{self.base_url}/chat/completions", body, headers, self.timeout, self.transport)
        return _content(data)

    @classmethod
    def from_env(cls, transport: Transport | None = None) -> "OpenAICompatModel | None":
        base = os.environ.get("MISSION_PLANNER_BASE_URL")
        model = os.environ.get("MISSION_PLANNER_MODEL", "Qwen3-Coder-Next-NVFP4")
        if not base:
            return None
        return cls(base, model, api_key=os.environ.get("MISSION_PLANNER_API_KEY"),
                   thinking=os.environ.get("MISSION_PLANNER_THINKING", "1") != "0",
                   transport=transport)


def _content(data: dict) -> str:
    try:
        msg = data["choices"][0]["message"]
        # some servers split reasoning out; if present, keep only the final content
        return msg.get("content") or msg.get("reasoning_content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_thinking(text: str) -> str:
    """Drop <think>…</think> reasoning blocks so JSON extraction sees only the answer."""
    return _THINK.sub("", text or "").strip()
