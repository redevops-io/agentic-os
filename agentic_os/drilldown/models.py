"""Constants and the refusal type of the drill-down contract (Timeline + Evidence Inspect).

The contract's one rule: a value on screen is either backed by a persisted record that it CITES (event seq + sha256), or
it is an explicit ``unavailable`` object with a reason from the closed set below. There is no third state in which the
server fills a gap with something plausible.
"""
from __future__ import annotations

TIMELINE_SCHEMA_ID = "aos.drilldown.timeline/v1"
EVIDENCE_LIST_SCHEMA_ID = "aos.drilldown.evidence-list/v1"
EVIDENCE_INSPECT_SCHEMA_ID = "aos.drilldown.evidence-inspect/v1"

#: why a value is absent. Closed: a UI maps each to fixed wording, so it can never show a reason the server did not give.
UNAVAILABLE_REASONS = ("not_recorded", "not_authorized", "redacted", "expired", "source_offline", "too_large", "integrity_failed")
CATEGORIES = ("lifecycle", "plan", "node", "approval", "policy", "verification", "world", "evidence", "replay", "other")
OUTCOMES = ("ok", "failed", "denied", "pending", "info")
EVIDENCE_KINDS = ("verification", "claim", "evidence_record", "node_result", "mission_evidence_ref")
INTEGRITY_STATUSES = ("recorded_hash_verified", "recorded_hash_mismatch", "recorded_hash_unchecked", "no_recorded_hash")
DURABILITY = ("durable", "volatile", "unknown")
#: what the store's own checks can say about the log (``mission.integrity``) plus ``not_reported`` for a store that runs none
ANCHOR_STATUSES = ("unanchored", "ok", "lagging", "truncated", "mismatch", "invalid", "not_reported")
UI_CONFIG_SCHEMA_ID = "aos.ui-config/v1"
DATA_SOURCES = ("sample", "runtime")

MAX_LIMIT = 500
MAX_PAYLOAD_BYTES = 4096


class DrilldownRefused(Exception):
    """The request produced no content. ``code`` is machine readable; the message is fixed and never echoes caller input."""

    STATUS = {"unauthenticated": 401, "not_found": 404, "bad_request": 400, "audit_unavailable": 503}
    MESSAGES = {"unauthenticated": "authentication required", "not_found": "not found",
                "bad_request": "invalid request parameters", "audit_unavailable": "the audit log is unavailable; nothing was returned"}

    def __init__(self, code: str):
        self.code = code
        self.status = self.STATUS[code]
        super().__init__(self.MESSAGES[code])


def unavailable(reason: str, detail: str) -> dict:
    assert reason in UNAVAILABLE_REASONS
    return {"available": False, "reason": reason, "detail": detail}
