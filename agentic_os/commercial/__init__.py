"""Canonical commercial vocabulary — one identity model and one outcome envelope across all capabilities.

P1C of the Sidekick Commercial Operations program (plan §22/§28): reconcile the per-domain vocabularies the
Phase-0 audit flagged. Typed ``CanonicalPerson``/``CanonicalOrganization`` facades over the single
``integration.CanonicalEntity``, and one ``CommercialOutcome`` that adapts onto the base ``overlays.Outcome``
reward loop. Additive and non-breaking — no existing type changes.
"""
from .identity import CanonicalOrganization, CanonicalPerson
from .outcome import CommercialOutcome, record_commercial_outcome

__all__ = ["CanonicalPerson", "CanonicalOrganization", "CommercialOutcome", "record_commercial_outcome"]
