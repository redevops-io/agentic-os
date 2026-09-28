"""Twenty CRM sensor — the primary commercial system of record (Revenue plan §2).

A thin, READ-ONLY client over a real Twenty core plus mappers to the canonical business objects, so the
revenue detectors and the flagship run on real pipeline data: `scan_stalled()` feeds the leakage detector
(§6 STALLED_OPPORTUNITY) from live opportunities, and `candidate_accounts()` gives the flagship the account
pool for entity resolution. Read-only by design — nothing here mutates the CRM (writes belong to the
approval-gated execution adapter, never to a sensor).

Self-skips cleanly (unreachable / unauthorized → empty), unit-testable with a stub, and live against a real
Twenty with `TWENTY_BASE_URL` + `TWENTY_API_KEY` (`twenty_from_env`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from agentic_os.integrations.business.contracts import Account, Opportunity, Provenance
from agentic_os.revenue.leakage import RevenueLeakage, stalled_opportunity

# Twenty stages that mean the deal is closed (not "active"); mapped to the canonical terminal forms so the
# leakage detector's own terminal-stage guard recognises them.
_TWENTY_TERMINAL = {"won": "closed_won", "customer": "closed_won", "lost": "closed_lost"}


def _iso_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def _amount_cents(amount: object) -> int:
    """Twenty money is {amountMicros, currencyCode}; micros→cents is /10_000 (1e6 micros = 1 unit = 100c)."""
    micros = amount.get("amountMicros") if isinstance(amount, dict) else None
    return int(micros // 10_000) if micros else 0


def _domain(company: dict) -> str:
    dn = company.get("domainName")
    return (dn.get("primaryLinkUrl") or "").strip() if isinstance(dn, dict) else (str(dn or "").strip())


@dataclass
class TwentyClient:
    base_url: str
    api_key: str
    timeout: float = 12.0

    def _get(self, path: str, params: dict) -> list:
        import httpx
        try:
            r = httpx.get(f"{self.base_url}{path}", params=params,
                          headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
            if r.status_code >= 400:
                return []
            data = r.json().get("data", {})
            key = path.rstrip("/").split("/")[-1]     # /rest/opportunities → "opportunities"
            return (data.get(key) if isinstance(data, dict) else data) or []
        except Exception:  # noqa: BLE001
            return []

    def opportunities(self, *, limit: int = 60) -> list:
        return self._get("/rest/opportunities", {"limit": limit})

    def companies(self, *, limit: int = 60) -> list:
        return self._get("/rest/companies", {"limit": limit})


def to_account(company: dict) -> Account:
    return Account(prov=Provenance(provider="twenty", provider_ref=str(company.get("id", ""))),
                   name=str(company.get("name", "") or ""), domain=_domain(company))


def to_opportunity(opp: dict) -> Tuple[Opportunity, int]:
    """Map a Twenty opportunity → (canonical Opportunity, last-activity ms). Terminal Twenty stages are
    normalized to the canonical closed_* forms; otherwise the stage is passed through as the active label."""
    raw = str(opp.get("stage", "") or "").lower()
    stage = _TWENTY_TERMINAL.get(raw, raw)
    amt = opp.get("amount")
    o = Opportunity(prov=Provenance(provider="twenty", provider_ref=str(opp.get("id", ""))),
                    name=str(opp.get("name", "") or ""), stage=stage, amount_cents=_amount_cents(amt),
                    currency=(amt.get("currencyCode", "") if isinstance(amt, dict) else ""))
    return o, _iso_to_ms(str(opp.get("updatedAt", "") or ""))


def candidate_accounts(client: TwentyClient, *, limit: int = 200) -> List[Account]:
    return [to_account(c) for c in client.companies(limit=limit)]


def scan_stalled(client: TwentyClient, *, now_ms: int, stale_days: int = 14,
                 limit: int = 200) -> List[RevenueLeakage]:
    """Read live opportunities and run the STALLED_OPPORTUNITY detector. `updatedAt` is used as the
    last-activity proxy (Twenty has no dedicated last-activity field), and future activity isn't modeled."""
    out: List[RevenueLeakage] = []
    for opp in client.opportunities(limit=limit):
        o, last_ms = to_opportunity(opp)
        if last_ms == 0:
            continue
        leak = stalled_opportunity(o, last_activity_at_ms=last_ms, has_future_activity=False,
                                   now_ms=now_ms, stale_days=stale_days)
        if leak is not None:
            out.append(leak)
    return out


def twenty_from_env() -> Optional[TwentyClient]:
    url = os.environ.get("TWENTY_BASE_URL", "").rstrip("/")
    key = os.environ.get("TWENTY_API_KEY", "")
    return TwentyClient(base_url=url, api_key=key) if (url and key) else None
