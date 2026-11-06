"""The counsel brief, the decision packages, the owner form and the quoted texts (docs/legal/).

Their job is to state facts, quote exact texts and list questions, never to conclude. The tests keep them honest:
* every “curly-quoted” passage is a verbatim substring of a source file in this repository;
* EXACT_TEXTS.md and OWNER_DECISION_FORM.md are regenerated from their sources and must match the committed copies;
* every decision of the gate file has a package, a form entry and counsel questions that exist, and no document says that
  anything is cleared or compliant without negating it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "docs" / "legal"
GATE = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text(encoding="utf-8"))["decisions"]
DOCS = ("COUNSEL_BRIEF.md", "DECISION_PACKAGES.md", "QA_BUNDLE_HANDOFF.md", "OWNER_DECISION_FORM.md")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_legal_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _norm(text: str) -> str:
    text = re.sub(r"(^|\n)\s*(/\*|\*/|\*)\s?", " ", text)           # comment markers of source headers
    text = text.replace("**", "")
    return re.sub(r"\s+", " ", text).strip()


def _corpus() -> str:
    files = [LEGAL / "EXACT_TEXTS.md", ROOT / "LICENSE", ROOT / "LICENSE-COMMONS-CLAUSE", ROOT / "LICENSE.md", ROOT / "LICENSING.md"]
    files += sorted((LEGAL / "sources").iterdir()) + sorted((ROOT / "packaging" / "notices" / "texts").iterdir())
    return _norm("\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files if p.is_file()))


def test_exact_texts_is_current():
    assert _load("build_legal_exact_texts").main(["--check"]) == 0


def test_owner_form_is_current_and_has_no_ticked_box():
    assert _load("build_owner_decision_form").main(["--check"]) == 0
    form = (LEGAL / "OWNER_DECISION_FORM.md").read_text(encoding="utf-8")
    assert not re.search(r"- \[[xX]\]", form)
    for d in GATE:
        assert f"### {d['id']}:" in form


def test_sources_manifest_matches_the_files():
    man = json.loads((LEGAL / "sources" / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
    on_disk = {p.name for p in (LEGAL / "sources").iterdir() if p.name != "MANIFEST.json"}
    assert on_disk == {k for k in man if not k.startswith("_")}, sorted(on_disk ^ set(man))
    for name, meta in man.items():
        if name.startswith("_"):
            continue
        assert hashlib.sha256((LEGAL / "sources" / name).read_bytes()).hexdigest() == meta["sha256"], name


@pytest.mark.parametrize("doc", ["COUNSEL_BRIEF.md", "DECISION_PACKAGES.md", "QA_BUNDLE_HANDOFF.md"])
def test_every_curly_quotation_is_verbatim(doc):
    text = (LEGAL / doc).read_text(encoding="utf-8")
    corpus = _corpus()
    quotes = re.findall(r"“([^”]+)”", text)
    assert quotes, doc
    bad = [q for q in quotes if _norm(q) not in corpus]
    assert bad == [], bad


def _questions(brief: str) -> set[str]:
    return set(re.findall(r"\*\*(Q\d+)\.\*\*", brief))


def test_every_decision_has_a_package_a_form_entry_and_existing_counsel_questions():
    packages = (LEGAL / "DECISION_PACKAGES.md").read_text(encoding="utf-8")
    brief = (LEGAL / "COUNSEL_BRIEF.md").read_text(encoding="utf-8")
    qs = _questions(brief)
    assert qs == {f"Q{n}" for n in range(1, 33)}, sorted(qs)
    referenced = set()
    for d in GATE:
        anchor = d["package"].split("#")[1]
        assert f'<a id="{anchor}"></a>' in packages, d["id"]
        assert d["id"] in packages
        assert set(d["counsel_questions"]) <= qs, d["id"]
        referenced |= set(d["counsel_questions"])
        if "counsel" in d["decides"]:
            assert d["counsel_questions"], f"{d['id']} is decided by counsel but names no question"
    assert referenced == qs, sorted(qs - referenced)           # no orphan question


def test_no_document_says_anything_is_cleared_without_a_negation():
    negation = re.compile(r"\b(not|no|never|nothing|none|neither|cannot|without)\b", re.I)
    for doc in DOCS:
        for para in re.split(r"\n\s*\n", (LEGAL / doc).read_text(encoding="utf-8")):
            if re.search(r"\b(cleared|compliant|clearance)\b", para, re.I):
                assert negation.search(para), f"{doc}: {para.strip()[:160]}"


def test_open_source_only_appears_negated_or_quoted():
    for doc in DOCS:
        for n, line in enumerate((LEGAL / doc).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"open[- ]source", line, re.I):
                assert re.search(r"\bnot\b|“|\"|wording|calls the product|says", line, re.I), f"{doc}:{n}: {line.strip()[:120]}"


def test_the_statement_wording_in_the_documents_is_the_family_wording():
    brief = (LEGAL / "COUNSEL_BRIEF.md").read_text(encoding="utf-8")
    assert "source-available (family licence: AGPL-3.0-or-later with the Commons Clause)" in _norm(brief)


def test_decision_packages_state_what_ships_meanwhile():
    packages = (LEGAL / "DECISION_PACKAGES.md").read_text(encoding="utf-8")
    assert "PRIVATE engineering candidate" in packages and "REDEVOPS_DISTRIBUTION_BUILD=1" in packages
