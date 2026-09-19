"""Workflow learning — the experience→workflow-improvement loop (Workflow-Teaching plan §9/§10/§15/§20).

The Projects extension has two learning inputs. *Demonstration* teaches the runtime the initial workflow
(recording → WorkflowCandidate → review → WorkflowDefinition). This module is the other half: **experience
teaches where that workflow should improve** — turning the outcomes and repeated decisions of past Mission
runs into proposed, governed, versioned amendments.

It does **not** re-implement a learning mechanism. It drives the one canonical engine — ``discovery_runtime.
learn`` — whose single invariant is the one the chess ablation earned: *a correction is not a rule*. A human
decision (or a lucky outcome) is an ``Experience``; enough consistent experiences become a ``Pattern`` with a
conservative Wilson-lower-bound confidence; a Pattern can be *proposed* as a candidate; and only an explicit,
governed review turns an eligible candidate into a new WorkflowDefinition version. Nothing here promotes on
its own, and the confidence arithmetic lives in the engine, not here.

Governance (plan §18) is enforced at the boundary, not assumed:
  * observation is not authorization — a learned rule is ``INFERRED`` until review makes it ``HUMAN_CONFIRMED``;
  * policy beats learned behavior — a caller-supplied ``policy_forbids`` predicate (the Governance plane, not
    model-visible context) can veto any candidate, and a veto never yields a version;
  * ACTIVE versions are immutable — acceptance always produces a NEW ``WorkflowDefinition`` with
    ``parent_version_id`` set; the active one is never mutated;
  * a workflow with material UNKNOWNs cannot activate unless policy permits abstention there.
"""
from __future__ import annotations

from dataclasses import replace

from discovery_runtime.learn import (
    Experience,
    Origin,
    Outcome,
    Pattern,
    PromotionPolicy,
    mine,
    observe,
)
from runtime_contracts import DecisionEvidence

from .contracts import (
    CandidateStatus,
    HumanGate,
    RuleProvenance,
    WorkflowDefinition,
    WorkflowLearningCandidate,
    WorkflowRule,
    WorkflowStatus,
    _now,
)

#: A correction is not a rule: >1 supporting observation and an explicit review are BOTH required, so no
#: single decision, demonstration or lucky outcome can promote. Callers may tune per workflow.
DEFAULT_POLICY = PromotionPolicy()


# ──────────────────────────── observations → experiences ────────────────────────────

def experience_from_outcome(*, context_signature: str, decision: str, action: str, good: bool,
                            decision_ref: str = "", mission_ref: str = "", runtime_version: str = "",
                            origin: Origin = Origin.EXPERIENCE) -> Experience:
    """An outcome-driven experience (§20): an action was taken and its outcome was good or bad
    (outreach → reply / no-reply; opportunity → WON / LOST). Evidence reuses the canonical
    ``DecisionEvidence`` so learning provenance and Discovery provenance are the same currency."""
    ev = tuple(DecisionEvidence(reader_id="projects", kind="outcome", value=action, source_ref=r)
               for r in (decision_ref, mission_ref) if r)
    return observe(context_signature, decision, action, Outcome.GOOD if good else Outcome.BAD,
                   evidence=ev, runtime_version=runtime_version, origin=origin)


def experience_from_choice(*, context_signature: str, chosen_action: str, decision_ref: str = "",
                           mission_ref: str = "", runtime_version: str = "",
                           origin: Origin = Origin.EXPERIENCE) -> Experience:
    """A decision-driven experience (§9): in this context the operator *chose* ``chosen_action`` (over the
    alternatives). The "outcome" of a choice is simply that it was the choice — good by construction; what
    makes a choice a candidate rule is that it *recurs*, and that alternatives in the same context are
    counterexamples (see :func:`propose_from_choices`)."""
    ev = tuple(DecisionEvidence(reader_id="projects", kind="decision", value=chosen_action, source_ref=r)
               for r in (decision_ref, mission_ref) if r)
    return observe(context_signature, "operator choice", chosen_action, Outcome.GOOD,
                   evidence=ev, runtime_version=runtime_version, origin=origin)


