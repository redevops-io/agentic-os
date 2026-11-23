"""SR-04 round 4: the first-party ledger guard (market/outcomes_join.py + market/ingestion.py).

Owner decision (option B): a strict METRIC_SOURCES allow-list, no provider/name heuristics, missing or unregistered
sources refused; plus defence in depth: a metric must carry an attestation minted by a REGISTERED source adapter and
bound to the metric's content, and synthetic/study artifacts are refused by content and type whatever label they
carry. The guard is unconditional (no feature flag). It is bypassable by code in the same
process; these tests try every bypass a caller could attempt WITHOUT reaching into private registry state."""
from __future__ import annotations

import ast
import dataclasses
import pathlib
from types import SimpleNamespace

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import ingestion as ING
from agentic_os.market import outcomes_join as OJ
from agentic_os.market.ingestion import IngestionRefused, register_source_adapter, repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger, FirstPartyMetric
from agentic_os.market.synthetic_research.evidence_ref import StudyEvidenceRef

REF = StudyEvidenceRef("study-1", "sha256:" + "a" * 64, "relevance", "sentence-transformers:bge-small")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def minted(source="umami", **kw):
    return repo_adapter(source).make_metric(metric=kw.pop("metric", "signup"), value=kw.pop("value", 0.5),
                                            segment=kw.pop("segment", "treatment"), **kw)


def hand(source="umami", provider="umami", **kw):
    return FirstPartyMetric(prov=Provenance(provider=provider), source=source, metric="signup", value=0.5,
                            segment="treatment", **kw)


def refused(m, exc=(ValueError, TypeError)):
    led = FirstPartyLedger()
    with pytest.raises(exc):
        led.record(m)
    assert led.get("signup", "treatment") is None


# ── what is accepted ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("source", ING.METRIC_SOURCES)
def test_a_metric_minted_by_a_registered_adapter_is_accepted_for_every_registered_source(source):
    led = FirstPartyLedger()
    m = minted(source)
    assert led.record(m) is m and led.value("signup", "treatment") == 0.5


def test_metric_sources_are_unchanged_and_contain_no_synthetic_source():
    assert OJ.METRIC_SOURCES == ("umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics")
    assert OJ.METRIC_SOURCES is ING.METRIC_SOURCES


def test_digest_and_serialisation_are_unchanged_by_the_attestation_field():
    m = minted()
    plain = dataclasses.replace(m, attestation="")
    assert m.digest() == plain.digest() and "attestation" not in m.to_dict()


# ── bypass attempt 1: labels ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("source", ["umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics", "ga4",
                                    "internal", "analytics_v2", "umami:site", "stripe", "UMAMI", " gsc ",
                                    "synthetic", "ssr:likert", "study:abc"])
@pytest.mark.parametrize("provider", ["umami", "internal", "buyer-panel", "ssrs-reports", "simulation"])
def test_a_hand_built_metric_is_refused_whatever_label_or_provider_it_carries(source, provider):
    refused(hand(source, provider), ValueError)


@pytest.mark.parametrize("source", ["", " ", "   ", "\t", None])
def test_missing_empty_or_whitespace_sources_are_refused(source):
    refused(hand(source, "buyer-panel"), ValueError)
    m = minted()
    refused(dataclasses.replace(m, source=source), ValueError)             # even with a copied attestation


def test_the_unregistered_source_error_names_the_allowed_sources_and_the_way_forward():
    with pytest.raises(ValueError, match="register an adapter"):
        FirstPartyLedger().record(hand("ga4"))
    with pytest.raises(IngestionRefused, match="register_source_adapter"):
        FirstPartyLedger().record(hand("umami"))


# ── bypass attempt 2: type tricks ────────────────────────────────────────────────────────────────────────
def test_duck_typed_and_subclassed_metrics_are_refused():
    m = minted()
    fields = {f.name: getattr(m, f.name) for f in dataclasses.fields(m)}
    refused(SimpleNamespace(**fields), TypeError)
    Sub = type("Sub", (FirstPartyMetric,), {})
    refused(Sub(**fields), TypeError)

    class Liar(FirstPartyMetric):
        def digest(self):                                                   # a subclass that rewrites its own digest
            return "sha256:forged"
    refused(Liar(prov=m.prov, metric="signup", value=0.5, segment="treatment", attestation=m.attestation), TypeError)
    for junk in (None, {"source": "umami"}, "umami", object(), 5):
        refused(junk, TypeError)


