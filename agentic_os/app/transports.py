"""Concrete Transport backends for the governed app.llm client (plan §3.6).

The kernel ships the governance (routing + receipts); the inference call is a provider ``Transport``
the deployment injects. ``OpenAICompatibleTransport`` is the reusable real backend for any
OpenAI-compatible endpoint (vLLM / self-hosted Qwen behind REDEVOPS_LLM_BASE_URL, Azure/Bedrock/
Vertex gateways, …). It calls the endpoint the GovernedModelRouter ALREADY chose — it reads the
base URL from ``endpoint.network_route`` (falling back to a configured default), so it can never
re-route around the governance decision. httpx is imported lazily so the kernel keeps no hard HTTP
dependency.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from agentic_os.governance.routing import ModelEndpoint


@dataclass
class OpenAICompatibleTransport:
    """Transport over an OpenAI-compatible ``/chat/completions`` API."""

    api_key: str = "EMPTY"
    base_url: str = ""              # default base; a routed endpoint's network_route overrides it
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "OpenAICompatibleTransport":
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY", "EMPTY") or "EMPTY",
            base_url=os.environ.get("REDEVOPS_LLM_BASE_URL", "") or "",
            timeout=float(os.environ.get("REDEVOPS_LLM_TIMEOUT", "60") or 60),
        )

    def _endpoint_base(self, endpoint: ModelEndpoint) -> str:
        base = getattr(endpoint, "network_route", "") or self.base_url
        if not base:
            raise RuntimeError(
                f"no base URL for endpoint {endpoint.model_id!r} (set endpoint.network_route or "
                f"REDEVOPS_LLM_BASE_URL)")
        return base.rstrip("/")

    def generate(self, endpoint: ModelEndpoint, prompt: str, *, system: str = "",
                 max_tokens: int = 512, temperature: float = 0.2, **kwargs: Any) -> str:
        import httpx  # lazy: kernel keeps no hard HTTP dependency

        url = self._endpoint_base(endpoint) + "/chat/completions"
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        payload = {"model": endpoint.model_id, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature}
        resp = httpx.post(url, json=payload,
                          headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


__all__ = ["OpenAICompatibleTransport"]
