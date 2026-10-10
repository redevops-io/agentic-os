"""acquisition.market_observe + acquisition.content_create are bound + callable through the orchestrator.

The wiring-maturity audit found both descriptors labelled COMPLETE but with NO handler (tested-unwired). This
proves bind_commercial_capabilities now binds them and each runs end to end via SidekickOrchestrator.invoke,
projecting a real answer + governed candidate actions (content stays at the publish gate — drafts, not sends).
"""
from __future__ import annotations

from agentic_os.commercial import bind_commercial_capabilities
from agentic_os.content import Channel, ContentBrief, FakeGenerator, FakePublisher
from agentic_os.market import MarketObservations, TrackedCompany
from agentic_os.market.contracts import CTA, PageSnapshot, Provenance
from agentic_os.market.observe import FetchedPage
from agentic_os.sidekick import CapabilityRegistry, SidekickOrchestrator, register_builtin_capabilities

_TRIAL = ("<html><body><p>Start your free trial today.</p>"
          "<a href='/signup'>Start free trial</a> <a href='/demo'>Book a demo</a></body></html>")


class _Fetcher:
    def get(self, url):
        return FetchedPage(url=url, status=200, html=_TRIAL)


def _orc():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bind_commercial_capabilities(reg)
    return reg, SidekickOrchestrator(registry=reg)


def test_market_observe_is_bound_and_runs():
    reg, orc = _orc()
    assert reg.get("acquisition.market_observe").bound is True
    own = MarketObservations(
        companies=(TrackedCompany(prov=Provenance("own", "us"), name="Us", domain="redevops.io"),),
        snapshots=(PageSnapshot(prov=Provenance("website", "us"), company_ref="us",
                                url="https://redevops.io", page_kind="home", title="Us"),),
        ctas=(CTA(prov=Provenance("website", "us"), page_ref="us", text="Book a demo", action="demo"),))
    watch = [TrackedCompany(prov=Provenance("seed", r), name=r, domain=f"{r}.example",
                            category="ai-agents", role="competitor") for r in ("a", "b")]
    res = orc.invoke("acquisition.market_observe", watch_list=watch, fetcher=_Fetcher(),
                     own=own, site="redevops.io")
    assert "Observed" in res.answer and "opportunity" in res.answer
    assert all(a["action"] == "flag_opportunity" for a in res.candidate_actions)


def test_content_create_is_bound_and_stays_at_the_gate():
    reg, orc = _orc()
    assert reg.get("acquisition.content_create").bound is True
    brief = ContentBrief(campaign_id="c-1", subject="ReDevOps", angle="runtime-native",
                         key_message="Turn chaotic workflows into learning systems",
                         channels=(Channel.X, Channel.LINKEDIN), cta="See how →")
    res = orc.invoke("acquisition.content_create", brief=brief,
                     generator=FakeGenerator(), publisher=FakePublisher(publishable=()))
    assert "Drafted content" in res.answer and res.evidence
    # publishing is gated: the only proposed action is to REQUEST approval, never an auto-send
    assert res.candidate_actions and res.candidate_actions[0]["action"] == "request_publish_approval"