def test_a_provenance_subclass_is_refused():
    class P(Provenance):
        pass
    m = minted()
    refused(dataclasses.replace(m, prov=P(provider=m.prov.provider, observed_at=m.prov.observed_at)), TypeError)


# ── bypass attempt 3: reusing or forging the attestation ─────────────────────────────────────────────────
def test_an_attestation_cannot_be_reused_on_a_different_metric():
    m = minted()
    for other in (dataclasses.replace(m, value=0.9), dataclasses.replace(m, metric="other"),
                  dataclasses.replace(m, segment="control"), dataclasses.replace(m, surface="pricing"),
                  dataclasses.replace(m, higher_is_better=False), dataclasses.replace(m, period_end_ms=5)):
        assert other.attestation == m.attestation
        refused(other, IngestionRefused)


def test_an_attestation_for_one_source_cannot_be_moved_to_another_source():
    m = minted("umami")
    refused(dataclasses.replace(m, source="gsc"), IngestionRefused)
    refused(dataclasses.replace(m, source="analytics"), IngestionRefused)


def test_mutating_a_minted_metric_or_its_provenance_invalidates_the_attestation():
    m = minted()
    object.__setattr__(m, "value", 9.9)
    refused(m, IngestionRefused)
    m2 = minted()
    refused(dataclasses.replace(m2, prov=dataclasses.replace(m2.prov, provider_ref="forged")), IngestionRefused)
    m3 = minted()
    refused(dataclasses.replace(m3, prov=dataclasses.replace(m3.prov, provider="internal")), IngestionRefused)
    m4 = minted()
    refused(dataclasses.replace(m4, prov=dataclasses.replace(m4.prov, observed_at=m4.prov.observed_at + 1)),
            IngestionRefused)


@pytest.mark.parametrize("att", ["", None, "v1:", "v1:repo:umami", "v1:repo:umami:" + "0" * 64, "v2:repo:umami:abc",
                                 "v1:nobody:" + "a" * 64, "garbage", "v1::", 5])
def test_forged_or_malformed_attestations_are_refused(att):
    refused(dataclasses.replace(minted(), attestation=att), (ValueError, TypeError))


# ── bypass attempt 4: study / synthetic artifacts, labelled as anything ──────────────────────────────────
def test_study_and_synthetic_objects_are_refused_by_type_and_content():
    for obj in (REF, REF.to_dict(), REF.to_ref(), SimpleNamespace(source="umami", synthetic=True)):
        refused(obj, (ValueError, TypeError))
    m = minted()
    fields = {f.name: getattr(m, f.name) for f in dataclasses.fields(m)}
    s = SimpleNamespace(**fields, synthetic=True)
    refused(s, ValueError)

    class Resp(FirstPartyMetric):
        synthetic = True
    refused(Resp(prov=m.prov, source="umami", metric="signup", value=0.5, segment="treatment",
                 attestation=m.attestation), ValueError)


@pytest.mark.parametrize("ref", ["synthetic-study:v1:{}", "Synthetic-Study:v1:{}", " synthetic-study:v1:x",
                                 "\tSYNTHETIC-STUDY:v2:x", "synthetic-study:"])
def test_a_properly_attested_metric_that_smuggles_a_study_ref_is_still_refused(ref):
    refused(minted(evidence_refs=("obs:1", ref)), ValueError)
    refused(minted(provider_ref=ref), ValueError)
    refused(minted(metric=ref), ValueError)
    refused(minted(surface=ref), ValueError)


def test_a_study_ref_stored_in_an_otherwise_valid_metric_by_mutation_is_refused():
    m = minted()
    object.__setattr__(m.prov, "evidence_refs", (REF.to_ref(),))
    refused(m, ValueError)


