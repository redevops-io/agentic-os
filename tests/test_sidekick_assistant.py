"""Sidekick's grounded LLM fallback — answers out-of-KB questions strictly from the curated
corpus, via an injected model. No network here: the model is a fake ChatModel."""
from __future__ import annotations

from agentic_os import projects_api
from agentic_os.sidekick_assistant import (
    GroundedSidekick,
    OpenAICompatModel,
    SYSTEM_PROMPT,
    _rank_source_ids,
    _strip_sources_line,
    build_user_prompt,
    make_grounded_sidekick,
    sidekick_model_from_env,
)
from agentic_os.stack_knowledge import STACK_KNOWLEDGE


class _FakeModel:
    """Records the prompts it was given and returns a canned answer."""
    def __init__(self, reply: str):
        self.reply = reply
        self.system = None
        self.user = None
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        self.system, self.user = system, user
        return self.reply


class _BoomModel:
    def complete(self, system: str, user: str) -> str:
        raise RuntimeError("model exploded")


# ── the seam ─────────────────────────────────────────────────────────────────────
def test_no_model_is_a_noop_fallback():
    assert GroundedSidekick(model=None).answer("anything at all?") is None


def test_grounded_answer_strips_sources_line_and_resolves_refs():
    m = _FakeModel("Your tokens never reach the model.\nSOURCES: secrets-to-model")
    ga = GroundedSidekick(model=m).answer("in plain terms, could the AI leak my keys?")
    assert ga is not None and ga.via_model
    assert ga.text == "Your tokens never reach the model."      # the SOURCES: line is stripped off
    src = {e.id: e.source for e in STACK_KNOWLEDGE}["secrets-to-model"]
    assert ga.sources == (src,)                                  # cited id → its enforcement ref


def test_prompt_grounds_on_full_kb_and_carries_the_rules():
    # every curated entry id appears in the grounding block
    prompt = build_user_prompt("where does my data go?", {"section": "Overview"})
    for e in STACK_KNOWLEDGE:
        assert f"[{e.id}]" in prompt
    assert "Overview" in prompt                                  # live context threaded in
    # the system prompt encodes: ground-only, don't-invent, decline-to-contact, cite sources
    s = SYSTEM_PROMPT.lower()
    assert "only from the facts" in s
    assert "never invent" in s
    assert "info@redevops.io" in s and "#agentic-apps" in s
    assert "sources:" in s


def test_declined_answer_has_no_fabricated_source():
    m = _FakeModel("I'm not certain about that — ask #agentic-apps on Slack.\nSOURCES: none")
    ga = GroundedSidekick(model=m).answer("what's your parent company's stock ticker?")
    assert ga is not None
    assert ga.sources == ()                                      # nothing cited, nothing invented


def test_model_error_never_breaks_the_reply():
    assert GroundedSidekick(model=_BoomModel()).answer("hello?") is None


def test_empty_completion_returns_none():
    assert GroundedSidekick(model=_FakeModel("   ")).answer("hello?") is None


# ── helpers ──────────────────────────────────────────────────────────────────────
def test_strip_sources_line_variants():
    assert _strip_sources_line("Ans.\nSOURCES: a, b") == ("Ans.", ("a", "b"))
    assert _strip_sources_line("Ans.\nSOURCES: none") == ("Ans.", ())
    assert _strip_sources_line("No trailing line.") == ("No trailing line.", ())


def test_rank_source_ids_uses_keyword_scoring():
    refs = _rank_source_ids("how do I revoke access to an app?")
    assert any("CredentialBroker" in r or "revoke" in r.lower() for r in refs)


def test_facts_block_includes_the_detail_tier_when_present():
    # duck-typed stand-ins: _facts_block reads .detail via getattr, so a tiered entry's deeper
    # technical tier is folded into the grounding, and an entry without one is unaffected.
    from types import SimpleNamespace
    from agentic_os.sidekick_assistant import _facts_block
    tiered = SimpleNamespace(id="context-runtime", topic="How it works", question="how?",
                             answer="overview text", source="redevops.io/in-plain-english",
                             detail="the deep technical bit")
    plain = SimpleNamespace(id="revoke", topic="Access & scope", question="revoke?",
                            answer="disconnect it", source="broker")  # no .detail attr at all
    block = _facts_block((tiered, plain))
    assert "overview text" in block and "the deep technical bit" in block
    assert "More technical detail" in block
    assert "disconnect it" in block           # the detail-less entry still renders fine


# ── env wiring ───────────────────────────────────────────────────────────────────
def test_from_env_none_without_base_url():
    assert sidekick_model_from_env({}) is None
    assert make_grounded_sidekick({}).model is None


def test_from_env_builds_openai_compat_when_base_set():
    m = sidekick_model_from_env({"SIDEKICK_MODEL_BASE_URL": "http://127.0.0.1:8000/",
                                 "SIDEKICK_MODEL_NAME": "local-qwen"})
    assert isinstance(m, OpenAICompatModel)
    assert m.base_url == "http://127.0.0.1:8000" and m.model == "local-qwen"


def test_openai_compat_extracts_content_not_reasoning(monkeypatch):
    import io, json as _json, urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    payload = {"choices": [{"message": {"role": "assistant",
                                        "reasoning_content": "thinking…",
                                        "content": "The final grounded answer."}}]}

    def fake_urlopen(req, timeout=None):
        return _Resp(_json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = OpenAICompatModel("http://x:8000", "m").complete("sys", "user")
    assert out == "The final grounded answer."          # reasoning_content is ignored


def test_openai_compat_returns_empty_on_error(monkeypatch):
    import urllib.request
    def boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert OpenAICompatModel("http://x:8000", "m").complete("s", "u") == ""


# ── integration through sidekick_reply ─────────────────────────────────────────────
def test_confident_kb_match_does_not_call_the_model(monkeypatch):
    # a question the curated KB answers must NOT burn a model call — deterministic path wins
    monkeypatch.setattr(projects_api, "_SIDEKICK", GroundedSidekick(model=_BoomModel()))
    monkeypatch.setattr(projects_api, "_SIDEKICK_TRIED", True)
    r = projects_api.sidekick_reply({}, "how are my credentials handled?")
    assert r["topic"] == "Credentials" and "reference" in r["text"].lower()


def test_out_of_kb_question_uses_grounded_model(monkeypatch):
    m = _FakeModel("Yes — you can run a Google-only setup.\nSOURCES: pick-apps")
    monkeypatch.setattr(projects_api, "_SIDEKICK", GroundedSidekick(model=m))
    monkeypatch.setattr(projects_api, "_SIDEKICK_TRIED", True)
    # a phrasing with no curated keyword hit → falls through to the grounded assistant
    r = projects_api.sidekick_reply({}, "hey, is a google-only footprint a thing here?")
    assert r.get("topic") == "Sidekick" and r.get("grounded") is True
    assert "google-only" in r["text"].lower()
    assert m.calls == 1


def test_out_of_kb_without_model_keeps_generic_fallback(monkeypatch):
    monkeypatch.setattr(projects_api, "_SIDEKICK", GroundedSidekick(model=None))
    monkeypatch.setattr(projects_api, "_SIDEKICK_TRIED", True)
    r = projects_api.sidekick_reply({}, "hey, is a google-only footprint a thing here?")
    assert "governed Mission" in r["text"]                       # unchanged safe fallback
