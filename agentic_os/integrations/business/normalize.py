"""Connector payloads → canonical business objects (plan §4: "provider payloads remain evidence").

A connector's ``ProviderResult.data`` / ``Observation.data`` is raw provider JSON. Mission logic should
reason about canonical objects, so this maps a ``(provider, object_type, data)`` triple to a typed
:mod:`.contracts` object, keeping the raw payload as evidence via ``Provenance.evidence_refs``. Object
type is inferred per provider when not given. Unknown shapes return ``None`` (never a guess).

Normalizers are registered, so adapters/apps can add their own without editing this module.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import contracts as C

Normalizer = Callable[[Mapping[str, Any], C.Provenance], C.BusinessObject]

_NORMALIZERS: Dict[Tuple[str, str], Normalizer] = {}


def register_normalizer(provider: str, object_type: str, fn: Normalizer) -> None:
    _NORMALIZERS[(provider, object_type)] = fn


def _infer_type(provider: str, data: Mapping[str, Any]) -> Optional[str]:
    if provider in ("stripe", "polar"):
        return str(data.get("object")) or None            # "charge" | "refund" | …
    if provider == "hubspot":
        props = data.get("properties")
        if isinstance(props, Mapping) and ("email" in props or "firstname" in props):
            return "contact"
        return None
    if provider in ("gmail", "outlook"):
        return "message" if ("threadId" in data or "thread_id" in data) else None
    if provider in ("slack", "whatsapp", "whatsapp_business", "whatsapp_waha"):
        return "message" if ("ts" in data or "message" in data or "text" in data) else None
    return None


def _provider_ref(provider: str, data: Mapping[str, Any]) -> str:
    for k in ("id", "ts", "message_id", "provider_object_id"):
        if data.get(k):
            return str(data[k])
    return ""


def normalize(provider: str, data: Mapping[str, Any], *, object_type: Optional[str] = None,
              provider_ref: str = "", evidence_refs: Tuple[str, ...] = (),
              known_at: int = 0) -> Optional[C.BusinessObject]:
    """Return a canonical object for a provider payload, or ``None`` when no normalizer matches."""
    ot = object_type or _infer_type(provider, data)
    if ot is None:
        return None
    fn = _NORMALIZERS.get((provider, ot))
    if fn is None:
        return None
    prov = C.Provenance(provider=provider, provider_ref=provider_ref or _provider_ref(provider, data),
                        evidence_refs=evidence_refs, known_at=known_at)
    return fn(data, prov)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── built-in normalizers (from the connector fixtures) ───────────────────────────────
def _stripe_charge(d: Mapping[str, Any], prov: C.Provenance) -> C.Charge:
    prov = C.Provenance(prov.provider, prov.provider_ref, prov.evidence_refs, prov.observed_at,
                        _int(d.get("created")) * 1000 or prov.known_at)
    return C.Charge(prov=prov, amount_cents=_int(d.get("amount")), currency=str(d.get("currency", "")),
                    status=str(d.get("status", "")), payment_intent_ref=str(d.get("payment_intent", "")),
                    refunded=bool(d.get("refunded", False)),
                    amount_refunded_cents=_int(d.get("amount_refunded")))


def _stripe_refund(d: Mapping[str, Any], prov: C.Provenance) -> C.Refund:
    prov = C.Provenance(prov.provider, prov.provider_ref, prov.evidence_refs, prov.observed_at,
                        _int(d.get("created")) * 1000 or prov.known_at)
    return C.Refund(prov=prov, charge_ref=str(d.get("charge", "")), amount_cents=_int(d.get("amount")),
                    currency=str(d.get("currency", "")), status=str(d.get("status", "")),
                    reason=str(d.get("reason") or ""))


def _hubspot_contact(d: Mapping[str, Any], prov: C.Provenance) -> C.Contact:
    props = d.get("properties", {}) or {}
    return C.Contact(prov=prov, email=str(props.get("email", "")),
                     first_name=str(props.get("firstname", "")), last_name=str(props.get("lastname", "")))


def _gmail_message(d: Mapping[str, Any], prov: C.Provenance) -> C.Message:
    labels = d.get("labelIds", []) or []
    direction = "outbound" if "SENT" in labels else ("inbound" if "INBOX" in labels else "unknown")
    return C.Message(prov=prov, channel="email", thread_ref=str(d.get("threadId", "")),
                     direction=direction, snippet=str(d.get("snippet", "")))


def _slack_message(d: Mapping[str, Any], prov: C.Provenance) -> C.Message:
    return C.Message(prov=prov, channel="slack", thread_ref=str(d.get("thread_ts", d.get("channel", ""))),
                     direction="outbound", snippet=str(d.get("text", d.get("message", ""))))


for _p, _t, _fn in (
    ("stripe", "charge", _stripe_charge), ("stripe", "refund", _stripe_refund),
    ("polar", "refund", _stripe_refund),
    ("hubspot", "contact", _hubspot_contact),
    ("gmail", "message", _gmail_message),
    ("slack", "message", _slack_message),
):
    register_normalizer(_p, _t, _fn)
