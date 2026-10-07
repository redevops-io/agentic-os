"""Metabase visualization capability — turn a request into a governed, verified Metabase card.

The agentic-apps stack could previously only DISPLAY pre-seeded Metabase dashboards; nothing created a chart in
response to a request. This package adds that, open-core and provider-shaped:

- ``contracts`` — a reviewable :class:`VizSpec` (explicit native SQL + display) and results.
- ``compile`` — VizSpec → the exact Metabase REST payloads.
- ``client`` — a ``MetabaseWriter`` (create card/dashboard/dashcards + read-back) with HTTP + in-memory impls.
- ``nl`` — natural language → VizSpec via VETTED metric templates (abstains rather than fabricate SQL).
- ``governed`` — propose → human-approve the exact chart → create → independently verify.

The enterprise overlay wires this onto the live Metabase + the Sidekick/console inbox; the pieces here run
offline against the in-memory writer (fake-until-credentialed).
"""
from .contracts import DISPLAY_ALIASES, DisplayType, VizCard, VizResult, VizSpec
from .compile import card_payload, dashboard_payload, dashcards_payload
from .client import HttpMetabaseWriter, InMemoryMetabaseWriter, MetabaseWriter
from .workspace import MetabaseWorkspaceProvider
from .nl import METRIC_LIBRARY, MetricTemplate, available_metrics, interpret
from .governed import (
    VIZ_RISK_TIER, InMemoryVizApprovals, VizProposal, add_card_to_dashboard, apply_if_approved, archive_card,
    archive_dashboard, create_visualization, propose, update_card,
)

__all__ = [
    "DisplayType", "DISPLAY_ALIASES", "VizSpec", "VizCard", "VizResult",
    "card_payload", "dashboard_payload", "dashcards_payload",
    "MetabaseWriter", "InMemoryMetabaseWriter", "HttpMetabaseWriter", "MetabaseWorkspaceProvider",
    "interpret", "available_metrics", "METRIC_LIBRARY", "MetricTemplate",
    "propose", "VizProposal", "InMemoryVizApprovals", "create_visualization", "apply_if_approved",
    "update_card", "add_card_to_dashboard", "archive_card", "archive_dashboard",
    "VIZ_RISK_TIER",
]
