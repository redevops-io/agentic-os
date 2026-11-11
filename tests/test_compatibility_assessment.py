"""The engineering compatibility assessment covers every counsel-named gate decision and never claims to be, or to replace, a legal opinion."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "legal" / "COMPATIBILITY_ASSESSMENT.md").read_text(encoding="utf-8")


def counsel_ids():
    g = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    return [d["id"] for d in g["decisions"] if "counsel" in d["decides"]]


def test_every_counsel_named_decision_has_its_own_section_and_a_table_row():
    ids = counsel_ids()
    assert len(ids) == 12
    for i in ids:
        assert re.search(rf"^### {re.escape(i)} ", DOC, re.M), i
        assert re.search(rf"^\| {re.escape(i)} ", DOC, re.M), i


def test_the_document_says_what_it_is_not_and_marks_nothing_decided():
    assert "It is not a legal opinion and it is not counsel's approval" in DOC
    assert "does not mark any decision as decided" in DOC
    assert "All twelve decisions stay OPEN" in DOC
    low = DOC.lower()
    for bad in ("counsel approved", "counsel has approved", "counsel agrees", "approved by counsel", "legal opinion obtained"):
        assert bad not in low, bad


def test_the_commons_clause_policy_is_stated_unchanged():
    assert "The Commons Clause stays; no relicensing is started" in DOC
