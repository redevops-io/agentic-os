"""PAIR runtime governance — node telemetry (#4), policy fallback (#5), Sidekick copy (#6)."""
from types import SimpleNamespace

import pytest

from agentic_os.mission.store import EventStore
from agentic_os.mission.default_llm import LLMChoice
from agentic_os.mission.pair_runtime import (
    pair_node_from_headers, record_inference, INFERENCE_SERVED,
    FallbackPolicy, build_llm_chain, attempt, AllProvidersFailed,
)
from agentic_os.mission.bootstrap import _summary_lines, INSTALLED


# ── #4 physical-node telemetry ────────────────────────────────────────────────────────────

def test_node_header_extraction_is_case_insensitive():
    assert pair_node_from_headers({"X-Pair-Node": "gpu-box-1"}) == "gpu-box-1"
    assert pair_node_from_headers({"x-served-by": "mac-studio"}) == "mac-studio"
    assert pair_node_from_headers({"content-type": "application/json"}) == ""
    assert pair_node_from_headers(None) == ""


def test_record_inference_persists_the_chosen_node():
    store = EventStore()
    record_inference(store, mission_id="m1", provider="pair", model="qwen",
                     node="gpu-box-1", base_url="http://127.0.0.1:1234/v1")
    (e,) = [e for e in store.for_mission("m1") if e.type == INFERENCE_SERVED]
    assert e.payload["node"] == "gpu-box-1" and e.payload["provider"] == "pair"


def test_record_inference_defaults_unknown_node():
    store = EventStore()
    record_inference(store, mission_id="m1", provider="pair", model="qwen")
    assert store.for_mission("m1")[0].payload["node"] == "unknown"


# ── #5 fallback by Mission policy ─────────────────────────────────────────────────────────

def _resolve_pair(env=None): return LLMChoice(source="pair", provider="pair",
                                              base_url="http://127.0.0.1:1234/v1", model="qwen")
def _resolve_cloud(env=None): return LLMChoice(source="user_creds", provider="groq",
                                               base_url="https://api.groq.com/openai/v1", model="llama")


def test_local_primary_gets_cloud_fallback_when_allowed():
    chain = build_llm_chain(policy=FallbackPolicy(allow_cloud_fallback=True, cloud_provider="groq"),
                            resolve=_resolve_pair)
    assert [c.provider for c in chain] == ["pair", "groq"]


def test_local_only_mission_has_no_cloud_fallback():
    chain = build_llm_chain(policy=FallbackPolicy(local_only=True), resolve=_resolve_pair)
    assert [c.source for c in chain] == ["pair"]      # fail closed, never leak to cloud


def test_cloud_primary_needs_no_fallback():
    chain = build_llm_chain(resolve=_resolve_cloud)
    assert len(chain) == 1 and chain[0].provider == "groq"


def test_attempt_falls_over_to_the_next_provider():
    store = EventStore()
    chain = build_llm_chain(resolve=_resolve_pair)     # [pair, groq]
    calls = []
    def call_fn(choice):
        calls.append(choice.provider)
        if choice.provider == "pair":
            raise ConnectionError("node asleep")
        return {"ok": choice.provider}
    result, used = attempt(chain, call_fn, store=store, mission_id="m1")
    assert result == {"ok": "groq"} and used.provider == "groq"
    assert calls == ["pair", "groq"]
    assert any(e.type == "InferenceProviderFailed" for e in store.for_mission("m1"))


def test_attempt_raises_when_all_fail():
    chain = build_llm_chain(resolve=_resolve_pair)
    with pytest.raises(AllProvidersFailed):
        attempt(chain, lambda c: (_ for _ in ()).throw(RuntimeError("down")))


# ── #6 Sidekick "local AI found" copy ─────────────────────────────────────────────────────

def test_bootstrap_says_local_ai_found_for_pair():
    llm = SimpleNamespace(source="pair", message="using local AI via NVIDIA PAIR")
    lines = _summary_lines(INSTALLED, None, llm, None, None)
    assert any("Local AI found" in ln for ln in lines)


def test_bootstrap_guided_still_prompts_setup():
    llm = SimpleNamespace(source="guided", message="Recommended free option: Groq …")
    lines = _summary_lines(INSTALLED, None, llm, None, None)
    assert any("LLM setup needed" in ln for ln in lines)
