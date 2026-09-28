"""Content signals → governed approval queue (Content & Search Intelligence §17, §19). Offline.

PROPOSE-status content signals (search or behavior) become CONSEQUENTIAL, approval-gated decisions ranked by
priority; WATCH signals are counted, not acted on. Umami behavior detectors calibrate to the site median.
"""
from __future__ import annotations

from agentic_os.content.behavior_signals import (
    BehaviorSignalType, high_traffic_leverage, scan_behavior, underperforming_page,
)
from agentic_os.content.interventions import (
    content_intervention_queue, from_content_signal, plan_content_interventions,
)
from agentic_os.content.search_signals import SearchSignal, SignalType
from agentic_os.integrations.umami import AnalyticsObservation
from agentic_os.priority_engine import Action, RiskTier


def _search(kind, page, conf=0.8, status="PROPOSE"):
    return SearchSignal(kind, query="q", affected_pages=(page,), confidence=conf, status=status,
                        proposed_action=f"do {kind.value}", evidence=("gsc:x",), site_id="redevops")


# ── from_content_signal ─────────────────────────────────────────────────────────────────────────────────
def test_propose_signal_becomes_consequential_candidate():
    c = from_content_signal(_search(SignalType.NEAR_WIN, "https://redevops.io/a"))
    assert c is not None and c.risk_tier is RiskTier.CONSEQUENTIAL
    assert c.subject == "https://redevops.io/a" and c.required_capabilities == ("content.page.strengthen",)
    assert c.action_kind == "NEAR_WIN"


def test_watch_signal_is_not_an_action():
    assert from_content_signal(_search(SignalType.NEAR_WIN, "p", status="WATCH")) is None
    assert from_content_signal(_search(SignalType.EMERGENT_INTENT, "p")) is None  # unmapped (watch-only)


# ── queue ───────────────────────────────────────────────────────────────────────────────────────────────
def test_queue_ranks_and_parks_on_approval_and_counts_watching():
    signals = [
        _search(SignalType.NEAR_WIN, "https://redevops.io/near", conf=0.9),
        _search(SignalType.CTR_OPPORTUNITY, "https://redevops.io/ctr", conf=0.7),
        _search(SignalType.MISSING_PAGE, "https://redevops.io/missing", status="WATCH"),  # watching
    ]
    q = content_intervention_queue(signals)
    assert q["count"] == 2 and q["requires_approval"] == 2 and q["auto"] == 0 and q["watching"] == 1
    assert q["decisions"][0]["page"] == "https://redevops.io/near"    # highest priority first
    assert all(d["action"] == "request_approval" for d in q["decisions"])


def test_plan_drops_abstained_low_confidence():
    from agentic_os.priority_engine import PriorityPolicy
    weak = _search(SignalType.NEAR_WIN, "p", conf=0.2)
    assert plan_content_interventions([weak], policy=PriorityPolicy(min_confidence=0.5)) == []


# ── umami behavior detectors ────────────────────────────────────────────────────────────────────────────
def _obs(url, views, site="redevops"):
    return AnalyticsObservation(page_url=url, pageviews=views, visitors=views, site_id=site)


def test_underperforming_and_leverage_detectors():
    assert underperforming_page(_obs("p", 5), site_median=100) is not None      # 5 << 30 (0.3*100)
    assert underperforming_page(_obs("p", 40), site_median=100) is None          # 40 > 30
    assert high_traffic_leverage(_obs("p", 350), site_median=100) is not None    # 350 >= 3*100
    assert high_traffic_leverage(_obs("p", 150), site_median=100) is None
    assert underperforming_page(_obs("p", 5), site_median=10) is None            # median below min_median


def test_scan_behavior_calibrates_to_site_median():
    obs = [_obs("/hot", 400), _obs("/ok1", 100), _obs("/ok2", 90), _obs("/ok3", 110), _obs("/cold", 5)]
    sigs = scan_behavior(obs)                     # median ≈ 100
    kinds = {(s.signal_type, s.affected_pages[0]) for s in sigs}
    assert (BehaviorSignalType.HIGH_TRAFFIC_LEVERAGE, "/hot") in kinds
    assert (BehaviorSignalType.UNDERPERFORMING_PAGE, "/cold") in kinds


def test_behavior_signals_flow_into_the_content_queue():
    obs = [_obs("/hot", 400), _obs("/ok1", 100), _obs("/ok2", 100), _obs("/cold", 5)]
    q = content_intervention_queue(scan_behavior(obs))
    assert q["count"] == 2 and q["requires_approval"] == 2                        # both park on approval
    signals = {d["signal"] for d in q["decisions"]}
    assert signals == {"HIGH_TRAFFIC_LEVERAGE", "UNDERPERFORMING_PAGE"}