# ──────────────────────────── mining → candidates ────────────────────────────

def _evidence_refs(pattern: Pattern) -> tuple[str, ...]:
    refs: list[str] = []
    for e in pattern.supporting:
        for d in e.evidence:
            if d.source_ref and d.source_ref not in refs:
                refs.append(d.source_ref)
    return tuple(refs)


def _candidate_from_pattern(project_id: str, workflow: WorkflowDefinition, pattern: Pattern, *,
                            intent: str, statement: str, gate: HumanGate,
                            source_decision_refs: tuple[str, ...],
                            source_mission_refs: tuple[str, ...]) -> WorkflowLearningCandidate:
    rule = WorkflowRule(intent=intent, statement=statement, provenance=RuleProvenance.INFERRED,
                        scope=pattern.scope, gate=gate, evidence_refs=_evidence_refs(pattern),
                        confidence=pattern.confidence)
    return WorkflowLearningCandidate(
        project_id=project_id, workflow_id=workflow.workflow_id, proposed_rule=rule, scope=pattern.scope,
        source_decision_refs=source_decision_refs, source_mission_refs=source_mission_refs,
        supporting=pattern.support, counterexamples=pattern.contradictions,
        confidence=pattern.confidence, rationale=pattern.proposition)


def propose_from_outcomes(experiences: list[Experience], workflow: WorkflowDefinition, *,
                          gate: HumanGate = HumanGate.G0_NONE) -> list[WorkflowLearningCandidate]:
    """§20: mine outcome experiences (via the engine) into per-``(scope, action)`` candidates — "this action
    in this context tends to succeed". Confidence is the engine's; every result is a PROPOSED candidate that
    still has to clear :func:`accept`."""
    out: list[WorkflowLearningCandidate] = []
    for pat in mine(experiences, proposition="tends to a good outcome"):
        out.append(_candidate_from_pattern(
            workflow.project_id, workflow, pat,
            intent=pat.scope, statement=f"In {pat.scope}, prefer: {pat.supporting[0].action}"
            if pat.supporting else pat.proposition, gate=gate,
            source_decision_refs=tuple(d.source_ref for e in pat.supporting for d in e.evidence),
            source_mission_refs=()))
    return out


def propose_from_choices(experiences: list[Experience], workflow: WorkflowDefinition, *,
                         gate: HumanGate = HumanGate.G0_NONE) -> list[WorkflowLearningCandidate]:
    """§9: mine repeated *decisions* into "prefer action A in this context" candidates. Within a context
    signature, each observed action is supported by the times it was chosen and contradicted by the times a
    different action was chosen in the same context — so a genuinely repeated choice earns confidence while a
    one-off does not. Groups and members keep input order (deterministic)."""
    order: list[str] = []
    by_scope: dict[str, list[Experience]] = {}
    for e in experiences:
        if e.context_signature not in by_scope:
            by_scope[e.context_signature] = []
            order.append(e.context_signature)
        by_scope[e.context_signature].append(e)

    out: list[WorkflowLearningCandidate] = []
    for scope in order:
        exps = by_scope[scope]
        actions: list[str] = []
        for e in exps:                                   # candidate actions, first-seen order
            if e.action not in actions:
                actions.append(e.action)
        for action in actions:
            supporting = tuple(e for e in exps if e.action == action)
            counter = tuple(e for e in exps if e.action != action)
            pat = Pattern(scope=scope, proposition=f"prefer '{action}'",
                          supporting=supporting, counterexamples=counter)
            out.append(_candidate_from_pattern(
                workflow.project_id, workflow, pat, intent=scope,
                statement=f"When {scope}, prefer to {action}.", gate=gate,
                source_decision_refs=tuple(d.source_ref for e in supporting for d in e.evidence),
                source_mission_refs=()))
    return out


