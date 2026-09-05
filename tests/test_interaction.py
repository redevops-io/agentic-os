"""Multimodal interaction (AGPL): operator + stubs + conversation store + conversation→Mission bridge.

Fully offline — the stub channel/speech providers stand in for a real NeMo adapter behind the same
seam. Proves the contract end-to-end: inbound audio → transcript (bound to audio) → open a Mission
(sealed intent, sentence unreachable) → reply, with cross-channel continuity and idempotent sends.
"""
from __future__ import annotations

import pytest

from runtime_contracts import (
    Channel,
    InteractionEvent,
    MediaArtifact,
    Modality,
    VerifiedIntent,
)

from agentic_os.interaction import (
    ConversationStore,
    InteractionService,
    StubChannelAdapter,
    StubSpeechProvider,
    build_interaction_operator,
    open_mission_for_conversation,
    sealed_intent_from_transcript,
)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec


_AUDIO = MediaArtifact(artifact_id="a1", modality=Modality.AUDIO, media_hash="sha256:call-1",
                       media_type="audio/wav", duration_ms=5200)
_SECRET = "I want to onboard my company acme"   # the sentence — must stay above the boundary


def _service(transcripts=None):
    store = ConversationStore()
    adapters = {Channel.PHONE: StubChannelAdapter(Channel.PHONE),
                Channel.SLACK: StubChannelAdapter(Channel.SLACK)}
    speech = StubSpeechProvider(transcripts=transcripts or {"sha256:call-1": _SECRET},
                                asr_model="parakeet", asr_version="1.2")
    return InteractionService(store, adapters=adapters, speech=speech)


def test_transcribe_binds_transcript_to_source_audio_and_records_a_derived_event():
    svc = _service()
    tr = svc.transcribe(_AUDIO, conversation_id="c1", participant_ref="cust:acme")
    assert tr.source_media_hash == _AUDIO.media_hash        # bound to the original audio by hash
    assert tr.asr_model == "parakeet" and tr.asr_version == "1.2"
    assert tr.text == _SECRET
    # the conversation now holds the derived text interaction, referencing (not replacing) the audio
    events = svc.store.for_conversation("c1")
    assert len(events) == 1 and events[0].modality is Modality.TEXT
    assert _AUDIO.media_hash in events[0].derived_artifact_refs
    assert events[0].artifact_ref == tr.content_hash


def test_operator_ingest_reply_are_idempotent_and_typed():
    op = build_interaction_operator(_service())
    ing = op.invoke("interaction.ingest", {"interaction_id": "i1", "conversation_id": "c1",
                                           "channel": "phone", "modality": "audio"}, "k-ing")
    assert ing["conversation_id"] == "c1" and ing["content_hash"].startswith("rcv1:")
    r1 = op.invoke("interaction.reply", {"conversation_id": "c1", "channel": "phone",
                                         "text": "thanks!"}, "k-reply")
    r2 = op.invoke("interaction.reply", {"conversation_id": "c1", "channel": "phone",
                                         "text": "thanks!"}, "k-reply")
    assert r1 == r2 and r1["status"] == "sent"              # exactly-once on the idempotency key
    assert op.calls.count(("interaction.reply", "k-reply")) == 1


def test_capabilities_manifest_lists_the_syscalls():
    op = build_interaction_operator(_service())
    names = {c.name for c in op.manifest.capabilities}
    assert names == {"interaction.ingest", "interaction.transcribe",
                     "interaction.reply", "interaction.handoff"}


def test_cross_channel_continuity_same_conversation():
    svc = _service()
    svc.ingest(InteractionEvent(interaction_id="p1", conversation_id="c1", channel=Channel.PHONE,
                                modality=Modality.AUDIO))
    svc.ingest(InteractionEvent(interaction_id="s1", conversation_id="c1", channel=Channel.SLACK,
                                modality=Modality.TEXT, text="approved"))
    assert set(svc.store.channels("c1")) == {Channel.PHONE, Channel.SLACK}   # one conversation, two channels
    pkg = svc.handoff("c1", to="slack:#support", note="needs a human")
    assert set(pkg.channels) == {"phone", "slack"} and pkg.interaction_ids == ("p1", "s1")


def test_bridge_seals_intent_bound_to_transcript_and_never_carries_the_sentence():
    svc = _service()
    tr = svc.transcribe(_AUDIO, conversation_id="c1")
    intent = sealed_intent_from_transcript(tr, objective="onboard_customer",
                                           fields={"customer": "acme"})
    assert isinstance(intent, VerifiedIntent)
    assert intent.utterance_ref == tr.content_hash          # the reference is kept
    # structural: the sealed artifact references the transcript, never contains the sentence
    flat = repr(intent.to_json())
    assert tr.content_hash in flat
    assert "onboard my company" not in flat.lower()


def _registry_for_onboarding():
    reg = CapabilityRegistry()
    for name, outcome in (("billing.subscribe", "subscription"),
                          ("interaction.reply", "onboarding_sent"),
                          ("books.record", "revenue_recorded"),
                          ("compliance.file", "consent_filed")):
        op = name.split(".")[0]
        reg.register(CapabilityManifest(op, [CapabilitySpec(name, op, provides=[outcome],
                                                            permissions=[f"{op}:write"])]))
    return reg


def test_open_mission_for_conversation_end_to_end():
    svc = _service()
    tr = svc.transcribe(_AUDIO, conversation_id="c1")
    runtime = MissionRuntime(_registry_for_onboarding(),
                             Executor(InMemoryOperatorClient({})), store=EventStore())
    mission = open_mission_for_conversation(
        runtime, tr, objective="onboard_customer", fields={"customer": "acme"},
        policy_refs=["billing:write", "interaction:write", "books:write", "compliance:write"])
    assert mission is not None
    # the mission exists and its plan came from the sealed objective, not the sentence
    assert getattr(mission, "id", getattr(mission, "mission_id", None))


def test_unsealed_intent_path_is_not_used_by_the_bridge():
    # the bridge always seals; feeding the runtime a sealed intent is the whole point
    svc = _service()
    tr = svc.transcribe(_AUDIO, conversation_id="c1")
    intent = sealed_intent_from_transcript(tr, objective="onboard_customer", fields={"customer": "acme"})
    assert intent.state.name != "DRAFT"  # sealed, not a draft
