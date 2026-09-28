"""Live wiring for the flagship quote-to-order composition (Revenue & Execution plan §14).

`flagship.plan_quote_from_request` is pure — it takes the candidate accounts and the catalog as data. This
module is the I/O counterpart that resolves those from the real systems of record and then calls it:

    plan_quote_from_request_live(text, customer_name=…, lines=…, twenty_client=…, erpnext_client=…)
      → candidate accounts from LIVE Twenty  (integrations.twenty.candidate_accounts)
      → catalog for the requested items from LIVE ERPNext  (integrations.erpnext.ErpnextClient.catalog)
      → the same governed QuotePlan the pure composition produces

Read-only: it only READS Twenty + ERPNext to construct the plan. The plan always carries
`approval_required` — actually creating the quotation / writing back to the CRM is the approval-gated Mission
step, never done here.
"""
from __future__ import annotations

from typing import List, Optional

from agentic_os.integrations.business.entity_resolution import EntityQuery
from agentic_os.integrations.erpnext import ErpnextClient
from agentic_os.integrations.twenty import TwentyClient, candidate_accounts
from agentic_os.revenue.flagship import QuotePlan, plan_quote_from_request
from agentic_os.revenue.quote import QuoteLine


def plan_quote_from_request_live(text: str, *, lines: List[QuoteLine], twenty_client: TwentyClient,
                                 erpnext_client: ErpnextClient, now_ms: int, customer_name: str = "",
                                 customer_domain: str = "", customer_email: str = "",
                                 source: str = "chatwoot", source_ref: str = "", accounts_limit: int = 200,
                                 min_intent_confidence: float = 0.6, allow_probable_entity: bool = False,
                                 **quote_kwargs) -> QuotePlan:
    """Resolve accounts (Twenty) + catalog (ERPNext) live, then compose the governed QuotePlan."""
    accounts = candidate_accounts(twenty_client, limit=accounts_limit)
    catalog = erpnext_client.catalog([ln.item_ref for ln in lines])
    query = EntityQuery(name=customer_name, email=customer_email, domain=customer_domain)
    return plan_quote_from_request(
        text, entity_query=query, candidate_accounts=accounts, lines=lines, catalog=catalog, now_ms=now_ms,
        source=source, source_ref=source_ref, min_intent_confidence=min_intent_confidence,
        allow_probable_entity=allow_probable_entity, **quote_kwargs)
