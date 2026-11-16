"""Twenty CRM — the approval-gated execution adapter (Mission Runtime operator), Stage E 2026-11-16.

`integrations.twenty`'s and `integrations.chatwoot`'s docstrings have, since their first commits, pointed
at "the approval-gated execution adapter" as where writes to Twenty happen — a component referenced four
times across `integrations/{twenty,chatwoot,lago,erpnext}.py` that never actually existed. This module is
that adapter for Twenty, scoped to the one write Stage E's inbound pipeline needs: a validated Contact
(Person) upsert, proposed by `revenue.contact_intake` from a real inbound signal, and NEVER executed
without a genuine runtime-delivered approval (`approval_gate.gated`, the same #399 pattern every other
side-effecting capability in this codebase uses).

`crm.contact.upsert` is `side_effecting=True, approval_required=True`: it writes to a real external CRM,
so it parks on approval exactly like `agentic-crm`'s `crm.qualify`. Shadow mode
(`TWENTY_WRITE_SHADOW_MODE`, default on — see `integrations.twenty_write`) additionally keeps even an
approved call from touching the real Twenty API until that flag is explicitly turned off.
"""
from __future__ import annotations

from typing import Optional

from agentic_os.approval_gate import gated
from agentic_os.integrations.twenty_write import TwentyWriteClient, twenty_write_from_env
from agentic_os.mission.operator_sdk import Operator, capability


def core_upsert_contact(inp: dict, *, client: Optional[TwentyWriteClient] = None) -> dict:
    """The real core logic behind `crm.contact.upsert`. Validates `name`/`email` are present and
    well-formed (the proposal layer already validated them once; this re-validates at the trust
    boundary, since `_approval` is the only thing distinguishing this call from an arbitrary invoke),
    then upserts via the write client (shadow-mode-aware)."""
    name = str(inp.get("name", "") or "").strip()
    email = str(inp.get("email", "") or "").strip()
    if not name or not email or "@" not in email:
        return {"status": "error", "action": "contact_upsert", "error": "name/email missing or malformed"}
    c = client or twenty_write_from_env()
    if c is None:
        return {"status": "error", "action": "contact_upsert", "error": "twenty_unconfigured"}
    result = c.upsert_person(name=name, email=email, company_id=inp.get("company_id"))
    if result.get("error"):
        return {"status": "error", "action": "contact_upsert", "error": result["error"]}
    return {"status": "done", "action": "contact_upsert", "name": name, "email": email, **result}


def build_twenty_operator(*, client: Optional[TwentyWriteClient] = None) -> Operator:
    return Operator("twenty-crm", [
        capability(
            "crm.contact.upsert",
            gated("crm.contact.upsert", lambda inp: core_upsert_contact(inp, client=client),
                 contact_upserted=False),
            provides=["contact_upserted"],
            outputs={"contact_upserted": "a Twenty Person record created (or matched by email) for a real inbound contact"},
            side_effecting=True, approval_required=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="TWENTY:{email}",
        ),
    ])
