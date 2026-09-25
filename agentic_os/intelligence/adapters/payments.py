"""Payments BYO adapter: Stripe Radar — network-scale payment fraud intelligence (moat §3.2/3.5 P0). BYO secret key.

Radar's risk score/level comes from Stripe's network-wide payment data (impossible to reproduce locally). The
subject is a Stripe charge/payment-intent id; the evidence is its Radar outcome.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import Capability, EvidenceRef, ProviderFamily, content_hash

from ._base import HttpEvidenceProvider


class StripeRadarProvider(HttpEvidenceProvider):
    provider_id = "stripe_radar"
    family = ProviderFamily.EXTERNAL_DATA
    _caps = (Capability.PAYMENT_FRAUD_SCORE,)
    _price = 0.0        # reading an existing charge's Radar outcome is not separately metered
    _license = "Stripe (account data; Radar network intelligence)"
    _BASE = "https://api.stripe.com/v1"

    def _request(self, request):
        charge_id = request.subject_refs[0]
        # form-encoded API, but GET reads need no body; Radar fields are on the charge's `outcome`.
        return "GET", f"{self._BASE}/charges/{quote(charge_id)}", {"Authorization": f"Bearer {self._cred}"}, None

    def _extract(self, body, request):
        outcome = body.get("outcome") or {}
        if not outcome:
            return [], [], 0.0
        obs = [{
            "charge": body.get("id"),
            "risk_level": outcome.get("risk_level"),          # normal | elevated | highest
            "risk_score": outcome.get("risk_score"),
            "network_status": outcome.get("network_status"),
            "seller_message": outcome.get("seller_message"),
            "rule": (outcome.get("rule") or {}).get("id") if isinstance(outcome.get("rule"), dict) else outcome.get("rule"),
        }]
        # confidence: Stripe's own network-derived risk score (0..100) normalized.
        score = outcome.get("risk_score")
        conf = min(0.99, (score / 100.0)) if isinstance(score, (int, float)) else 0.8
        return obs, [EvidenceRef(ref=f"stripe:charge:{body.get('id','')}", source="stripe",
                                 content_hash=content_hash(body), ref_type="record")], conf
