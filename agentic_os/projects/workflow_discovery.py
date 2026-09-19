"""Workflow discovery — demonstration → WorkflowCandidate (Workflow-Teaching plan §5–§9, Phases 1–3).

The other input to workflow teaching. `workflow_learning` improves an existing workflow from experience;
this *acquires* the initial workflow by watching an operator do the work once. The user-facing loop is
SHOW → REVIEW → RUN; this module is the REVIEW-producing middle: it turns an immutable
:class:`DemonstrationSession` (recorded by the UI Agent) into an inspectable :class:`WorkflowCandidate`.

The load-bearing principle (§3) is *a demonstration is evidence, not executable policy*. So the pipeline
does not "record once, run forever". It:

  1. **segments** raw observations into semantic operations (consecutive actions on the same intent/app
     collapse into one step — the recording is clicks, the workflow is meaning, §5);
  2. **lifts** each segment to a :class:`WorkflowStep`, labelled ``OBSERVED`` when it rests on a browser/
     network action and ``INFERRED`` when the pipeline supplied it;
  3. **surfaces hidden decisions** (§9) — a SELECTION with alternatives, or an explicit DECISION, becomes
     a candidate ``decision_rule`` (INFERRED) plus an unresolved question, never a silently-learned rule;
  4. **resolves capabilities** (§8) — a demonstrated UI interaction maps to a provider API when the
     capability registry has one, with the governed UI Agent as fallback; unresolved intents become
     ``unsupported_steps`` marked ``UNKNOWN`` (demonstration teaches intent, not the execution transport);
  5. **proposes human gates** — an external write/submit becomes a G4 gate, a commitment a G5 (observation
     does not authorize future repetition, §18.3);
  6. **records what it does not know** — no observed completion, an unresolved decision, or an unmatched
     capability each become an explicit assumption / ambiguity / unresolved question (§7/§14).

Pure and deterministic: no model calls, no clocks in the logic. :func:`candidate_to_definition` bridges to
the existing :class:`WorkflowDefinition` — always as a DRAFT (or NEEDS_CLARIFICATION when questions remain),
never ACTIVE, so it flows into the same review/shadow/activate gate as everything else.
"""
from __future__ import annotations

from .contracts import (
    DemonstrationSession,
    HumanGate,
    ObservationKind,
    RuleProvenance,
    WorkflowCandidate,
    WorkflowDefinition,
    WorkflowRule,
    WorkflowStatus,
    WorkflowStep,
)

#: A minimal capability registry: semantic intent → (preferred capability id, fallback capability ids).
#: The real registry comes from the installed capabilities/connectors; this is the injection point (§8).
CapabilityRegistry = dict  # {intent: (preferred, (fallbacks...))}

#: Observation kinds that carry a real, external side effect — the ones that must be gated (§18.3).
_EXTERNAL_WRITE = {ObservationKind.SUBMIT, ObservationKind.WRITE, ObservationKind.UPLOAD}
#: Observation kinds that are pure evidence-gathering, folded into the step they precede.
_PASSIVE = {ObservationKind.NAVIGATION, ObservationKind.WAIT, ObservationKind.READ,
            ObservationKind.NETWORK}


def _external_comms(intent: str) -> bool:
    t = (intent or "").lower()
    return any(w in t for w in ("send", "email", "post", "publish", "message", "outreach", "tweet"))


def _commitment(intent: str) -> bool:
    t = (intent or "").lower()
    return any(w in t for w in ("price", "quote", "commit", "contract", "sign", "pay", "purchase"))


def _gate_for(intent: str) -> HumanGate:
    if _commitment(intent):
        return HumanGate.G5_COMMITMENT
    if _external_comms(intent):
        return HumanGate.G4_EXTERNAL_COMMS
    return HumanGate.G0_NONE


