# ADR: optional voice integration on Pipecat

Status: proposed (first milestone, P0). Scope: [integrations/voice-pipecat](../integrations/voice-pipecat/README.md). Date of the checks below: 2026-10-29, against `main` at `5342fc6` (tag v0.4.1; the first draft of this record was written against v0.4.0 at `794c943` and was revised for 0.4.1).

## Purpose

Let a voice framework run a phone-style conversation while every action the conversation asks for goes through the existing Governed Agent Gateway, so a spoken request gets the same permission check, authority limits, approval, idempotency and audit as any other agent request. The first milestone is deliberately narrow: one framework (Pipecat), one worked example (inbound lead qualification against a mock CRM), synthetic input only.

## Method and sources

Everything below about the repository was read from the code at the commit above. Everything about Pipecat was read from the published artifacts of one release, not from articles or summaries:

| Source | What was taken from it |
|---|---|
| PyPI JSON API for `pipecat-ai` (release list, upload times, `requires_dist`, `requires_python`, file hashes) | versions and dates, dependency list, Python range |
| the `pipecat_ai-1.12.0` wheel (sha256 `4cd3dc07…ed6d9`, matches PyPI) | the licence file, `License-Expression`, package layout, bundled data files, and the source of the APIs used |
| the repository tag `v1.12.0` on GitHub | the `LICENSE` file at that tag (same text as the wheel's) and the release notes |

A planning document for this work (`REDEVOPS_VOICE_AGENTS_FRAMEWORK_AUDIT_AND_IMPLEMENTATION_PLAN_2026-10-29.md`) was named in the request but was not on disk when this was written. The task brief was used instead. No statement in that plan about other frameworks, licences, vendor news or messaging channels was available, none was verified, and none is repeated here or in the package.

## Decision

1. **Pipecat first, behind a provider-neutral bridge.** `VoiceActionBridge` and its contracts do not import Pipecat. Only `agentic_os_voice.pipecat_adapter` and the demo do. A second framework would add a sibling adapter module and its own install profile; nothing for it is implemented now.
2. **A separate distribution, outside the base wheel.** The code lives in `integrations/voice-pipecat/` with its own `pyproject.toml` (`agentic-os-voice-pipecat`), its own `uv.lock`, and the import package `agentic_os_voice`. The root `pyproject.toml`, root `uv.lock` and everything under `agentic_os/` are unchanged by this work; the root wheel still contains only `agentic_os`. This follows the repository's existing pattern of optional capability behind a separate install (the `mcp` extra for FastMCP, the connector plugin), but as a separate project, because a root extra would put Pipecat's whole dependency closure into the root lock.
3. **The bridge adds no permission system.** It never grants anything. It narrows what can be asked (allowlist, schemas, session identity) and hands accepted requests to `AgentGateway.invoke`.
4. **Pipecat owns the conversation, ReDevOps owns the effect.** Turns, interruptions and (in the demo) the node-to-node flow belong to Pipecat and Pipecat Flows. Authorisation, approval, side effects, receipts and recovery belong to the gateway and the bridge. The Flows functions that touch accounts or systems are the bridge's tools, wrapped; Flows is not reimplemented.

## Where it plugs in: the extension points that were inspected

| Area | What exists (path) | Used by the voice bridge as |
|---|---|---|
| Agent Gateway | `agentic_os/agent_gateway/gateway.py` `AgentGateway.invoke`: visibility and permission, authority envelope, budget, approval gate, handler, egress, audit, idempotency replay | the single execution path; every voice action is a `GatewayRequest` with `protocol="voice"` |
| Capability registry | `registry.py` `CapabilityRegistry`, `CapabilityManifest` (permissions, scopes, `risk_tier`, `side_effecting`, `approval_policy`, `idempotent`) | the example capabilities are ordinary manifests; the gateway's `prepare_* / request_*` convention is followed; two manifests are reused from `write_capabilities.py` |
| Authorizer | `Authorizer(principal, permission)` seam on `AgentGateway` | unchanged; the demo supplies a revocable stand-in for the identity plane |
| Authority guard | `agentic_os/mission/authority.py` `AuthorityGuard` (`authorize`, and `check`, which since 0.4.1 answers "inside the authority now?" without moving a counter), `AuthorityEnvelope`, `Grant` (resource selectors, tier, call and rate limits, expiry, revocation), `SqliteLedger` leases (reserved, dispatched, settled, unknown) | the owner's limits for the voice agent; its lease states are how a crash or a lost reply is recovered without a second send; `check` is how the bridge revalidates before answering a repeat |
| Approvals | `approvals.py` `InboxApprovalStore` (single use, 15 minutes by default, bound to the exact request, requester cannot approve) | human approval of consequential actions; `ApprovalTap` only records which inbox item belongs to which action |
| Idempotency | gateway `idempotency_key` with the 0.4.1 store (`begin`/`complete`/`abandon`/`seed`, one key per tenant, bound to a fingerprint of workspace, subject, authority key, capability and canonical arguments; `idempotency_conflict` and `idempotency_in_progress`; a bounded store that keeps completed writes and refuses a new claim when full); `gateway.reconcile_call`, which seeds the store | the key is the action id (36 characters, within the 1 to 256 bound); unknown outcomes are reconciled through `reconcile_call` |
| Audit | `AuditSink` (in-memory by default) | unchanged; the bridge keeps its own durable journal in addition (below) |
| Mission runtime | `agentic_os/mission/` (`MissionRuntime`, `EventStore` with hash chaining, `mission_receipt`) and the gateway's `MissionPort` / `missions.delegate_goal` | the journal reuses `EventStore` (durable JSONL, fsync, hash chain, `verify_chain`) and its `CrashingStore` test pattern; delegating a long task to a mission is a future capability, not part of P0 |
| Operator seam | `agentic_os/mission/operator_sdk.py` (`/invoke`, Idempotency-Key cache), `approval_gate.gated`, `HTTPOperatorClient` | not used in P0: the example handlers call a port directly. A real CRM operator would sit behind a gateway handler or a mission node |
| Tool classification | `agentic_os/tool_actions.py` `TOOL_ACTIONS` (read, write, delete per in-repo app tool, trusted registration metadata) | not extended: it classifies the apps' own tools for the permissions plane. Voice tools are gateway capabilities, classified by their manifest (`risk_tier`, `side_effecting`) and the owner's grants. Nothing under `agentic_os/` changes |
| Permissions plane | `agentic_os/permissions.py` | reached through the gateway's `authorize` callable, not called by the bridge |

## How `VoiceActionBridge` maps onto the gateway

| Requirement | Mechanism | Where |
|---|---|---|
| typed request: session, tenant, deterministic action id, allowlisted tool, bounded arguments, evidence references, confirmation state | `ActionRequest`; `ToolSpec` + `ArgSpec` schemas; `derive_action_id` | `contracts.py` |
| result: approved, denied, pending approval, completed, failed | `ActionStatus` (plus `outcome_unknown`, see Differences) | `contracts.py` |
| tenant and identity are not caller claims | `SessionBinding` is created by the host after its own authentication; a request's tenant must equal it; caller ID is stored (hashed) as metadata and never read for a decision; account tools need `VERIFIED` and the account in `verified_accounts` | `bridge.py` `prepare` |
| capability allowlist | the model can only name a `ToolSpec`; anything else is refused before the gateway and journaled (`not_allowlisted`) | `bridge.py` |
| bounded payload | per-field limits, a 2048-byte canonical-JSON cap, at most 16 arguments, 8 evidence references of 128 characters, 4096 bytes of tool output | `contracts.py`, `bridge.py` |
| no arbitrary code execution, no implicit admin | arguments are data (typed, pattern-checked, control characters refused); no tool maps to `sandbox.execute`; the principal is a service identity with exactly the owner's grants; `sandbox.execute` is not registered | `capabilities.py` |
| policy gate on the bounded write | an authority `Grant` with resource selectors (the voice agent may set `qualified` or `nurture`, nothing else) and call and rate limits; a value outside the selector is denied, not escalated | `capabilities.default_grants` |
| human approval for the consequential action | the send capability is registered with `ApprovalPolicy.MANDATORY`; a standing grant cannot satisfy it (gateway rule), so it parks in the inbox | `capabilities.OUTREACH_SEND` |
| action-level idempotency | the gateway idempotency key is the action id, derived from tenant, tool and the intent arguments only (not the session, not the principal), so it is stable per business intent; the gateway binds the key to the full scope and arguments and refuses a mismatch; the bridge answers a repeat from its journal without calling the gateway, but only after it has revalidated the principal (visibility and `AuthorityGuard.check`), so a replay does not outlive the right to the call | `bridge._run`, `bridge._entitled` |
| gateway answers other than success | `idempotency_conflict` becomes `failed` / `idempotency_conflict`, not retry safe (the key is held for something else; a person decides); `idempotency_in_progress` becomes `approved` / `idempotency_in_progress` (another call is running; nothing is sent); a store that cannot take a claim (`gateway_error` before any reservation) becomes `failed` / `gateway_error`, retry safe; a write whose output the egress policy holds becomes `approved` / `output_held` (it ran, its receipt is not released; asking again replays the held outcome and never re-sends); none of these is `completed` | `bridge._map` |
| durable receipts and correlation ids | every attempt gets `corr_…`, used as the gateway `request_id` and the authority lease id; the journal records submitted, decided, refused, replayed, reconciled; a receipt is the fold of an action's events and holds hashes, never argument values | `bridge.receipt` |
| completed only with a downstream receipt | for a side effect the output (or evidence) must carry `provider_object_id`; without it the result is `outcome_unknown` | `bridge._map` |
| cancellation never cancels a committed write; replay never duplicates | `submit_async` runs the work in its own task and shields it from the caller; before dispatch a cancel is honoured (`cancelled_before_dispatch`); after dispatch the work finishes and is journaled; a repeat gets the same receipt | `bridge.submit_async` |
| deny-by-default timeouts | every tool has a deadline (5 s read, 10 s write unless set, 60 s ceiling); on expiry a write is `approved` with reason `in_flight`, a read is `failed` with reason `timeout`; never `completed` | `bridge.submit_async` |
| crash at the write boundary | intent is journaled before dispatch; a restart folds `in flight` against the authority lease: never dispatched gives `failed` (safe to ask again); settled with a provider reference gives `completed`; dispatched or unknown gives `outcome_unknown` and no resend until `reconcile` | `bridge._effective_status`, `bridge.reconcile` |
| approval service unavailable | an exception in the approval store fails the call closed (gateway behaviour); the bridge reports `failed/approval_unavailable`, nothing is dispatched and no authority is left reserved | `ApprovalTap`, `bridge._map` |
| tool callbacks get only proxies | `ActionProxy` (slots, read-only, one session) is the only object the Pipecat handlers close over | `bridge.py`, `pipecat_adapter.py` |
| distress language | `DistressGuard` keeps matching transcripts from the model, speaks a fixed script that requests a hand-off and promises nothing, and latches the session so no further tool runs | `handoff.py`, `pipecat_adapter.py` |

## Pipecat facts that the design depends on (release 1.12.0, read from the wheel)

- Licence: the wheel carries `licenses/LICENSE` titled "BSD 2-Clause License", "Copyright (c) 2024–2026, Daily", and `METADATA` says `License-Expression: BSD-2-Clause`. The tag `v1.12.0` on GitHub has the same `LICENSE` text.
- `Requires-Python: >=3.11`.
- Always-installed dependencies (no extras): aiofiles, aiohttp, audioop-lts (3.13 and later), docstring_parser, loguru, loudness, Markdown, sentencex, numpy, Pillow, protobuf, pydantic, python-dotenv, resampy, soundfile, soxr, openai, typing_extensions, numba, wait_for2 (before 3.12), websockets, pyyaml, num2words, onnxruntime. Two of them (num2words, soxr) declare LGPL licences; see the integration page's licence table and the decisions below.
- Provider SDKs, transports (Daily, LiveKit, WebSocket server, local audio, SmallWebRTC) and servers are extras. This profile installs none.
- Two ONNX models ship inside the wheel as package data (`pipecat/audio/vad/data/silero_vad.onnx`, `pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx`) and are loaded from there; the code read does not download them. The default `UserTurnStrategies()` constructs the smart-turn analyzer, so the demo selects `ExternalUserTurnStrategies` and loads neither model.
- Pipecat Flows is part of `pipecat-ai` as `pipecat.flows` (`FlowManager`, `NodeConfig`, `FlowsFunctionSchema`); there is no separate package.
- Tool callbacks: `LLMService.register_function(name, handler, cancel_on_interruption=…, timeout_secs=…)`; the handler receives `FunctionCallParams` (`function_name`, `tool_call_id`, `arguments`, `result_callback`, `context`, `app_resources`, …). `register_function(None, handler)` is a catch-all.
- Interruption: by default (`cancel_on_interruption=True`) an interruption delivers `asyncio.CancelledError` into the running handler. With `False` the call is treated as asynchronous: the model continues, the call keeps running, and the result arrives later as a developer message. A timeout (`timeout_secs`) cancels the handler the same way. A result delivered after the call was settled is ignored. `on_function_calls_cancelled` reports cancelled calls.
- Test and local options: no Daily account or key is needed to run a pipeline. Frames can be queued into a `PipelineWorker`; `pipecat.tests.utils.run_test` ships in the wheel; the local audio transport needs PyAudio (extra `local`); WebSocket and WebRTC transports need extras and a listening port. P0 uses in-process frames and fake services only.
- Names used here are those of 1.12.0 (`PipelineWorker`, `WorkerRunner`, `LLMContextAggregatorPair`, `ExternalUserTurnStrategies`). A newer release (1.15.0, published 2026-10-29) ran the same suite and passed, with one deprecation warning (`aggregated_by` on text frames, deprecated since 1.13.0); the pin stays at the newest release that is more than two weeks old.

## Compatibility matrix

| Component | Pinned or tested | Notes |
|---|---|---|
| agentic-os | `main` at `5342fc6` (v0.4.1) | not on PyPI; install from the tag first. The package declares `agentic-os>=0.4.1`: the bridge uses `AuthorityGuard.check` and the 0.4.1 idempotency store semantics, and the suite checks them |
| Python | 3.11, 3.12 (hosted CI), 3.13 | 3.13 runs locally on macOS arm64; 3.11 to 3.13 run in the hosted workflow `voice-pipecat`. 3.11 and 3.14 passed locally against 0.4.0 before the rebase and were not repeated |
| pipecat-ai | `==1.12.0` (uploaded 2026-09-26, 33 days before the pin), no extras | 1.13.0 (10-19), 1.14.0 (10-24) and 1.15.0 (10-29) were less than two weeks old; 1.15.0 also passes the suite |
| Lock | `integrations/voice-pipecat/uv.lock`, 1756 recorded hashes | `uv sync --frozen --extra dev` installs it; CI runs the suite on it and `uv lock --check` |
| OS | macOS arm64 (local), ubuntu-latest (hosted) | not tested on Windows |

Base install, before and after: this work changes no file under `agentic_os/`, the root `pyproject.toml` or the root `uv.lock` (compared with v0.4.1); an environment with only the base package has 26 installed distributions (pip's own included) and 47 MB of site-packages; adding this package, Pipecat and the test tools gives 71 and 495 MB (45 more distributions: 38 for Pipecat and what it pulls in, 7 for this package and the test tools). `import agentic_os, agentic_os.agent_gateway` takes about 0.049 s in either environment (median of nine runs, both measured on the same machine). `tests/test_voice_pipecat_boundary.py` runs in the required job and fails if the base imports any of pipecat, the voice package, numba, onnxruntime or openai, or if the root dependency set mentions the voice packages.

## Differences from the illustrative contract in the brief

| Brief | Built | Why |
|---|---|---|
| five result statuses | six: `outcome_unknown` is added | after a lost reply or a crash at the write boundary the system cannot honestly say `failed` (the effect may exist) or `completed` (no receipt). The gateway already models this state; collapsing it into `failed` would invite a repeated effect |
| `approved` | means authorised and accepted but without a downstream receipt yet (in flight) | a human's approval is `pending_approval` resolving to `completed` through `resume` |
| `action_id` supplied with the request | derived by the bridge; a supplied value must equal it | the model must not choose the idempotency key |
| `tenant` in the request | must equal the session's tenant, which comes from the host | tenant is never a caller claim |
| confirmation state | recorded and required by the write tools, but it is consent evidence, not authority | the model can set it without asking; a consequential action still needs a person |
| a voice approval flow | none built | the gateway's inbox is used as it is |
| missions | not used in P0 | the example actions are bounded direct capabilities; long tasks would go through `missions.delegate_goal` |

## Observations about the existing gateway

Found while wiring the bridge against 0.4.0 and re-checked against 0.4.1. Nothing under `agentic_os/` is changed by this work.

Fixed in 0.4.1 (and no longer a reason for any bridge workaround, though the bridge keeps its own checks):

1. A stored idempotent result used to be returned before the visibility, permission and authority checks. Since 0.4.1 the gateway revalidates the caller (visibility, `AuthorityGuard.check`) before it serves a stored result. The bridge answers repeats from its own journal, so it asks the same question itself before it does.
2. The key used to be unbound to the arguments. Since 0.4.1 it is bound to a fingerprint of scope and canonical arguments, per tenant, and a mismatch is `idempotency_conflict`. The bridge still derives the key from the arguments, so a conflict is not expected in normal use.

Still true in 0.4.1:

3. The default idempotency store and audit sink are in memory, and so is the approval inbox. After a restart the durable record of an attempt is the authority ledger's lease (with the provider reference annotated on it) and the bridge journal. (The 0.4.1 store also has a time-to-live and a size bound; after either, a key is unknown and only the ledger holds an unknown outcome.)
4. When an authority guard returns `ESCALATE` (tier over the grant, a limit reached) the call is reported as pending approval but no inbox item is created; the owner has to extend the grant. A per-action human approval needs a grant that reaches the tier and a `MANDATORY` approval policy, which is how the send is registered.
5. Any error after dispatch of a side-effecting call, including a connection failure before the downstream did anything, leaves the lease `unknown` until a named person reconciles it. That is the safe choice; it means a failed write needs an operator step before it can be retried.

New behaviour of 0.4.1 that the bridge accounts for: a side-effecting capability with a key is always retry-safe in the gateway's store, even if not declared `idempotent`; a held write (egress `REQUIRE_APPROVAL`) is stored and replayed instead of re-run; `reconcile_call` seeds the store under the fingerprint recorded on the lease; a non-JSON argument fails the call with `gateway_error`; keys outside 1 to 256 characters are refused.

## Risks and limits of this milestone

- Python is not a sandbox. The proxy narrows what well-behaved handler code can reach; the checks behind it do not trust what the handler passes.
- One process per tenant is assumed: per-action locks are in-process. Two replicas could dispatch the same action at the same time; the downstream's own idempotency key is then the only guard. Not addressed.
- The journal holds hashes and ids, not arguments, so a pending action does not survive a restart. That is a privacy choice with a cost.
- Values returned by a read are given to the model and therefore to whatever model provider a deployment uses. Egress policy (`PolicyEgressEngine`) is a gateway seam that the demo does not configure.
- The distress screen is a short English phrase list. It misses phrasings, languages and recognition errors and it fires on harmless text ("emergency meeting"). It is a safety net, not detection.
- Calling, recording, consent and disclosure rules differ by place and by use. Nothing here addresses them.
- Not measured or tested: real speech recognition or synthesis, a real model, telephony, WebRTC or WebSocket transports, network impairment, load, or any speech-path latency.

## Decisions needed from the owner

1. Pin policy: keep `1.12.0` (newest release older than two weeks) or move to `1.15.0`, which passes the suite today.
2. Licences in the closure: Pipecat's always-installed dependencies include `num2words` and `soxr`, whose metadata declares LGPL licences (`COPYING`; `COPYING.LGPL`, with `LICENSE-libsoxr.txt` and `LICENSE-PFFFT.txt`). The separate package keeps them out of the base install; whether that is acceptable for the way this will be distributed is a licence review.
3. The extra status `outcome_unknown`.
4. (Resolved by 0.4.1, which this record now targets.) Observations 3 to 5 above remain; whether any of them should change in the gateway, in a separate change.
5. Whether pending approvals must survive a restart for voice (needs a durable approval store).
6. Whether the distress phrase list needs review by someone qualified before any use with real callers.
7. When to open the LiveKit adapter: the contract points that keep the bridge provider-neutral are `ActionRequest`, `ActionResult`, `SessionBinding` and `ActionProxy`; nothing for another framework is built.
