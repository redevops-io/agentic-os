"""A capability is reported true only if a named integration test against the REAL product proves it."""
from __future__ import annotations

import ast
from pathlib import Path


def test_every_proven_capability_names_an_existing_integration_test(mods):
    P = mods.onlyoffice_edit.OnlyOfficeEditBackend
    assert set(P.PROOF_TESTS) == set(P.PROVEN)
    tests = set()
    for f in (Path(__file__).parent / "integration").glob("test_*.py"):
        tests |= {n.name for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.FunctionDef)}
    for cap, name in P.PROOF_TESTS.items():
        assert name in tests, f"{cap}: proof test {name} is missing"


def test_read_only_adapter_does_not_claim_edit_capabilities(mods):
    assert not {"automated_edit", "tracked_changes"} & mods.onlyoffice.OnlyOfficeBackend.PROVEN
    assert {"automated_edit", "tracked_changes"} <= mods.onlyoffice_edit.OnlyOfficeEditBackend.PROVEN


def test_other_providers_prove_nothing(mods):
    for prov in ("collabora", "elixiroffice"):
        be = mods.backend.UnimplementedBackend(mods.backend.BackendDecl.parse(
            {"provider": prov, "capabilities": {c: True for c in mods.backend.CAPABILITIES}}))
        assert be.PROVEN == frozenset() and not any(be.describe().report()["capabilities"].values())


def test_wiring_only_uses_the_proven_edit_adapter_for_onlyoffice(mods, tmp_path):
    raw = {"document_backend": {"provider": "collabora", "capabilities": {"automated_edit": True}},
           "document_root": str(tmp_path), "content_base_url": "http://c", "backend_zone": "LOCAL",
           "tenants": {"acme": {"backends": {"collabora": "LOCAL"}}}}
    cfg = mods.config.parse_config(raw, env={})
    be = mods.wiring.build_backend(cfg, mods.content.ContentRegistry("http://c", secret="s"))
    assert be.describe().report()["capabilities"]["automated_edit"] is False
