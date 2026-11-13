"""Downstream receipts. An action is ``completed`` only when the downstream system returned a receipt; anything else is ``outcome_unknown``.

The gateway reports a handler's success as ``GatewayStatus.OK``. For a side-effecting capability that is not enough: the handler must have
returned a provider object id (``provider_object_id`` in its output) or the gateway must carry an evidence reference. Without one the
effect cannot be shown, and sending the action again could repeat it, so the result is ``outcome_unknown`` with reason ``no_downstream_receipt``.
"""
from __future__ import annotations

from typing import Any, Tuple


def receipt_ref(output: Any, evidence_refs: Tuple[str, ...] = ()) -> str:
    if isinstance(output, dict) and output.get("provider_object_id"):
        return str(output["provider_object_id"])[:128]
    if evidence_refs:
        return str(evidence_refs[0])[:128]
    return ""
