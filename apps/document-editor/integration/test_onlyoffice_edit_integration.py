"""Edit-path integration tests against a REAL stock ONLYOFFICE Document Server (Document Builder service).

Proofs for ``automated_edit`` and ``tracked_changes`` (see ``OnlyOfficeEditBackend.PROOF_TESTS``).
"""
from __future__ import annotations

import hashlib
import importlib
import json

import pytest

pytestmark = pytest.mark.integration

APPROVAL = {"approved": True, "by": "integration-test"}


def _m(name):
    return importlib.import_module(f"document-editor.{name}")


def _apply(live, p, key):
    return live.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                                patch_digest=p["patch_digest"], expected_version=p["base_version"],
                                idempotency_key=key, approval=APPROVAL, actor="it")


def test_automated_edit_docx_and_xlsx(live, seeds):
    p = live.svc.propose_patch("acme", seeds.docx, [
        {"type": "replace_text", "search": "100 EUR", "replace": "120 EUR"},
        {"type": "append_paragraph", "text": "Approved by the governed connector."}], actor="it")
    assert p["valid"], p
    r = _apply(live, p, "it-edit-docx-0001")
    assert r["status"] == "ok" and r["verified"] and r["new_version"] == 2
    data = live.store.read("acme", seeds.docx, 2)[0]
    paras = _m("ooxml").docx_paragraphs(data)
    assert "The price is 120 EUR." in paras and paras[-1] == "Approved by the governed connector."
    assert live.store.read("acme", seeds.docx, 1)[0] != data                       # v1 untouched
    # spreadsheet cells
    px = live.svc.propose_patch("acme", seeds.xlsx, [{"type": "set_cell", "cell": "B2", "value": 99},
                                                      {"type": "set_cell", "cell": "C2", "value": "edited"}], actor="it")
    assert px["valid"], px
    rx = _apply(live, px, "it-edit-xlsx-0001")
    assert rx["status"] == "ok" and rx["verified"]
    out = live.store.read("acme", seeds.xlsx, rx["new_version"])[0]
    assert _m("ooxml").xlsx_cell(out, "B2") == "99" and _m("ooxml").xlsx_cell(out, "C2") == "edited"


def test_tracked_changes_produce_revisions(live, seeds):
    # contract.docx is now at v2 from the previous test; propose against the head
    head = live.store.head("acme", seeds.docx)
    p = live.svc.propose_patch("acme", seeds.docx, [{"type": "replace_text", "search": "120 EUR", "replace": "130 EUR"}],
                               track_changes=True, actor="it")
    assert p["valid"] and p["base_version"] == head.version
    r = _apply(live, p, "it-edit-track-0001")
    assert r["status"] == "ok" and r["verified"]
    data = live.store.read("acme", seeds.docx, r["new_version"])[0]
    rev = _m("ooxml").docx_revisions(data)
    assert rev["insertions"] >= 1 and rev["deletions"] >= 1                       # a real tracked deletion + insertion
    assert "The price is 130 EUR." in _m("ooxml").docx_paragraphs(data)             # visible (accepted-view) text
    assert "120 EUR" in "".join(_m("ooxml").docx_paragraphs(data, include_deleted=True))


def test_stale_version_is_rejected_against_the_real_server_flow(live, seeds):
    p = live.svc.propose_patch("acme", seeds.docx, [{"type": "append_paragraph", "text": "late"}], actor="it")
    live.svc.store.put_version("acme", seeds.docx, live.store.read("acme", seeds.docx)[0], expected_version=p["base_version"],
                               actor="other", kind="edit")
    with pytest.raises(_m("errors").StaleVersion):
        _apply(live, p, "it-edit-stale-0001")


def test_a_script_cannot_come_from_the_caller(live, seeds):
    """The only script the editor ever runs is the fixed template; hostile op values are data, not code."""
    p = live.svc.propose_patch("acme", seeds.docx, [{"type": "append_paragraph",
                                                      "text": "'); Api.GetDocument().RemoveAllElements(); //"}], actor="it")
    r = _apply(live, p, "it-edit-hostile-0001")
    paras = _m("ooxml").docx_paragraphs(live.store.read("acme", seeds.docx, r["new_version"])[0])
    assert paras[-1] == "'); Api.GetDocument().RemoveAllElements(); //" and len(paras) > 3
