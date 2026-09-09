"""Context Sources — confirm-first intent + a real local-folder scan, plus the
capability-vs-context grant separation and content-addressed identity/fingerprint."""
from __future__ import annotations

import json

import pytest

from agentic_os.sources import (
    AccessMode,
    ConfirmedSourceIntent,
    ContextSource,
    IndexingPolicy,
    LocalFilesConnector,
    ProposedSource,
    SourceConnectionProposal,
    SourceConnectorRegistry,
    SourceGrant,
    SourceHealthState,
    SourceKind,
)


def test_confirm_refuses_while_questions_open():
    p = SourceConnectionProposal(
        project_id="customer-ops",
        sources=[ProposedSource(kind=SourceKind.DATABASE, location="localhost/customer_ops")],
        questions=["Which schemas may be used?"],
    )
    with pytest.raises(ValueError, match="questions are open"):
        p.confirm(confirmed_by="u", confirmed_at="t")


def test_confirm_seals_a_content_addressed_intent():
    p = SourceConnectionProposal(
        project_id="customer-ops",
        sources=[ProposedSource(kind=SourceKind.FILES, location="~/docs", allowed_content_types=["pdf"])],
        assumptions=["read-only inferred"],
    )
    a = p.confirm(confirmed_by="alex", confirmed_at="2026-09-09T00:00:00Z")
    b = p.confirm(confirmed_by="someone-else", confirmed_at="2026-09-09T09:00:00Z")
    assert isinstance(a, ConfirmedSourceIntent)
    assert a.content_hash == b.content_hash  # identity is meaning-only, not who/when confirmed


def test_local_files_connector_scans_a_real_folder(tmp_path):
    (tmp_path / "policy.pdf").write_text("a")
    (tmp_path / "notes.md").write_text("b")
    (tmp_path / "data.csv").write_text("c")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")  # not in default content types
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "readme.txt").write_text("d")

    spec = ProposedSource(kind=SourceKind.FILES, location=str(tmp_path))  # default content types
    reg = SourceConnectorRegistry().register(LocalFilesConnector(clock=lambda: "2026-09-09T00:00:00Z"))
    intent = ConfirmedSourceIntent(project_id="p1", sources=(spec,))
    (cs,) = reg.connect(intent)

    assert cs.kind is SourceKind.FILES and cs.health.state is SourceHealthState.HEALTHY
    assert cs.stats["discovered"] == 5
    assert cs.stats["indexed"] == 4      # pdf, md, csv, txt — png skipped
    assert cs.stats["skipped"] == 1
    assert cs.source_fingerprint  # content-addressed over what was observed


def test_content_type_filter_and_on_demand_indexing(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "b.md").write_text("y")
    spec = ProposedSource(kind=SourceKind.FILES, location=str(tmp_path),
                          allowed_content_types=["pdf"], indexing_policy=IndexingPolicy.ON_DEMAND)
    cs = LocalFilesConnector().connect_and_scan(spec, project_id="p1", source_id="s1")
    assert cs.stats["discovered"] == 2 and cs.stats["skipped"] == 1  # only the pdf is eligible
    assert cs.stats["indexed"] == 0  # on-demand → not indexed at connect time


def test_missing_folder_is_error_not_crash():
    spec = ProposedSource(kind=SourceKind.FILES, location="/no/such/folder/xyz")
    cs = LocalFilesConnector().connect_and_scan(spec, project_id="p1", source_id="s1")
    assert cs.health.state is SourceHealthState.ERROR


def test_unregistered_kind_is_pending_not_fatal():
    intent = ConfirmedSourceIntent(project_id="p1",
                                   sources=(ProposedSource(kind=SourceKind.DATABASE, location="localhost/db"),))
    (cs,) = SourceConnectorRegistry().connect(intent)  # no db connector registered
    assert cs.health.state is SourceHealthState.DEGRADED and "pending" in cs.health.detail


def test_context_grant_is_separate_and_scoped():
    grant = SourceGrant(allowed_schemas=("support", "public"), denied=("billing.card_data", "hr.*"),
                        access_mode=AccessMode.READ_ONLY)
    cs = ContextSource(source_id="s1", project_id="p1", name="CRM", kind=SourceKind.DATABASE,
                       location="localhost/customer_ops", provider="postgres", grant=grant)
    proj = cs.to_projection()
    assert proj["access_mode"] == "read_only"
    assert proj["allowed_schemas"] == ["support", "public"]
    assert proj["denied"] == ["billing.card_data", "hr.*"]
    assert proj["source_runtime"] == "context"
    # no plaintext secret ever appears in the projection
    assert "credential" not in json.dumps(proj).lower() or proj.get("credential_ref") is None


def test_projection_is_json_serializable():
    cs = ContextSource(source_id="s1", project_id="p1", name="Docs", kind=SourceKind.FILES, location="~/docs")
    json.dumps(cs.to_projection())  # must not raise
