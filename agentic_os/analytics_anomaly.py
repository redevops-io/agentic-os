"""Analytics: Anomaly → Explanation → Action — a domain PRODUCER for the shared decision/outcome loop
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §13).

Conventional analytics stops at "metric changed → alert". The agentic version decides whether the
change MATTERS, then offers candidate responses — investigate the cause, alert the owner, roll back a
suspected change, adjust a lever, or just keep watching — and the shared runtime selects and learns
which response actually corrects the metric. Analytics is a *producer*: it detects material anomalies
and maps observed results back into OutcomeEvents; selection + learning live in the shared runtime.

Deterministic materiality gate (a z-score against the metric's own baseline), so an immaterial wobble
yields only 'monitor' rather than crying wolf. Rolling back or adjusting a live lever is consequential
⇒ approval (§19).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, OutcomeEvent


@dataclass(frozen=True)
class MetricAnomaly:
    metric: str
    observed: float
    baseline_mean: float
    baseline_std: float
    higher_is_better: bool = True    # a drop in a good metric (or a rise in a bad one) is ADVERSE
    owner: str = ""


def zscore(a: MetricAnomaly) -> float:
    return abs(a.observed - a.baseline_mean) / a.baseline_std if a.baseline_std > 1e-9 else 0.0


def is_material(a: MetricAnomaly, z_threshold: float = 2.0) -> bool:
    return zscore(a) >= z_threshold


def is_adverse(a: MetricAnomaly) -> bool:
    dropped = a.observed < a.baseline_mean
    return dropped if a.higher_is_better else (not dropped)


def _c(metric: str, kind: str, action: str, ev: float, tier: RiskTier, *, urgency: float = 0.4) -> InterventionCandidate:
    return InterventionCandidate(
        source_app="analytics", subject=metric, proposed_action=action, expected_value=ev,
        confidence=0.7, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.6,
        required_capabilities=(f"analytics.{kind}",), candidate_id=f"analytics:{metric}:{kind}")


def anomaly_opportunity(a: MetricAnomaly) -> DecisionOpportunity:
    """Turn a metric movement into candidate responses — but only escalate a MATERIAL, adverse move;
    an immaterial wobble offers just 'monitor' so the loop can't be pushed into acting on noise."""
    z = zscore(a)
    actions: list = []
    if is_material(a) and is_adverse(a):
        urgency = min(0.9, 0.3 + 0.1 * z)
        actions.append(_c(a.metric, "investigate_cause", "Investigate the probable cause", 0.6,
                          RiskTier.READ, urgency=urgency))            # low-risk analysis ⇒ can auto-run
        actions.append(_c(a.metric, "alert_owner", "Alert the metric owner", 0.5, RiskTier.BOUNDED_WRITE,
                          urgency=urgency))
        actions.append(_c(a.metric, "roll_back_change", "Roll back the suspected change", 0.65,
                          RiskTier.CONSEQUENTIAL, urgency=urgency))
        actions.append(_c(a.metric, "adjust_lever", "Adjust an operational lever", 0.5,
                          RiskTier.CONSEQUENTIAL, urgency=urgency))
    elif is_material(a):                                             # material but favourable — learn from it
        actions.append(_c(a.metric, "investigate_cause", "Investigate what drove the improvement", 0.4,
                          RiskTier.READ))
    actions.append(_c(a.metric, "monitor", "Keep watching, no intervention yet", 0.15, RiskTier.READ,
                      urgency=0.1))
    return DecisionOpportunity(
        entity=a.metric, source_app="analytics", candidate_actions=tuple(actions),
        evidence=(f"z={z:.1f}", f"observed={a.observed:.2f}", f"baseline={a.baseline_mean:.2f}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL},
        uncertainty=max(0.1, 1.0 - min(1.0, z / 5.0)), opportunity_id=f"analytics:{a.metric}")


def analytics_outcome(action: InterventionCandidate, *, cause_found: bool = False,
                      metric_recovered: bool = False, false_alarm: bool = False,
                      delay_hours: float = 0.0) -> OutcomeEvent:
    """Map the result of an analytics action. Recovering the metric is the real win; a false alarm
    (the anomaly didn't matter) is a negative so the loop learns not to over-react to noise."""
    dims = {}
    if cause_found:
        dims["cause_found"] = 1.0
    if metric_recovered:
        dims["metric_recovered"] = 1.0
    if false_alarm:
        dims["false_alarm"] = -1.0
    weights = {"cause_found": 0.3, "metric_recovered": 1.0, "false_alarm": 0.7}
    raw = sum(weights.get(k, 1.0) * v for k, v in dims.items())
    attribution = 0.85 if delay_hours <= 24 else max(0.4, 0.85 - 0.01 * (delay_hours / 24.0))
    return OutcomeEvent(candidate_id=action.candidate_id, source_app="analytics",
                        action_kind=action.action_kind, observed_reward=raw, reward_dimensions=dims,
                        delay=delay_hours, attribution_confidence=attribution)