def test_the_guard_is_unconditional_and_independent_of_the_synthetic_research_flag(monkeypatch):
    for value in (None, "", "0", "1", "true", "on"):
        if value is None:
            monkeypatch.delenv("AGENTIC_OS_SYNTHETIC_RESEARCH", raising=False)
        else:
            monkeypatch.setenv("AGENTIC_OS_SYNTHETIC_RESEARCH", value)
        refused(hand("umami"), ValueError)
        refused(hand("ga4"), ValueError)
        refused(minted(evidence_refs=(REF.to_ref(),)), ValueError)
        assert FirstPartyLedger().record(minted()) is not None
    for name in ("outcomes_join.py", "ingestion.py"):
        code = "\n".join(l for l in (ROOT / "agentic_os" / "market" / name).read_text().splitlines()
                         if not l.lstrip().startswith(("#",)))
        assert "environ" not in code and "getenv" not in code, name


# ── the registry ─────────────────────────────────────────────────────────────────────────────────────────
def test_registration_is_limited_to_metric_sources_and_cannot_be_repeated():
    for bad in ("ga4", "stripe", "internal", "analytics_v2", "umami:site", "UMAMI", "", "synthetic"):
        with pytest.raises(IngestionRefused):
            register_source_adapter(bad, "acme-import")
    a = register_source_adapter("analytics", "acme-registry-test-1", owner="test")
    with pytest.raises(IngestionRefused, match="already registered"):
        register_source_adapter("analytics", "acme-registry-test-1")
    assert ("analytics", "acme-registry-test-1") in ING.registered_adapters()
    led = FirstPartyLedger()
    m = a.make_metric(metric="signup", value=0.3, segment="control", period_start_ms=1, period_end_ms=2)
    assert led.record(m).source == "analytics" and m.prov.provider == "acme-registry-test-1"


@pytest.mark.parametrize("adapter_id", ["ssr-import", "synthetic-panel", "simulation-feed", "study-import",
                                        "Acme", "x", "a" * 80, "bad id", "SSR"])
def test_adapter_ids_that_look_synthetic_or_malformed_are_refused(adapter_id):
    with pytest.raises(IngestionRefused):
        register_source_adapter("billing", adapter_id)


def test_an_adapter_repr_does_not_leak_its_key_and_a_second_adapter_cannot_attest_for_the_first():
    a = register_source_adapter("erp", "acme-erp-one")
    b = register_source_adapter("erp", "acme-erp-two")
    assert "key" not in repr(a).lower()
    m = a.make_metric(metric="signup", value=0.1)
    forged = dataclasses.replace(m, attestation=b.attest(m).replace("acme-erp-two", "acme-erp-one"))
    refused(forged, IngestionRefused)
    assert FirstPartyLedger().record(b.make_metric(metric="signup", value=0.1, segment="treatment")) is not None


# ── architecture: SSR / study code cannot reach the ledger, outcomes_join, adapters or the outcome store ────
def _imported_names(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = "." * n.level + (n.module or "")
            names.add(base)
            names.update(f"{base}.{a.name}" for a in n.names)
    return names


def test_no_synthetic_research_module_imports_the_ledger_adapters_or_the_outcome_store():
    pkg = ROOT / "agentic_os" / "market" / "synthetic_research"
    forbidden = ("outcomes_join", "ingestion", "outcome_store", "FirstPartyLedger", "FirstPartyMetric",
                 "register_source_adapter", "repo_adapter", "OutcomeEvent", "priority_engine", "policy_promotion")
    files = sorted(pkg.glob("*.py"))
    assert files
    for f in files:
        for name in _imported_names(f):
            assert not any(tok in name for tok in forbidden), (f.name, name)
        text = f.read_text()
        for tok in ("FirstPartyLedger", "register_source_adapter", "repo_adapter", "OutcomeStore"):
            if f.name not in ("__init__.py", "evidence_ref.py"):        # those two mention them in prose only
                assert tok not in text, (f.name, tok)
    assert "market.ingestion" not in (pkg / "evidence_ref.py").read_text().replace("this module, ", "")


def test_the_ingestion_module_does_not_import_outcomes_join_or_the_synthetic_package_at_import_time():
    tree = ast.parse((ROOT / "agentic_os" / "market" / "ingestion.py").read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = " ".join(ast.dump(n) for n in top)
    assert "outcomes_join" not in names and "synthetic_research" not in names