def discover_workflow(session: DemonstrationSession, *, capabilities: CapabilityRegistry | None = None,
                      trigger: str = "") -> WorkflowCandidate:
    """Lift a recording into an inspectable :class:`WorkflowCandidate`. Deterministic."""
    caps = capabilities or {}
    steps: list[WorkflowStep] = []
    assumptions: list[str] = []
    ambiguities: list[str] = []
    unsupported: list[str] = []
    questions: list[str] = []
    inputs: list[str] = []
    outputs: list[str] = []
    matches: list[str] = []
    evidence: list[str] = []

    # 1–2. segment consecutive observations sharing an intent (skipping passive evidence) into steps.
    cur_intent: str | None = None
    cur: list = []

    def _flush():
        nonlocal cur, cur_intent
        if not cur or cur_intent is None:
            cur, cur_intent = [], None
            return
        _emit_step(cur_intent, cur)
        cur, cur_intent = [], None

    def _emit_step(intent: str, obs_group: list):
        # provenance: OBSERVED when any observation in the group is a real browser/network action;
        # INFERRED when the group is only passive/derived signal.
        observed = any(o.kind not in _PASSIVE for o in obs_group)
        prov = RuleProvenance.OBSERVED if observed else RuleProvenance.INFERRED
        pref, fbs = "", ()
        if intent in caps:
            pref, fbs = caps[intent][0], tuple(caps[intent][1])
            matches.append(f"{intent} → {pref}")
        else:
            # no capability match: still executable via the governed UI Agent fallback, but flagged.
            fbs = ("ui_agent",)
            unsupported.append(intent)
            questions.append(f"No installed capability matches '{intent}' — use the UI Agent, or bind one?")
        # a hidden decision inside the group (§9)
        decision_rule = ""
        gate = _gate_for(intent)
        for o in obs_group:
            if o.kind in (ObservationKind.SELECTION, ObservationKind.DECISION) and o.alternatives:
                decision_rule = f"prefer '{o.target or o.value_ref}' over {list(o.alternatives)}"
                prov = RuleProvenance.INFERRED
                questions.append(
                    f"You chose '{o.target or o.value_ref}' over {list(o.alternatives)} for '{intent}'. "
                    "Apply always / only when exactly one exists / ask each time / no?")
            for f in o.fields_written:
                if f not in outputs:
                    outputs.append(f)
            for f in o.fields_read:
                if f not in inputs:
                    inputs.append(f)
            if o.api_ref and o.api_ref not in evidence:
                evidence.append(o.api_ref)
            for e in o.evidence_refs:
                if e not in evidence:
                    evidence.append(e)
        conf = 0.9 if observed and intent in caps else (0.6 if observed else 0.4)
        steps.append(WorkflowStep(
            intent=intent, description=obs_group[0].target or intent, preferred_capability=pref,
            fallback_capabilities=fbs, decision_rule=decision_rule, human_gate=gate, provenance=prov,
            observation_refs=tuple(o.obs_id for o in obs_group), confidence=conf))

    for o in sorted(session.observations, key=lambda x: x.seq):
        if o.kind in _PASSIVE and cur:                 # fold passive evidence into the running step
            cur.append(o)
            continue
        key = o.intent or o.target or o.kind.value
        if key != cur_intent:
            _flush()
            cur_intent = key
        cur.append(o)
    _flush()

    # 5. gates the candidate believes it needs.
    gates = tuple(dict.fromkeys(s.human_gate for s in steps if s.human_gate is not HumanGate.G0_NONE))

    # 6. completion: if nothing marks a RESULT/terminal outcome, we cannot invent one (§7 test).
    if not any(o.kind is ObservationKind.RESULT for o in session.observations):
        questions.append("No completion condition was observed — when is this workflow done?")
        assumptions.append("completion condition not observed; must be supplied before activation")

    if not gates and any(_external_comms(s.intent) or _commitment(s.intent) for s in steps):
        ambiguities.append("an external action was observed but no approval point was inferred")

    conf_by = {s.intent: s.confidence for s in steps}
    conf_by["overall"] = round(sum(s.confidence for s in steps) / len(steps), 3) if steps else 0.0

    return WorkflowCandidate(
        project_id=session.project_id, session_id=session.session_id,
        proposed_trigger=trigger or (session.title or "demonstrated workflow"),
        proposed_inputs=tuple(inputs), steps=tuple(steps), proposed_outputs=tuple(outputs),
        capability_matches=tuple(matches), proposed_human_gates=gates,
        assumptions=tuple(assumptions), ambiguities=tuple(ambiguities),
        unsupported_steps=tuple(dict.fromkeys(unsupported)), unresolved_questions=tuple(questions),
        evidence_refs=tuple(evidence), confidence_by_component=conf_by)


def candidate_to_definition(candidate: WorkflowCandidate, *, title: str = "",
                            description: str = "") -> WorkflowDefinition:
    """Bridge a reviewed candidate to a :class:`WorkflowDefinition` — always a DRAFT, or
    NEEDS_CLARIFICATION when unresolved material questions remain (§3/§14). Never ACTIVE: it enters the
    same review → shadow → activate gate as everything else, and its rules keep the candidate's provenance
    labels so the UI can show what is observed vs inferred vs unknown."""
    rules = tuple(
        WorkflowRule(intent=s.intent, statement=(s.decision_rule or s.description or s.intent),
                     provenance=s.provenance, scope=candidate.proposed_trigger, gate=s.human_gate,
                     evidence_refs=s.observation_refs, confidence=s.confidence)
        for s in candidate.steps)
    status = (WorkflowStatus.NEEDS_CLARIFICATION if candidate.unresolved_questions
              else WorkflowStatus.DRAFT)
    return WorkflowDefinition(
        project_id=candidate.project_id, title=title or candidate.proposed_trigger,
        description=description, app_ids=(), trigger=candidate.proposed_trigger, rules=rules,
        human_gates=candidate.proposed_human_gates, unresolved_questions=candidate.unresolved_questions,
        learned_from={"recording_refs": [candidate.session_id], "candidate_refs": [candidate.candidate_id]},
        status=status, version=1)
