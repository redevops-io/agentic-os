"""agentic_os.app — the governed clients an app imports (plan §3.6).

``agentic_os.app.llm`` is the only LLM client an app may use (every model call goes through the
``GovernedModelRouter``). ``agentic_os.app.context`` (retrieval through Context Runtime) lands in a
later slice.
"""
from __future__ import annotations

from .llm import (
    APP_LLM_CONTRACT_VERSION,
    DEFAULT_UNCLASSIFIED_AS,
    GovernedLLM,
    LLMResult,
    Transport,
)

__all__ = [
    "APP_LLM_CONTRACT_VERSION",
    "DEFAULT_UNCLASSIFIED_AS",
    "GovernedLLM",
    "LLMResult",
    "Transport",
]
