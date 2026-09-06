"""NeMo Voice Agent → InteractionEvent wiring (the real VoiceSessionProvider).

The mapping is exercised on the exact context shape the deployed agent returns from the RTVI
``get_context_history`` action (verified live during the two-bot eval): a list of {role, content}
messages, delivered either as a real list or as its single-quoted python-repr string.
"""
from __future__ import annotations

from runtime_contracts import Channel, InteractionEvent, Modality

from agentic_os.interaction import (
    ConversationStore,
    NeMoVoiceAgentProvider,
    interaction_events_from_context,
)

# the repr-string form the agent actually sends on the wire (single quotes), trimmed from a real run
_CONTEXT_STR = (
    "[{'role': 'system', 'content': 'You are Lisa, a restaurant assistant.'}, "
    "{'role': 'user', 'content': 'Hello'}, "
    "{'role': 'assistant', 'content': \"Welcome to FastBites! I'm Lisa, what can I help you with?\"}, "
    "{'role': 'user', 'content': 'A cheeseburger combo please'}]"
)

_CONTEXT_LIST = [
    {"role": "system", "content": "scaffolding"},
    {"role": "user", "content": "I want to cancel my flight"},
    {"role": "assistant", "content": "I can help with that."},
]


def test_projects_user_and_agent_turns_skips_system():
    ev = interaction_events_from_context(_CONTEXT_STR, conversation_id="c1")
    # system dropped; 2 user + 1 assistant, in order
    assert [e.provenance for e in ev] == ["nemo-asr", "nemo-agent", "nemo-asr"]
    assert ev[0].text == "Hello" and ev[0].modality is Modality.TEXT
    assert "cheeseburger combo" in ev[2].text
    assert all(e.conversation_id == "c1" for e in ev)


def test_accepts_both_repr_string_and_list_forms():
    from_str = interaction_events_from_context(_CONTEXT_STR, conversation_id="c1")
    from_list = interaction_events_from_context(_CONTEXT_LIST, conversation_id="c2")
    assert len(from_str) == 3 and len(from_list) == 2
    assert from_list[0].provenance == "nemo-asr" and from_list[0].text.startswith("I want to cancel")


def test_events_are_content_addressed_and_deterministic():
    a = interaction_events_from_context(_CONTEXT_STR, conversation_id="c1")
    b = interaction_events_from_context(_CONTEXT_STR, conversation_id="c1")
    assert [e.content_hash for e in a] == [e.content_hash for e in b]
    assert all(isinstance(e, InteractionEvent) and e.content_hash.startswith("rcv1:") for e in a)


def test_malformed_context_yields_nothing_not_a_crash():
    assert interaction_events_from_context("not a context", conversation_id="c1") == []
    assert interaction_events_from_context([{"role": "system", "content": "x"}], conversation_id="c1") == []


def test_provider_records_a_session_transcript_onto_the_store():
    store = ConversationStore()
    prov = NeMoVoiceAgentProvider(channel=Channel.PHONE, store=store)
    sess = prov.start_session("call-1")
    recorded = prov.ingest_context(sess, _CONTEXT_STR, participant_ref="cust:acme")
    assert len(recorded) == 3
    # the customer utterances carry the participant; agent turns don't
    user_turns = [e for e in recorded if e.provenance == "nemo-asr"]
    assert all(e.participant_ref == "cust:acme" for e in user_turns)
    # events() reads them back from the conversation store
    assert [e.content_hash for e in prov.events(sess)] == [e.content_hash for e in recorded]
    prov.end_session(sess)


def test_provider_capabilities_advertise_the_nemo_pipeline():
    caps = NeMoVoiceAgentProvider(ws_url="ws://box:8765").capabilities()
    assert caps["provider"] == "nvidia-nemo-voice-agent"
    assert caps["transport"] == "pipecat-rtvi-websocket"
    assert caps["ws_url"] == "ws://box:8765"
    assert Modality.AUDIO.value in caps["modalities"] and caps["diarization"] is True