# ──────────────────────────── the gate + amendment ────────────────────────────

def _meets(candidate: WorkflowLearningCandidate, policy: PromotionPolicy) -> bool:
    """Mirror of ``discovery_runtime.learn.eligible`` over the candidate's stored counts + engine confidence
    (kept here so the Projects candidate stays serializable — it holds numbers, not the engine handle)."""
    total = candidate.supporting + candidate.counterexamples
    rate = candidate.counterexamples / total if total else 0.0
    return (candidate.supporting >= policy.min_support
            and candidate.confidence >= policy.min_confidence
            and rate <= policy.max_contradiction_rate)


def accept(workflow: WorkflowDefinition, candidate: WorkflowLearningCandidate, *, reviewer: str,
           policy: PromotionPolicy = DEFAULT_POLICY, policy_forbids=None
           ) -> tuple[WorkflowDefinition | None, WorkflowLearningCandidate]:
    """The gate. Returns ``(new_version, updated_candidate)``.

    Governance first (§18): if ``policy_forbids(candidate)`` is true the candidate is REJECTED and no version
    is produced — policy beats learned behavior, and the predicate is the Governance plane's, not
    model-visible context. Then the engine's eligibility must hold *and* a human reviewer must accept (both;
    this is the "a correction is not a rule" invariant). On success a **new** WorkflowDefinition version is
    minted — the active one is never mutated — carrying the now ``HUMAN_CONFIRMED`` rule, with
    ``parent_version_id`` set and ``status=READY_FOR_REVIEW`` (a material amendment is shadow-tested before
    activation, §13/§15)."""
    if policy_forbids is not None and policy_forbids(candidate):
        return None, replace(candidate, status=CandidateStatus.REJECTED,
                             rationale=(candidate.rationale + " · rejected by policy").strip(" ·"))
    if not _meets(candidate, policy):
        return None, replace(candidate, status=CandidateStatus.NEEDS_MORE_EVIDENCE)

    confirmed = replace(candidate.proposed_rule, provenance=RuleProvenance.HUMAN_CONFIRMED)
    learned_from = dict(workflow.learned_from)
    learned_from.setdefault("decision_refs", [])
    learned_from["decision_refs"] = list(learned_from["decision_refs"]) + list(candidate.source_decision_refs)
    new_def = WorkflowDefinition(
        project_id=workflow.project_id, title=workflow.title, description=workflow.description,
        app_ids=workflow.app_ids, trigger=workflow.trigger, rules=workflow.rules + (confirmed,),
        human_gates=workflow.human_gates, unresolved_questions=workflow.unresolved_questions,
        learned_from=learned_from, status=WorkflowStatus.READY_FOR_REVIEW,
        version=workflow.version + 1, parent_version_id=workflow.workflow_id)
    updated = replace(candidate, status=CandidateStatus.ACCEPTED,
                      resulting_workflow_version=new_def.workflow_id,
                      proposed_rule=confirmed)
    return new_def, updated


def activate(workflow: WorkflowDefinition, *, actor: str, allow_unknowns: bool = False) -> WorkflowDefinition:
    """Promote a reviewed/shadow-tested version to ACTIVE (§7/§14). Refuses while material UNKNOWNs remain
    unless policy explicitly permits abstention there (``allow_unknowns``). Returns a new ACTIVE version;
    supersede the parent separately via :func:`supersede`."""
    if workflow.has_blocking_unknowns and not allow_unknowns:
        raise ValueError(f"workflow {workflow.workflow_id} has unresolved material questions and cannot "
                         f"activate: {list(workflow.unresolved_questions)}")
    return replace(workflow, status=WorkflowStatus.ACTIVE, updated_at=_now())


def supersede(workflow: WorkflowDefinition) -> WorkflowDefinition:
    """Mark a previously-active version SUPERSEDED once a successor is active. Never called on the successor."""
    return replace(workflow, status=WorkflowStatus.SUPERSEDED, updated_at=_now())
