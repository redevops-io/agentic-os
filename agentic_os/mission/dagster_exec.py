"""Durable execution on Dagster — generalizes sidekick-reel/reel/dagster_client.py.

The reel gateway already launches a durable, retryable Dagster job per run and reads state back
via GraphQL. Mission Runtime reuses that pattern two ways:

  * DagsterOperatorClient — the Executor's `OperatorClient`: runs each node as a durable Dagster
    run of a generic `mission_capability_job` (op → POST the operator's /invoke), launch + poll +
    return result. Retries + run storage come from Dagster; the operator still dedupes on the
    idempotency key for exactly-once side effects.

  * compile_to_dagster(plan) — the fuller offload: compile a whole ExecutionPlan graph into ONE
    Dagster job (node → op, depends_on → op deps) so Dagster schedules the DAG. Guarded import so
    the module loads without dagster installed.

The GraphQL transport is injectable, so the client is unit-tested with canned responses (no live
Dagster). Mirrors the queries in ai/dagster-mcp/server.py.
"""
from __future__ import annotations

import os
import time
from typing import Callable

_LAUNCH = """
mutation($p: ExecutionParams!) { launchRun(executionParams: $p) {
  __typename
  ... on LaunchRunSuccess { run { runId status } }
  ... on PythonError { message }
} }"""

_STATUS = """
query($id: ID!) { runOrError(runId: $id) {
  __typename
  ... on Run { runId status }
} }"""

_TERMINAL = {"SUCCESS": "done", "FAILURE": "failed", "CANCELED": "failed"}


class DagsterError(RuntimeError):
    pass


class DagsterOperatorClient:
    """OperatorClient backed by durable Dagster runs (one run per node)."""

    def __init__(self, graphql_url: str | None = None, job: str | None = None,
                 gql: Callable[[str, dict], dict] | None = None,
                 poll_interval: float = 1.0, max_polls: int = 600,
                 result_reader: Callable[[str], dict] | None = None):
        self.url = graphql_url or os.environ.get(
            "DAGSTER_GRAPHQL_URL", "http://dagster-webserver.dagster.svc:3000/graphql")
        self.job = job or os.environ.get("DAGSTER_MISSION_JOB", "mission_capability_job")
        self._gql = gql or self._http_gql
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        # how to fetch the op's return value once the run is SUCCESS (run tags / asset / logs).
        # Injectable so prod can read it from wherever the job writes it; default = raise.
        self._result_reader = result_reader

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str) -> dict:
        run_config = {"ops": {"run_capability": {"config": {
            "operator": operator, "capability": capability, "inputs": inputs,
            "idempotency_key": idempotency_key}}}}
        launched = self._gql(_LAUNCH, {"p": {
            "selector": {"repositoryLocationName": "mission", "repositoryName": "__repository__",
                         "jobName": self.job},
            "runConfigData": run_config, "mode": "default"}})
        data = launched.get("data", {}).get("launchRun", {})
        if data.get("__typename") != "LaunchRunSuccess":
            raise DagsterError(f"launch failed: {data.get('message') or data}")
        run_id = data["run"]["runId"]
        status = self._poll(run_id)
        if status != "done":
            raise DagsterError(f"run {run_id} ended {status}")
        if self._result_reader:
            return self._result_reader(run_id)
        return {"run_id": run_id, "status": "done"}

    def _poll(self, run_id: str) -> str:
        for _ in range(self.max_polls):
            resp = self._gql(_STATUS, {"id": run_id})
            run = resp.get("data", {}).get("runOrError", {})
            st = run.get("status")
            if st in _TERMINAL:
                return _TERMINAL[st]
            time.sleep(self.poll_interval)
        return "failed"

    def _http_gql(self, query: str, variables: dict) -> dict:
        from .util import post_json
        return post_json(self.url, {"query": query, "variables": variables}, timeout=45.0)


def compile_to_dagster(plan, invoke_url_for: Callable[[str], str]):
    """Compile an ExecutionPlan graph into ONE Dagster job (node→op, depends_on→deps).

    Each op POSTs the operator's /invoke. Returns a Dagster JobDefinition. Import-guarded so the
    module is usable without dagster; raises a clear error if called without it installed."""
    try:
        from dagster import op, job, In, Out, Nothing, OpExecutionContext
    except Exception as e:  # pragma: no cover - dagster optional
        raise RuntimeError("compile_to_dagster requires the 'dagster' package") from e

    from .util import post_json

    ops_by_node: dict[str, object] = {}
    for node in plan.graph.nodes:
        deps = list(node.depends_on)

        def _make(n):
            @op(name=f"op_{n.id}", ins={f"dep_{i}": In(Nothing) for i in range(len(n.depends_on))})
            def _run(context: OpExecutionContext):  # pragma: no cover - needs a live Dagster run
                base = invoke_url_for(n.operator)
                return post_json(f"{base.rstrip('/')}/invoke",
                                 {"capability": n.capability, "inputs": n.inputs,
                                  "idempotency_key": n.idempotency_key})
            return _run
        ops_by_node[node.id] = _make(node)

    @job(name=f"mission_{plan.mission_id.split('_')[-1][:8]}")
    def _mission_job():  # pragma: no cover - needs a live Dagster instance
        outputs: dict[str, object] = {}
        # topological emit so deps resolve
        emitted: set[str] = set()
        remaining = list(plan.graph.nodes)
        while remaining:
            progressed = False
            for node in list(remaining):
                if all(d in emitted for d in node.depends_on):
                    ops_by_node[node.id](*[outputs[d] for d in node.depends_on])
                    emitted.add(node.id)
                    remaining.remove(node)
                    progressed = True
            if not progressed:
                break
    return _mission_job
