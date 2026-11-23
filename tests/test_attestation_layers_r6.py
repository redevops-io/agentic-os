"""SR-04 round 6: the three source-binding layers of the attestation are REDUNDANT defence in depth, and each is pinned
by its own test so a future refactor cannot silently drop one:

1. the content digest includes the metric's `source`;
2. the signing key and MAC input are per (source, adapter id): a MAC made for one pair does not verify for another;
3. verify_attestation refuses an attestation whose adapter id is registered for a DIFFERENT source than the metric's.

Any one of them already stops an attestation from being moved to another source; together they mean a refactor of one
(or a shared key) does not reopen that hole."""
from __future__ import annotations

import dataclasses

import pytest

from agentic_os.market import ingestion as ING
from agentic_os.market.ingestion import IngestionRefused, register_source_adapter, repo_adapter, verify_attestation


def test_layer1_the_content_digest_binds_the_source():
    m = repo_adapter("umami").make_metric(metric="m", value=1.0, segment="control")
    assert ING.content_digest(m) != ING.content_digest(dataclasses.replace(m, source="gsc"))
    assert ING.content_digest(m) == ING.content_digest(dataclasses.replace(m))


def test_layer2_a_mac_is_bound_to_its_source_and_adapter_id_pair():
    a = register_source_adapter("erp", "r6-layer-a")
    b = register_source_adapter("erp", "r6-layer-b")
    digest = "d" * 64
    mac = a._sign(digest)
    assert ING._vault_verify("erp", "r6-layer-a", digest, mac)
    assert not ING._vault_verify("billing", "r6-layer-a", digest, mac)       # another source
    assert not ING._vault_verify("erp", "r6-layer-b", digest, mac)           # another adapter of the same source
    assert not ING._vault_verify("erp", "r6-layer-a", "e" * 64, mac)         # another digest
    assert a._sign(digest) != b._sign(digest)


def test_layer3_the_id_to_source_binding_refuses_even_when_the_mac_check_is_forced_to_pass(monkeypatch):
    erp = register_source_adapter("erp", "r6-layer-erp")
    billing_metric = repo_adapter("billing").make_metric(metric="m", value=1.0, segment="control")
    # an attestation from the erp adapter over a billing metric (the adapter does not check the metric's source)
    forged = dataclasses.replace(billing_metric, attestation=erp.attest(billing_metric))
    monkeypatch.setattr(ING, "_vault_verify", lambda *a, **k: True)           # isolate the layer: MAC check disabled
    with pytest.raises(IngestionRefused, match="not registered for source"):
        verify_attestation(forged)
    monkeypatch.undo()
    with pytest.raises(IngestionRefused):
        verify_attestation(forged)                                            # and the MAC layer refuses it on its own


def test_the_layers_are_documented_as_redundant_defence_in_depth():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "ledger_source_inventory.md").read_text().lower()
    assert "redundant" in text and "defence in depth" in text
