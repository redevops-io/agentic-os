"""Governed visualization creation — propose → approve → create → verify.

Creating a saved question/dashboard in a shared BI tool is an EXTERNAL WRITE other people will see, so it is
approval-gated by default (``RiskTier.CONSEQUENTIAL``), exactly like the stack's other outward actions. The flow:
``propose(spec)`` → a human approves the exact fingerprint (the title+SQL+display they reviewed) → ``create``
persists it via the injected :class:`MetabaseWriter` and INDEPENDENTLY reads it back, so success means the card
exists, not merely that a POST returned 2xx. Nothing here authors SQL; it carries a reviewed :class:`VizSpec`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..agent_gateway.contracts import RiskTier
from .client import MetabaseWriter
from .compile import card_payload
from .contracts import VizCard, VizResult, VizSpec

# A saved card is a reversible, bounded external write to a shared tool → approval by default (never auto).
VIZ_RISK_TIER = RiskTier.CONSEQUENTIAL


@dataclass(frozen=True)
class VizProposal:
    """A proposed visualization awaiting approval. ``fingerprint`` binds an approval to this exact chart."""
    spec: VizSpec
    fingerprint: str
    risk_tier: RiskTier = VIZ_RISK_TIER
    rationale: str = ""


def propose(spec: VizSpec, *, rationale: str = "") -> VizProposal:
    return VizProposal(spec=spec, fingerprint=spec.fingerprint(),
                       rationale=rationale or f"Create Metabase card '{spec.title}' ({spec.display.value})")


@dataclass
class InMemoryVizApprovals:
    """Content-addressed approval gate (same discipline as the deal-closing gate): a decision binds to one
    exact chart's fingerprint, so approving one never authorizes a different query."""
    _status: Dict[str, str] = field(default_factory=dict)

    def park(self, fingerprint: str) -> None:
        self._status.setdefault(fingerprint, "pending")

    def approve(self, fingerprint: str, *, by: str = "operator") -> None:
        self._status[fingerprint] = "approved"

    def reject(self, fingerprint: str, *, by: str = "operator") -> None:
        self._status[fingerprint] = "rejected"

    def is_approved(self, fingerprint: str) -> bool:
        return self._status.get(fingerprint) == "approved"

    def pending(self) -> List[str]:
        return [fp for fp, s in self._status.items() if s == "pending"]


def create_visualization(proposal: VizProposal, writer: MetabaseWriter, *, approved: bool,
                         dashboard_name: Optional[str] = None, verify: bool = True,
                         base_url: str = "") -> VizResult:
    """Create the card iff ``approved``. Independently reads the card back (``get_card``) so a silent write
    failure surfaces as created-but-unverified rather than a false success. Optionally pins it to a new
    dashboard. Refuses (no side effect) when not approved."""
    if not approved:
        return VizResult(created=False, verified=False,
                         detail="refused: creating a Metabase visualization requires human approval")
    rec = writer.create_card(card_payload(proposal.spec))
    cid = rec.get("id")
    if not cid:
        return VizResult(created=False, verified=False, detail="create_card returned no id")

    verified = True
    if verify:
        back = writer.get_card(cid)
        verified = back is not None and back.get("name") == proposal.spec.title

    url = f"{base_url.rstrip('/')}/question/{cid}" if base_url else ""
    card = VizCard(card_id=cid, name=rec.get("name", ""), display=rec.get("display", ""), url=url)

    if dashboard_name:
        dash = writer.create_dashboard(dashboard_name)
        if dash.get("id"):
            writer.add_dashcards(dash["id"], [cid])

    return VizResult(created=True, verified=verified, card=card,
                     detail="created and verified by read-back" if verified
                     else "created but read-back did not confirm the card")


def apply_if_approved(proposal: VizProposal, writer: MetabaseWriter, approvals: InMemoryVizApprovals,
                      **kw) -> VizResult:
    """Convenience: create only when the gate holds an approval for this proposal's fingerprint."""
    return create_visualization(proposal, writer, approved=approvals.is_approved(proposal.fingerprint), **kw)


# ── governed MUTATIONS (update / archive / add-to-existing-dashboard) ────────────────────────────────
# Every workspace mutation is an external write to a shared tool → approval-gated exactly like create. These
# refuse (no side effect) unless ``approved`` and return a small result dict for the caller/audit trail.
def update_card(provider, card_id: int, changes: Dict, *, approved: bool) -> dict:
    if not approved:
        return {"ok": False, "detail": "refused: updating a Metabase card requires approval"}
    rec = provider.update_card(card_id, changes)
    return {"ok": bool(rec), "card": rec, "detail": "updated" if rec else "card not found"}


def add_card_to_dashboard(provider, dashboard_id: int, card_id: int, *, approved: bool,
                          size_x: int = 12, size_y: int = 4) -> dict:
    if not approved:
        return {"ok": False, "detail": "refused: modifying a Metabase dashboard requires approval"}
    ok = provider.add_card_to_dashboard(dashboard_id, card_id, size_x=size_x, size_y=size_y)
    return {"ok": ok, "detail": "card added to dashboard" if ok else "dashboard not found"}


def archive_card(provider, card_id: int, *, approved: bool) -> dict:
    if not approved:
        return {"ok": False, "detail": "refused: archiving a Metabase card requires approval"}
    return {"ok": provider.archive_card(card_id), "detail": "archived"}


def archive_dashboard(provider, dashboard_id: int, *, approved: bool) -> dict:
    if not approved:
        return {"ok": False, "detail": "refused: archiving a Metabase dashboard requires approval"}
    return {"ok": provider.archive_dashboard(dashboard_id), "detail": "archived"}


__all__ = ["VIZ_RISK_TIER", "VizProposal", "propose", "InMemoryVizApprovals",
           "create_visualization", "apply_if_approved",
           "update_card", "add_card_to_dashboard", "archive_card", "archive_dashboard"]
