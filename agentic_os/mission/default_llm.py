"""A default LLM for the first bootstrap steps — when the user has configured no credentials.

Sidekick's guided configuration needs *a* model to parse "what do you want this device to do?"
before the user has set anything up. This resolves one, in priority order, and **never ships a
key** (a bundled key is a secret and an abuse magnet):

1. **User credentials** — a provider key already in the environment (or a bring-your-own
   ``RDO_LLM_*``). Always wins.
2. **A reachable local model** — e.g. the on-box qwen shim / ``MODEL_ENDPOINT``. No third party,
   no key, best for privacy.
3. **Guided free tier** — if neither exists, recommend a genuinely free provider and hand the UX
   a signup URL to walk a non-technical user through (a one-time paste, stored on-device).

The registry below is the free/promo landscape (researched 2026-09): standing free tiers with
**no credit card** are preferred, and providers that **train on your data** on the free tier are
flagged so a governed install can avoid them by default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str                 # OpenAI-compatible endpoint
    model: str                    # a sensible free default model
    api_key_env: str              # env var the key is read from
    signup_url: str
    needs_card: bool = False
    trains_on_data: bool = False  # on the FREE tier
    note: str = ""


# Ordered by suitability as a *guided* default: no card + does-not-train + standing-free first.
# (Gemini is the most generous free tier but trains on free-tier data; xAI Grok is promo credit.)
REGISTRY: Tuple[Provider, ...] = (
    Provider("groq", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
             "GROQ_API_KEY", "https://console.groq.com/keys",
             note="fast, no card, does not train on your data"),
    Provider("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "llama-3.3-70b",
             "CEREBRAS_API_KEY", "https://cloud.cerebras.ai", note="1M tokens/day free, no card"),
    Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
             "deepseek/deepseek-r1:free", "OPENROUTER_API_KEY", "https://openrouter.ai/keys",
             note="~30 free models via one endpoint, no card"),
    Provider("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
             "gemini-2.5-flash", "GEMINI_API_KEY", "https://aistudio.google.com/apikey",
             trains_on_data=True, note="most generous free tier, no card, but trains on free-tier data"),
    Provider("xai", "xAI Grok", "https://api.x.ai/v1", "grok-4.6", "XAI_API_KEY",
             "https://console.x.ai", note="$25 promo credit on signup (expires); data-sharing off by default"),
)
_BY_ID = {p.id: p for p in REGISTRY}

# Provider key env vars checked for pre-configured user credentials, plus common generic ones.
_USER_KEY_ENVS = tuple(p.api_key_env for p in REGISTRY) + ("OPENAI_API_KEY",)


@dataclass(frozen=True)
class LLMChoice:
    source: str                   # "user_creds" | "local" | "guided"
    base_url: str = ""
    model: str = ""
    provider: str = ""
    api_key_env: str = ""
    needs_setup: bool = False     # True ⇒ the UX must obtain a key (guided)
    signup_url: str = ""
    trains_on_data: bool = False
    message: str = ""
    alternatives: Tuple[str, ...] = ()   # other provider ids the UX may offer


def _default_local_probe(env: Dict[str, str]) -> Optional[str]:
    """Return a reachable local model base URL, or None. Env-only by default (offline/testable);
    a launcher can inject a real reachability check."""
    url = env.get("MODEL_ENDPOINT") or env.get("RDO_LOCAL_MODEL_URL")
    return url or None


def resolve_llm(env: Optional[Dict[str, str]] = None, *,
                local_probe: Optional[Callable[[Dict[str, str]], Optional[str]]] = None,
                prefer: Tuple[str, ...] = ()) -> LLMChoice:
    """Pick the LLM for the first bootstrap steps. Never returns or embeds a key.

    ``prefer`` may reorder the guided recommendation (e.g. the user picked a provider in the UI).
    """
    env = dict(os.environ if env is None else env)
    probe = local_probe or _default_local_probe

    # 1) bring-your-own generic endpoint
    if env.get("RDO_LLM_BASE_URL") and env.get("RDO_LLM_API_KEY"):
        return LLMChoice(source="user_creds", base_url=env["RDO_LLM_BASE_URL"],
                         model=env.get("RDO_LLM_MODEL", ""), provider="custom",
                         api_key_env="RDO_LLM_API_KEY", message="using your configured LLM endpoint")
    # 1b) a known provider's key already set
    for prov in REGISTRY:
        if env.get(prov.api_key_env):
            return LLMChoice(source="user_creds", base_url=prov.base_url, model=prov.model,
                             provider=prov.id, api_key_env=prov.api_key_env,
                             trains_on_data=prov.trains_on_data,
                             message=f"using your {prov.name} credentials")
    if env.get("OPENAI_API_KEY"):
        return LLMChoice(source="user_creds", base_url="https://api.openai.com/v1",
                         model="gpt-4o-mini", provider="openai", api_key_env="OPENAI_API_KEY",
                         message="using your OpenAI credentials")

    # 2) a reachable local model
    local = probe(env)
    if local:
        return LLMChoice(source="local", base_url=local,
                         model=env.get("MODEL_NAME", env.get("RDO_LOCAL_MODEL", "")),
                         provider="local", message="using the local model on this device")

    # 3) guided free tier — recommend, do not fabricate a key
    order = [p for pid in prefer for p in (_BY_ID.get(pid),) if p] + \
            [p for p in REGISTRY if p.id not in prefer]
    top = order[0]
    return LLMChoice(
        source="guided", provider=top.id, base_url=top.base_url, model=top.model,
        api_key_env=top.api_key_env, needs_setup=True, signup_url=top.signup_url,
        trains_on_data=top.trains_on_data,
        message=(f"No LLM configured. Recommended free option: {top.name} — {top.note}. "
                 f"Create a free key at {top.signup_url}, then it is stored on this device."),
        alternatives=tuple(p.id for p in order[1:]))
