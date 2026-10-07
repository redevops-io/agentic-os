"""Privacy modes — the hard boundary is the business default, but local/BYO deployments may opt out (plan §31).

Three deployment modes, picked deliberately (never by fallback):
- STRICT_PRIVATE: no external model is ever used; everything runs in-boundary (works NO_INTERNET).
- PRIVATE_WITH_ENGINEERING_ASSIST (default for business): business data stays in-boundary; external frontier models
  serve only ENGINEERING/PUBLIC context (coding/infra).
- OPEN: a single provider key may handle EVERYTHING, including business data. Data privacy is NOT guaranteed — this
  is for someone running core+apps locally who accepts that their data leaves their infrastructure. It must be an
  explicit choice, and the consequence is made visible (routing decisions/receipts flag that privacy is not preserved).

Pure. The mode gates the GovernedModelRouter; the router still never routes to an unregistered/unapproved endpoint.
"""
from __future__ import annotations

import os
from enum import Enum


class PrivacyMode(str, Enum):
    STRICT_PRIVATE = "strict_private"
    PRIVATE_WITH_ENGINEERING_ASSIST = "private_with_engineering_assist"
    OPEN = "open"


DEFAULT_MODE = PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST


_NOTICES = {
    PrivacyMode.STRICT_PRIVATE:
        "STRICT_PRIVATE: no data leaves your trust boundary; external model providers are not used at all.",
    PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST:
        "PRIVATE_WITH_ENGINEERING_ASSIST (recommended): your business data stays inside your boundary; external "
        "frontier models are used only for coding/infrastructure work over non-business (engineering/public) context.",
    PrivacyMode.OPEN:
        "OPEN — DATA PRIVACY IS NOT GUARANTEED: business/customer data may be sent to external model providers "
        "(e.g. a single API key used for everything). Choose this only if you accept that your data leaves your "
        "infrastructure. For confined business data, use PRIVATE_WITH_ENGINEERING_ASSIST or STRICT_PRIVATE.",
}


def privacy_notice(mode: PrivacyMode) -> str:
    """A human-readable statement of what the mode does — shown so the operator understands the consequence."""
    return _NOTICES[mode]


def privacy_mode_from_env(default: PrivacyMode = DEFAULT_MODE) -> PrivacyMode:
    """Resolve the mode from REDEVOPS_PRIVACY_MODE; falls back to the safe business default. An unknown value is
    treated as the default (never silently more permissive)."""
    raw = (os.environ.get("REDEVOPS_PRIVACY_MODE", "") or "").strip().lower()
    try:
        return PrivacyMode(raw) if raw else default
    except ValueError:
        return default


__all__ = ["PrivacyMode", "DEFAULT_MODE", "privacy_notice", "privacy_mode_from_env"]
