"""Multimodal customer interaction (AGPL base) — one conversation, any channel, as a Mission.

Wires the ``runtime_contracts`` interaction contracts (InteractionEvent / TranscriptArtifact / …)
into the runtime: an append-only conversation store, an ``InteractionService`` over duck-typed
channel/speech providers, a capability ``Operator`` (GET /capabilities + POST /invoke), and the
conversation→Mission bridge. Offline stubs (``StubChannelAdapter`` / ``StubSpeechProvider``) let the
whole path run without any channel or speech SDK; a real NeMo Voice Agent adapter drops into the same
seam later.

Invariant preserved from the runtime: the sentence stays above the boundary. The bridge opens a
Mission from a sealed ``VerifiedIntent`` whose ``utterance_ref`` is the transcript's content hash — it
carries the reference, never the transcript text, so nothing below can re-read what was said.
"""
from __future__ import annotations

from .conversation import ConversationStore
from .service import HandoffPackage, InteractionService
from .stubs import StubChannelAdapter, StubSpeechProvider
from .operator import build_interaction_operator
from .bridge import open_mission_for_conversation, sealed_intent_from_transcript
from .harness import (
    CoverageReport,
    InteractionScenario,
    KeywordIntentResolver,
    ScenarioResult,
    SuiteReport,
    builtin_scenarios,
    coverage_report,
    load_scenarios,
    run_scenario,
    run_suite,
)
from .nemo_eval import export_nemo_scenarios, write_jsonl
from .providers_nemo import (
    NeMoSession,
    NeMoVoiceAgentProvider,
    interaction_events_from_context,
)

__all__ = [
    "ConversationStore",
    "InteractionService",
    "HandoffPackage",
    "StubChannelAdapter",
    "StubSpeechProvider",
    "build_interaction_operator",
    "open_mission_for_conversation",
    "sealed_intent_from_transcript",
    "InteractionScenario",
    "ScenarioResult",
    "SuiteReport",
    "KeywordIntentResolver",
    "run_scenario",
    "run_suite",
    "builtin_scenarios",
    "load_scenarios",
    "CoverageReport",
    "coverage_report",
    "export_nemo_scenarios",
    "write_jsonl",
    "NeMoVoiceAgentProvider",
    "NeMoSession",
    "interaction_events_from_context",
]
