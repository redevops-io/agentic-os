"""Canonical concurrency safety semantics (plan §7–§13, §22): the scheduler parallelizes the maximal
SAFE ready-set and serializes resource conflicts, with an auditable reason for each.

These drive the real `TopoScheduler` directly (constructing the physical graph the compiler would emit),
so they lock the safety rules independent of any planner/model. 2B = bounded provider fan-out, 2C =
conflicting shared resource (static key AND input-derived key), plus read-only non-conflict and the
EXPLAIN surface.
"""
from __future__ import annotations

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node
from agentic_os.mission import concurrency as conc


def _node(nid: str, *, keys=None, mode="", key_tmpl="", inputs=None, max_par=None) -> Node:
    n = Node(capability=f"cap.{nid}", operator="op",
             resource_keys=list(keys or []), concurrency_mode=mode,
             concurrency_key=key_tmpl, inputs=dict(inputs or {}), max_parallelism=max_par)
    n.id = nid
    return n


def _graph(*nodes: Node) -> ExecutionGraph:
    return ExecutionGraph(nodes=list(nodes))


SCHED = TopoScheduler()
WIDE = SchedulePolicy(max_concurrency=8)   # cap wide enough that ONLY resource conflicts bind


# ── 2C: conflicting shared resource → selective serialization (static key) ──────────────────────────────

def test_2c_same_exclusive_key_serializes_one_at_a_time():
    """Two side-effecting nodes on the SAME resource key must not co-release; a third on a DIFFERENT key
    runs alongside. This is the plan's 'deployment conflicts by cluster/namespace' (§8)."""
    a = _node("A", keys=["k8s:cluster:prod"], mode="side_effecting")
    b = _node("B", keys=["k8s:cluster:prod"], mode="side_effecting")
    c = _node("C", keys=["k8s:cluster:staging"], mode="side_effecting")
    released = {n.id for n in SCHED.ready(_graph(a, b, c), done=set(), running=set(), policy=WIDE)}
    # exactly one of the prod-conflicting pair, plus the staging node
    assert "C" in released
    assert len({"A", "B"} & released) == 1, f"both prod writers released together: {released}"


def test_2c_serialized_node_runs_once_the_holder_is_done():
    """The held conflicting node is released on the NEXT wave, once its sibling has committed — selective
    serialization, not starvation."""
    a = _node("A", keys=["k8s:cluster:prod"], mode="side_effecting")
    b = _node("B", keys=["k8s:cluster:prod"], mode="side_effecting")
    g = _graph(a, b)
    wave1 = {n.id for n in SCHED.ready(g, done=set(), running=set(), policy=WIDE)}
    first = next(iter(wave1))
    wave2 = {n.id for n in SCHED.ready(g, done={first}, running=set(), policy=WIDE)}
    assert wave1 | wave2 == {"A", "B"} and wave1 != wave2


# ── 2C: input-derived key — same cap, different account runs; same account serializes ───────────────────

def test_2c_input_derived_different_accounts_parallelize():
    a = _node("A", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={"account_id": 123})
    b = _node("B", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={"account_id": 456})
    released = {n.id for n in SCHED.ready(_graph(a, b), done=set(), running=set(), policy=WIDE)}
    assert released == {"A", "B"}, "writes to DIFFERENT accounts must run together"


def test_2c_input_derived_same_account_serializes():
    a = _node("A", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={"account_id": 123})
    b = _node("B", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={"account_id": 123})
    released = {n.id for n in SCHED.ready(_graph(a, b), done=set(), running=set(), policy=WIDE)}
    assert len(released) == 1, "two writes to the SAME account must serialize"


def test_2c_unresolved_template_serializes_conservatively():
    """If the key can't be resolved yet (input only materialises from world state at run time), two nodes
    sharing the same template still serialize — safer to over-serialize than to miss a real conflict."""
    a = _node("A", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={})
    b = _node("B", key_tmpl="crm:account:{account_id}", mode="side_effecting", inputs={})
    released = SCHED.ready(_graph(a, b), done=set(), running=set(), policy=WIDE)
    assert len(released) == 1


# ── 2B: bounded provider fan-out ────────────────────────────────────────────────────────────────────────

def test_2b_bounded_provider_fanout_caps_concurrency():
    """Five renders share one rate-limited provider (max_parallelism=2). The global cap is 8, but the
    provider key binds the wave to 2."""
    nodes = [_node(f"R{i}", keys=["provider:seedance"], mode="side_effecting", max_par=2) for i in range(5)]
    released = SCHED.ready(_graph(*nodes), done=set(), running=set(), policy=WIDE)
    assert len(released) == 2, f"provider fan-out not bounded to 2: {[n.id for n in released]}"


# ── read-only work never conflicts ──────────────────────────────────────────────────────────────────────

def test_read_only_nodes_do_not_lock_even_on_a_shared_key():
    """Two searches over the same corpus (read_only) run together — reads don't block reads."""
    a = _node("A", keys=["corpus:main"], mode="read_only")
    b = _node("B", keys=["corpus:main"], mode="read_only")
    released = {n.id for n in SCHED.ready(_graph(a, b), done=set(), running=set(), policy=WIDE)}
    assert released == {"A", "B"}
    assert conc.conflict(a, {"corpus:main": 5}) is None   # never conflicts regardless of holders


# ── unclassified capabilities are unchanged (additive) ──────────────────────────────────────────────────

def test_capabilities_that_declare_nothing_are_unchanged():
    """A node with no concurrency metadata holds no lock — today's behaviour, bounded only by the cap."""
    nodes = [_node(f"N{i}") for i in range(4)]
    released = SCHED.ready(_graph(*nodes), done=set(), running=set(), policy=SchedulePolicy(max_concurrency=8))
    assert len(released) == 4


# ── EXPLAIN surface (plan §22) ──────────────────────────────────────────────────────────────────────────

def test_explain_gives_an_auditable_reason_per_node():
    a = _node("A", keys=["k8s:cluster:prod"], mode="side_effecting")
    b = _node("B", keys=["k8s:cluster:prod"], mode="side_effecting")
    rows = SCHED.explain(_graph(a, b), done=set(), running=set(), policy=WIDE)
    by = {r["node"]: r for r in rows}
    decisions = {r["decision"] for r in rows}
    assert decisions == {"parallelized", "serialized"}
    serialized = next(r for r in rows if r["decision"] == "serialized")
    assert "k8s:cluster:prod" in serialized["reason"]        # names the conflicting key
    assert by["A"]["resource_keys"] == ["k8s:cluster:prod"]
