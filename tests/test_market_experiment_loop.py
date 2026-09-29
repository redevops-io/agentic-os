"""Experiment planning + governed execution + outcome learning (Market Intelligence Phases 5-7).

Closes the loop end-to-end offline: opportunities -> planned experiments (proposed vs no_change + EXPLAIN) ->
approval-gated execution (dry-run by default, real executor gated) -> measured outcomes -> learned priors that
reweight future opportunities so first-party results supersede competitor prevalence.

Run:  uv run python -m pytest tests/test_market_experiment_loop.py -q
"""
from __future__ import annotations

from agentic_os.market import (
    DryRunExecutor, Experiment, ExperimentOutcome, ExperimentQueue, Opportunity, learn_priors,
    plan_experiments, proposed, reweight_opportunities)
from agentic_os.market.contracts import Provenance
from agentic_os.market.execution import ExecutionResult

SITE = "redevops.io"


def _opp(pattern_ref, ev, ref=None):
    return Opportunity(prov=Provenance("internal.opportunity_matcher", ref or f"{SITE}:{pattern_ref}"),
                       pattern_ref=pattern_ref, site=SITE, gap=f"{pattern_ref} absent",
                       proposed_experiment=f"add {pattern_ref}", expected_value=ev, evidence_refs=("e1",),
                       confidence=ev)


# ── P5 planning ───────────────────────────────────────────────────────────────────────────────────────────
def test_plan_proposes_top_by_value_and_no_change_below_bar():
    opps = [_opp("offer_pattern:trial", 1.0), _opp("cta_pattern:signup", 0.5),
            _opp("cta_pattern:demo", 0.2)]                       # 0.2 below min_expected_value
    exps = plan_experiments(opps, capacity=3, min_expected_value=0.34)
    by_ref = {e.pattern_ref if False else e.opportunity_ref.split(":", 1)[1]: e for e in exps}
    assert len(exps) == 3                                        # every opportunity yields one experiment
    assert by_ref["offer_pattern:trial"].decision == "proposed" and "PROPOSE" in by_ref["offer_pattern:trial"].explain
    assert by_ref["cta_pattern:demo"].decision == "no_change" and "NO_CHANGE" in by_ref["cta_pattern:demo"].explain
    assert len(proposed(exps)) == 2


def test_capacity_limits_proposed():
    opps = [_opp(f"cta_pattern:a{i}", 0.9, ref=f"{SITE}:a{i}") for i in range(5)]
    assert len(proposed(plan_experiments(opps, capacity=2))) == 2


# ── P6 governed execution ─────────────────────────────────────────────────────────────────────────────────
def test_approve_dry_runs_by_default_and_no_change_is_refused():
    exps = plan_experiments([_opp("offer_pattern:trial", 1.0), _opp("cta_pattern:demo", 0.1)])
    q = ExperimentQueue()                                        # DryRunExecutor, execute disabled
    trial = next(e for e in exps if e.decision == "proposed")
    res = q.approve(trial)
    assert res.status == "dry_run" and "no site change" in res.detail
    assert q.status(trial.prov.provider_ref) == "approved"
    nochange = next(e for e in exps if e.decision == "no_change")
    assert q.approve(nochange).status == "refused"              # no_change can't run


def test_real_executor_only_runs_when_enabled():
    class _RealExec:
        def __init__(self): self.ran = False
        def execute(self, e): self.ran = True; return ExecutionResult(e.prov.provider_ref, "executed", "PR #1")
    exp = proposed(plan_experiments([_opp("offer_pattern:trial", 1.0)]))[0]
    ex = _RealExec()
    # execute_enabled False → falls back to dry-run, real executor NOT called
    assert ExperimentQueue(executor=ex, execute_enabled=False).approve(exp).status == "dry_run" and not ex.ran
    # enabled → real executor runs
    r = ExperimentQueue(executor=ex, execute_enabled=True).approve(exp)
    assert r.status == "executed" and ex.ran


# ── P7 learning ───────────────────────────────────────────────────────────────────────────────────────────
def _outcome(pattern_ref, result):
    return ExperimentOutcome(prov=Provenance("internal.outcome", f"{SITE}:{pattern_ref}:o"),
                             experiment_ref="x", pattern_ref=pattern_ref, site=SITE, result=result)


def test_learned_priors_reweight_and_reorder_opportunities():
    # 'trial' helped us repeatedly; 'demo' hurt — even though both were equally prevalent among competitors
    outcomes = [_outcome("offer_pattern:trial", "improved")] * 3 + [_outcome("cta_pattern:demo", "worse")] * 3
    priors = learn_priors(outcomes)
    assert priors[f"{SITE}|offer_pattern:trial"].multiplier > 1.0
    assert priors[f"{SITE}|cta_pattern:demo"].multiplier < 1.0
    opps = [_opp("offer_pattern:trial", 0.5), _opp("cta_pattern:demo", 0.5)]   # equal competitor prevalence
    re = reweight_opportunities(opps, priors)
    assert re[0].pattern_ref == "offer_pattern:trial"           # our own outcomes promote it above demo
    demo = next(o for o in re if o.pattern_ref == "cta_pattern:demo")
    assert demo.expected_value < 0.5                            # damped by our bad result


def test_unlearned_opportunity_unchanged_explore():
    opps = [_opp("cta_pattern:new", 0.6)]
    re = reweight_opportunities(opps, learn_priors([]))          # no priors yet
    assert re[0].expected_value == 0.6                          # unchanged → still explorable
