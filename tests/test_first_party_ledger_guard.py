"""SR-04 round 2, E1: the first-party ledger refuses anything that is not an observed first-party metric.

This is the ONLY edit outside agentic_os/content/ and agentic_os/market/synthetic_research/ besides the E2 guards:
a minimal guard in FirstPartyLedger.record (market/outcomes_join.py). Every source used by existing code and tests
("umami") is inside METRIC_SOURCES, so the guard is an allow-list (plus a deny-list for synthetic-looking
sources/providers); the empty legacy default source is still accepted."""
from __future__ import annotations

import pytest

from agentic_os.market import outcomes_join as OJ
from agentic_os.market.contracts import Provenance
from agentic_os.market.synthetic_research.evidence_ref import StudyEvidenceRef

REF = StudyEvidenceRef("study-1", "sha256:" + "a" * 64, "relevance", "sentence-transformers:bge-small")


def metric(source="umami", provider="umami", **kw):
    return OJ.FirstPartyMetric(prov=Provenance(provider=provider), source=source, metric=kw.pop("metric", "signup"),
                               value=0.5, segment="treatment", **kw)


@pytest.mark.parametrize("source", ["synthetic-ssr", "synthetic", "SSR", " ssr:likert", "Simulated", "simulated-demo",
                                    "study:abc", "STUDY", "\tSynthetic"])
def test_e1_ledger_refuses_synthetic_sources(source):
    led = OJ.FirstPartyLedger()
    with pytest.raises(ValueError):
        led.record(metric(source=source))
    assert led.get("signup", "treatment") is None


def test_e1_ledger_refuses_synthetic_provenance_and_unknown_sources_and_non_metrics():
    led = OJ.FirstPartyLedger()
    with pytest.raises(ValueError):
        led.record(metric(source="umami", provider="synthetic-research"))
    with pytest.raises(ValueError):
        led.record(metric(source="made-up-source"))
    for bad in (REF, REF.to_ref(), REF.to_dict(), None, object()):
        with pytest.raises(TypeError):
            led.record(bad)                                           # type: ignore[arg-type]
    assert led.get("signup", "treatment") is None


def test_e1_every_real_source_and_the_empty_default_are_still_accepted():
    led = OJ.FirstPartyLedger()
    for i, src in enumerate(OJ.METRIC_SOURCES):
        assert led.record(metric(source=src, provider=src, metric=f"m{i}")).source == src
    assert led.record(metric(source="", provider="x", metric="legacy_default")).source == ""
    assert OJ.METRIC_SOURCES == ("umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics")


