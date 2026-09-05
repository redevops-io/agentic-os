"""agentic customer-interaction as a Mission Runtime operator.

Mounts the Operator SDK surface (GET /capabilities + POST /invoke) so the Mission Runtime can drive
interaction as a capability operator. Handlers take JSON-friendly inputs (what a channel adapter would
hand over) and call the InteractionService. Outbound sends are ``side_effecting`` (deduped
exactly-once on the Idempotency-Key). ``interaction.reply`` on an EXTERNAL channel is where an
Enterprise adapter would set ``approval_required=True``; the base stub does not gate.
"""
from __future__ import annotations

from typing import Any, Dict

from agentic_os.mission.operator_sdk import Operator, capability
from runtime_contracts import Channel, InteractionEvent, MediaArtifact, Modality

from .service import InteractionService


def build_interaction_operator(service: InteractionService) -> Operator:
    def ingest(inp: Dict[str, Any]) -> Dict[str, Any]:
        event = InteractionEvent(
            interaction_id=inp["interaction_id"], conversation_id=inp["conversation_id"],
            channel=Channel(inp["channel"]), modality=Modality(inp.get("modality", "text")),
            participant_ref=inp.get("participant_ref", ""), artifact_ref=inp.get("artifact_ref", ""),
            text=inp.get("text", ""), timestamp=inp.get("timestamp", ""),
            provenance=inp.get("provenance", "channel-adapter"),
        )
        e = service.ingest(event)
        return {"interaction_id": e.interaction_id, "content_hash": e.content_hash,
                "conversation_id": e.conversation_id}

    def transcribe(inp: Dict[str, Any]) -> Dict[str, Any]:
        media = MediaArtifact(
            artifact_id=inp["artifact_id"], modality=Modality.AUDIO,
            media_hash=inp["media_hash"], media_type=inp.get("media_type", "audio/wav"),
            duration_ms=int(inp.get("duration_ms", 0)),
        )
        tr = service.transcribe(media, conversation_id=inp["conversation_id"],
                                participant_ref=inp.get("participant_ref", ""),
                                language=inp.get("language", ""))
        return {"transcript_id": tr.artifact_id, "transcript_hash": tr.content_hash,
                "source_media_hash": tr.source_media_hash, "text": tr.text}

    def reply(inp: Dict[str, Any]) -> Dict[str, Any]:
        r = service.reply(inp["conversation_id"], Channel(inp["channel"]), text=inp.get("text"))
        return {"status": r.status, "provider_ref": r.provider_ref, "channel": r.channel.value}

    def handoff(inp: Dict[str, Any]) -> Dict[str, Any]:
        pkg = service.handoff(inp["conversation_id"], to=inp["to"], note=inp.get("note", ""))
        return pkg.summary()

    return Operator("interaction", [
        capability(
            "interaction.ingest", ingest,
            provides=["interaction_recorded"],
            outputs={"interaction_recorded": "inbound interaction recorded on the conversation"},
            side_effecting=True, permissions=["interaction:write"],
            concurrency_mode="exclusive", concurrency_key="interaction:conversation:{conversation_id}",
        ),
        capability(
            "interaction.transcribe", transcribe,
            provides=["transcript_produced"],
            outputs={"transcript_produced": "audio transcribed to a transcript bound to the source"},
            side_effecting=False, permissions=["interaction:read"],
            estimated_value="high", latency_ms=800,
            data_classifications=["customer_audio"],
        ),
        capability(
            "interaction.reply", reply,
            provides=["reply_sent"],
            outputs={"reply_sent": "outbound response delivered on the channel"},
            side_effecting=True, permissions=["interaction:write"],
            estimated_value="high", latency_ms=700,
            concurrency_mode="exclusive", concurrency_key="interaction:conversation:{conversation_id}",
        ),
        capability(
            "interaction.handoff", handoff,
            provides=["handoff_packaged"],
            outputs={"handoff_packaged": "conversation bundled for a human takeover"},
            side_effecting=True, permissions=["interaction:write"],
            estimated_value="medium", latency_ms=400,
        ),
    ])
