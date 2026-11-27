"""Synthetic Buyer Research (SSR) — OPT-IN, read-only, synthetic-only.

Semantic Similarity Rating (Maier et al., arXiv:2510.08338, CC BY 4.0) maps free-text answers from synthetic
respondents onto a 5-point Likert pmf by comparing the answer's embedding with embedded anchor statements.

What this package is:  hypothesis generation and early concept screening for website messaging and search-intent
fit. What it is NOT: customer research, a conversion predictor, a ranking signal, or first-party evidence.
Every object it produces is ``synthetic=True`` and must never be written to an OutcomeStore or the first-party
ledger (see ``agentic_os.outcome_store.refuse_simulated``).

The package is import-light on purpose: nothing heavy is imported at load and the feature is OFF unless
``AGENTIC_OS_SYNTHETIC_RESEARCH`` is set to a truthy value (see :func:`enabled`).
"""
from __future__ import annotations

import os

__all__ = ["enabled", "FLAG_ENV"]

FLAG_ENV = "AGENTIC_OS_SYNTHETIC_RESEARCH"
_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """The feature flag. Default OFF: with it off no study may be run and no existing behaviour changes."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY
