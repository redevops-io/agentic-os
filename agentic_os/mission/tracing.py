"""Mission-native tracing (opt-in) — the Mission is the root of one causal trace tree.

The Mission mints its root trace; each plan node is a child span; each capability invocation is a child of
its node; and a deployment substrate (Argo, K8s, Terraform, an app's spans) is told — via a propagated
``traceparent`` + baggage — to nest *underneath* the causing node's span. `spans()` projects the mission's
own ``SecurityTrajectory`` (the boundary-emitted event stream) into the nested OTel-shaped span tree.

This is the native semantic record. A telemetry plugin turns these span dicts into real OTLP and exports
them; a deployment adapter (e.g. Argo) uses ``substrate_context`` to make its workflow spans children of a
Mission node. Requires runtime-contracts; imported lazily so tracing stays opt-in.
"""
from __future__ import annotations

from typing import Any


class MissionTrace:
    def __init__(self, mission_id: str, *, intent_id: str = ""):
        from runtime_contracts import TraceContext   # noqa: PLC0415 — lazy: tracing is opt-in
        self.root = TraceContext.root(mission_id, intent_id=intent_id)
        self._nodes: dict[str, Any] = {}

    def node(self, node_id: str):
        """The span for a plan node (memoized, so a node has one stable span across its invocations)."""
        ctx = self._nodes.get(node_id)
        if ctx is None:
            ctx = self.root.child(node_id=node_id)
            self._nodes[node_id] = ctx
        return ctx

    def capability_span(self, node_id: str, capability: str, *, step: str = ""):
        """A span for one capability invocation under a node."""
        return self.node(node_id).child(capability=capability, step=step)

    def substrate_context(self, node_id: str, *, capability: str = ""):
        """The child context to hand a deployment substrate so ITS spans nest under this Mission node.
        Returns the TraceContext; the adapter propagates ``.traceparent()`` + ``.baggage()`` into the
        substrate (Argo controller/workload, a downstream OTel collector, …). The substrate can only ever
        become a child here — it never carries a trace_id of its own up into the Mission."""
        return self.capability_span(node_id, capability or "substrate", step="substrate")

    def spans(self, trajectory) -> list[dict]:
        """Project a ``SecurityTrajectory`` into the nested OTel span tree rooted at this Mission. Each
        event's span parents to the span of its causal parent event when present, else to a per-capability
        node span under the Mission root — so the causal event stream becomes a proper trace tree."""
        from runtime_contracts import span_of                     # noqa: PLC0415
        from runtime_contracts.protocol.telemetry import causal_order   # noqa: PLC0415 — disambiguate:
        # models.investigation also exports a causal_order (over transitions); we need the telemetry one.
        ordered = causal_order(list(getattr(trajectory, "events", ())))
        ctx_by_event: dict[str, Any] = {}
        out: list[dict] = []
        for e in ordered:
            parent_ctx = ctx_by_event.get(e.parent_event_id) if e.parent_event_id else None
            base = parent_ctx or self.node(e.capability or "node")
            ctx = base.child(capability=e.capability, step=str(e.sequence))
            ctx_by_event[e.event_id] = ctx
            out.append(span_of(e, ctx))
        return out
