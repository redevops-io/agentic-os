"""Mission Planner — the LOGICAL layer (SQL -> logical plan).

Goal + world/belief state + discovered capabilities -> an ExecutionIntent (WHAT outcomes are
needed, in what dependency shape), NOT nodes and NOT apps.

**The invariant, restated.** This used to read "the one place the LLM runs". That was true
while Mission was the top of the stack and it stops being true once a Discovery Runtime sits
above it: models interpret and discover upstream, and Mission is handed meaning that is already
closed. Counting model calls was always a proxy for the property that actually matters, which is

    no LLM may change mission semantics after a VerifiedIntent is sealed.

Below a sealed intent, everything is deterministic — compile, schedule, execute. Mission may
one day use a model for a genuinely hard *planning* problem, and if it does, that model's output
is an execution proposal constrained by the verified intent and the capability manifest. It is
never a re-reading of what the user wanted. The difference is the whole boundary: Mission may
answer EXECUTABLE, UNSUPPORTED_CAPABILITY, NEEDS_APPROVAL, POLICY_DENIED or BUDGET_EXCEEDED, and
may never answer "I could not do what you meant, so I interpreted it as something else".

Production wires a `ThinkModel` (RedHatAI/Qwen3-Coder-Next-NVFP4, thinking mode, self-hosted).
The default `TemplatePlanner` matches the goal to a Mission Template and needs no model, so the
kernel is runnable and testable offline. A `ModelPlanner` calls an injected model and parses the
intent JSON — same output type, so the rest of the kernel doesn't care which planner ran.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from . import templates
from .types import ExecutionIntent, IntentStep


class ThinkModel(Protocol):
    def complete(self, prompt: str) -> str: ...


class Planner(Protocol):
    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent: ...


class TemplatePlanner:
    """Match the goal to a template; fall back to a single-outcome intent. No model needed."""

    _KEYWORDS = {
        "onboarding": ["onboard", "new customer", "sign up", "signup", "welcome"],
        "invoice_recovery": ["overdue", "unpaid", "invoice", "dunning", "collect payment", "recover payment"],
    }

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        template = (context or {}).get("template") or self._match(goal)
        if template:
            intent = templates.get(template, mission_id)
            if intent:
                # Name the choice on the way out. A caller that recorded only
                # its own argument recorded `None` here, leaving the goal
                # string as the sole record of which template ran.
                intent.template = template
                return intent
        # generic fallback: one outcome named after the goal
        return ExecutionIntent(
            mission_id=mission_id, rationale="generic single-outcome plan",
            steps=[IntentStep(outcome=_slug(goal), need=goal, value_hint="medium")],
        )

    def _match(self, goal: str) -> str | None:
        g = (goal or "").lower()
        for name, kws in self._KEYWORDS.items():
            if any(k in g for k in kws):
                return name
        return None


class ModelPlanner:
    """Ask an injected thinking model for the intent as JSON, then validate into ExecutionIntent.
    Falls back to TemplatePlanner if the model output can't be parsed (planning must not crash)."""

    _PROMPT = (
        "You are the Mission Planner. Decompose the GOAL into a LOGICAL plan of outcomes — do NOT "
        "name apps or tools, only the outcomes that must be achieved and their dependencies.\n"
        "Return STRICT JSON: {\"steps\":[{\"outcome\":str,\"need\":str,\"inputs_from\":[str],"
        "\"constraints\":[str],\"value_hint\":\"low|medium|high\"}]}.\n\nGOAL: {goal}\n"
        "Available capability needs (for grounding, do not copy verbatim): {caps}\n"
    )

    def __init__(self, model: ThinkModel, fallback: Planner | None = None):
        self.model = model
        self.fallback = fallback or TemplatePlanner()

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        caps = ", ".join((context or {}).get("capability_hints", []))
        try:
            from .models import strip_thinking
            raw = self.model.complete(self._PROMPT.replace("{goal}", goal).replace("{caps}", caps))
            data = json.loads(_extract_json(strip_thinking(raw)))
            steps = [IntentStep(outcome=s["outcome"], need=s.get("need", s["outcome"]),
                                inputs_from=s.get("inputs_from", []), constraints=s.get("constraints", []),
                                value_hint=s.get("value_hint", "medium")) for s in data["steps"]]
            if not steps:
                raise ValueError("empty plan")
            return ExecutionIntent(mission_id=mission_id, rationale="model plan", steps=steps)
        except Exception:
            return self.fallback.plan(mission_id, goal, context)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "outcome").lower()).strip("_")[:40] or "outcome"


def _extract_json(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text
