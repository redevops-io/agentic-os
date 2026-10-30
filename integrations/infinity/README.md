# agentic-os-infinity

An optional bridge between the [Infinity](https://github.com/hydro-project/infinity) agent runtime and the ReDevOps Agent Gateway. It is **not part of the
base install**: `pip install agentic-os` does not include it, import it or depend on it. Design record: [docs/INFINITY_RAP_BRIDGE_ADR.md](../../docs/INFINITY_RAP_BRIDGE_ADR.md).

## Purpose

Infinity decides when an agent runs: it wakes a thread on a message, lets it hibernate while a tool works, and wakes it again when the result arrives. It talks to tools
over the Reactive Agent Protocol (RAP): the runtime POSTs an invocation, the tool answers HTTP 200 at once, and later POSTs the result to a callback URL.
This package is a RAP tool server. Every invocation it accepts crosses one door into the effect, `AgentGateway.invoke`, so the call gets the same permission check,
authority envelope, budget, approval, idempotency and audit as any other agent request. The native ReDevOps execution is unchanged and stays the default.

## What is here

| Module | What it does |
|---|---|
| `contracts` | RAP wire messages built from the specification, statuses, reason codes |
| `operation_binding` | the closed set of operations; each binds to one gateway capability; bounded argument schemas; deterministic action ids |
| `callback_auth` | `RAP-HMAC-SHA256-V1` request and callback signing, peers, nonce cache, rate limiter, callback-URL (SSRF) policy and pinned delivery |
| `store` | the durable pending-action journal and transactional outbox (SQLite) |
| `rap_gateway` | `RapGateway`: accept, dispatch once, recover after a crash, deliver results at least once, cancel, close, subscribe, reconcile |
| `http_server` | the four endpoints, deny by default |
| `lineage` | Infinity thread lineage mapped onto the kernel's `ChildSpawner`: attenuated, time-bounded child authority |
| `event_mapping`, `receipt_adapter` | action records to RAP messages; a completed write needs a downstream receipt |
| `lifecycle_backend` | the `AgentLifecycleExecutor` contract (declared, not implemented) |
| `demo` | a mock CRM, three governed capabilities, an emulated RAP runtime, a runnable scenario |
| `harness/` | a Rust program that embeds the real Infinity runtime (`infinity-agent-core` 0.2.0) with a scripted model |

## Install

agentic-os is not on PyPI; install it from a release tag first, then this package. In a checkout of this repository:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
pip install -e integrations/infinity
```

To install exactly the versions that were tested (hashes recorded in `uv.lock`):

```bash
cd integrations/infinity
pip install uv
uv sync --frozen --extra dev
```

## Run

```bash
python -m redevops_infinity.demo.run                    # the synthetic scenario against an emulated RAP runtime, loopback only
cd integrations/infinity && pytest -q tests             # the suite (APPROVAL_SECRET unset)
python scripts/prove_tests_bite.py                      # removes each protection in a scratch copy and requires its tests to fail
python scripts/list_licenses.py                         # declared licences of everything installed
```

The real Infinity runtime test needs a Rust toolchain:

```bash
cd integrations/infinity/harness && cargo build --release --locked
cd .. && RAP_HARNESS_BIN=harness/target/release/infinity-rap-harness pytest -q -m sidecar tests/test_infinity_sidecar.py
```

## Deploying

The bridge is a service you run next to your agent runtime. Configure, in code: the operations (`OperationSpec`), the gateway registry and authority guard,
one `Peer` per runtime (key id, secret from your secret manager, tenant, principal, callback origins), and a durable `EventStore`. Serve it with
`RapHttpServer(bridge, host=..., port=...)` behind TLS. Point the runtime at the base URL; its HTTP client must sign requests as described in
`callback_auth.sign_request`, and its callback receiver should verify signatures with `CallbackVerifier`.

## Limits

Read the ADR before using this near real systems. In short: one process owns the outbox; the approvals inbox and the idempotency store of the gateway are
process-local; the stock Infinity local clients send no credentials, so a runtime needs the signing `HttpClient` shown in `harness/`; the demo and the
tests use a mock CRM and a scripted model; no AWS, Bedrock or Lambda deployment is included or tested.

## Licence

The licence files of the repository are copied here (`LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`): this package imports agentic-os.
Infinity is Apache-2.0; its dependencies keep their own licences (see the ADR).
