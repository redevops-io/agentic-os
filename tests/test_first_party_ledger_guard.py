"""SR-04 round 2/3, E1: a naming-convention guard plus an allow-list in FirstPartyLedger.record
(market/outcomes_join.py). It is NOT enforcement against a caller who mislabels a metric on purpose.

It refuses: non-FirstPartyMetric objects; a non-empty source outside METRIC_SOURCES (the empty legacy default is
tolerated); and a source or provenance provider that looks synthetic (a token starting with 'synthetic' or
'simulat', the token 'ssr', or 'study' / 'study:...'), including an empty source with such a provider. Behaviour
change for out-of-repo callers: sources such as 'stripe' or 'ga4' that older versions accepted are refused.
Every source used by existing code and tests ('umami') is inside METRIC_SOURCES."""
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




# ── round 3: the two false negatives the reviewer found, and the false positives the guard must avoid ──────────
@pytest.mark.parametrize("provider", ["simulation", "Simulator", "simulated", "synthetic.ledger", "ssr", "SSR-panel",
                                      "study", "study:abc", "synthetic-research"])
def test_e1_deny_listed_providers_are_refused_even_with_an_empty_or_valid_source(provider):
    for source in ("", "umami"):
        led = OJ.FirstPartyLedger()
        with pytest.raises(ValueError):
            led.record(metric(source=source, provider=provider))
        assert led.get("signup", "treatment") is None


@pytest.mark.parametrize("provider", ["ssrs", "SSRS-reports", "study-hall", "studio", "umami", "analytics-pipeline",
                                      "internal", "buyer-panel"])
def test_e1_ordinary_providers_are_not_caught_by_the_word_aware_deny_list(provider):
    # 'buyer-panel' with an empty source is a KNOWN false negative of a naming-convention guard (option B in the
    # PR description, `source in METRIC_SOURCES` only, would close it); it is pinned here so the limit is explicit.
    led = OJ.FirstPartyLedger()
    assert led.record(metric(source="umami", provider=provider)).source == "umami"


def test_e1_out_of_repo_sources_the_allow_list_refuses_are_the_documented_behaviour_change():
    led = OJ.FirstPartyLedger()
    for source in ("stripe", "ga4", "internal", "analytics_v2", "umami:site"):
        with pytest.raises(ValueError):
            led.record(metric(source=source))
