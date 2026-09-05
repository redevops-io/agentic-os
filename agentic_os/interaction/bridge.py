"""Conversation → Mission bridge.

The interaction plan's spine: ``TranscriptArtifact → Discovery → VerifiedIntent → Mission``. This is
the last hop — turning a sealed intent (whose meaning Discovery has closed) into a Mission, with the
transcript referenced by hash on ``utterance_ref`` and NEVER inlined. The objective + fields come from
the Discovery layer (a closed objective vocabulary); this bridge does not read the transcript text, so
nothing below the boundary can re-read what the customer said.

Two entry points:
  * ``sealed_intent_from_transcript`` — build the sealed VerifiedIntent (utterance_ref = transcript
    content hash). Pure; no runtime needed.
  * ``open_mission_for_conversation`` — hand that sealed intent to ``MissionRuntime`` and open the
    Mission. The runtime refuses an unsealed intent and refuses an objective with no template BY NAME.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from runtime_contracts import Author, IntentField, TranscriptArtifact, VerifiedIntent


def sealed_intent_from_transcript(
    transcript: TranscriptArtifact, *, objective: str, fields: Mapping[str, str],
    produced_by: str = "interaction-bridge@0", author: Author = Author.USER,
) -> VerifiedIntent:
    """A sealed VerifiedIntent bound to the transcript by hash — the reference, never the text."""
    draft = VerifiedIntent(
        objective=objective,
        produced_by=produced_by,
        utterance_ref=transcript.content_hash,
        fields={k: IntentField(value=v, author=author) for k, v in fields.items()},
    )
    return draft.seal()


def open_mission_for_conversation(
    runtime: Any, transcript: TranscriptArtifact, *, objective: str, fields: Mapping[str, str],
    policy_refs: Optional[list] = None, produced_by: str = "interaction-bridge@0",
) -> Any:
    """Open a Mission from a conversation's sealed intent. ``runtime`` is a ``MissionRuntime``."""
    intent = sealed_intent_from_transcript(transcript, objective=objective, fields=fields,
                                           produced_by=produced_by)
    return runtime.create_mission_from_intent(intent, policy_refs=list(policy_refs or []))
