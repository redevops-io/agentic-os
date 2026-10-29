# Voice agents on Pipecat, with governed actions (draft)

Draft content for an integration page. Not published. Nothing in this file has been deployed anywhere.

## Purpose

[Pipecat](https://github.com/pipecat-ai/pipecat) is an open-source framework for real-time voice conversations. It decides how a call goes:
audio frames, turn-taking, interruptions, the language model's context, and (with Pipecat Flows) which step of a script the call is in.
It does not decide what a business system may do. This integration puts one typed bridge between the two: whatever a voice
agent asks for (look up a lead, update a record, send a message) becomes a request to the ReDevOps Governed Agent Gateway, which applies the
permission check, the owner's limits, human approval, idempotency and audit that every other agent request already gets.

The first release is a worked example, not a product: an inbound lead-qualification call against a mock CRM, driven entirely by fixtures.

## What you can run

Everything below runs on a laptop with no account, no key, no phone line and no network access. Run from a checkout of the repository
(agentic-os is installed from source, because it is not on PyPI).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
pip install -e integrations/voice-pipecat
cd integrations/voice-pipecat
python -m agentic_os_voice.demo.run
pytest -q tests
python bench/measure_action_path.py --n 300
python scripts/list_licenses.py
```

To install exactly the tested versions (hashes recorded in the lock file):

```bash
cd integrations/voice-pipecat
pip install uv
uv sync --frozen --extra dev
uv run --frozen pytest -q tests
```

## What the synthetic call does

The "caller" is a fixture: silent audio frames carrying a fixture id, turned into text by a fake speech-to-text stage. The "model" is a
deterministic script, run inside Pipecat's real LLM service so that Pipecat's own function-call, interruption and timeout code is what executes.
The "voice" is a fake text-to-speech stage that emits silence. The CRM is an in-process mock with fixture leads. The reviewer who approves
the message after the call is a fixture name.

1. The call starts in Pipecat Flows' first node. The caller names themselves; the host marks the caller verified (a simulated step, no real code is sent).
2. The agent looks the lead up through the bridge (a read).
3. It collects budget and timeline, reads them back, and the caller talks over the read-back (a deterministic barge-in).
4. It asks to record the result on the lead. The owner's limits allow `qualified` or `nurture` and nothing else; the write runs once, under an idempotency key derived from the content, and the mock CRM returns its own receipt.
5. It drafts a follow-up message (a read) and asks for it to be sent. Sending is registered so that a person must approve it: the request is parked and nothing is sent.
6. The call ends. The bot says only the bridge's fixed sentence for each outcome ("That is done, and the system returned a confirmation." after the CRM receipt; "I have asked a team member to review this. It is not done yet…" for the parked send).
7. After the call a named reviewer approves the parked message in the approval inbox, the bridge runs the identical request, and the mock CRM returns a second receipt.

Output of `python -m agentic_os_voice.demo.run` (the approval id differs per run):

```text
== spoken by the bot (fake TTS) ==
  bot: Hello, this is the automated phone assistant for Example Co. May I have your name?
  bot: Thanks Dana. Before I open your record I need to verify it is you. Please read me your verification code.
  bot: Thank you. I can see your request for Northwind Example Co. What budget and timeline do you have in mind?
  bot: That is done, and the system returned a confirmation.
  bot: I have drafted a short follow-up message. Would you like me to ask a colleague to send it?
  bot: I have asked a team member to review this. It is not done yet, and I cannot say when it will be. Thank you for calling Example Co. Goodbye.
== cut off by the caller ==
  bot (interrupted): To confirm: a budget between ten and fifty thousand dollars, starting this quarter, for help with onboarding. May I record that on your lead?
== flow nodes at each model turn ==
  welcome > welcome > welcome > qualify > qualify > qualify > confirm > confirm > followup > followup > followup > wrapup
== actions (bridge receipts; no argument values) ==
  lookup_lead            completed         reason=ok                 receipt_ref=audit_0
  update_lead            completed         reason=ok                 receipt_ref=crm_upd_0001
  draft_followup         completed         reason=ok                 receipt_ref=audit_2
  request_followup_send  pending_approval  reason=approval_required  receipt_ref=-
== operator review after the call ==
  alex.reviewer approved apr_N2wePbFG3CLafA; action now completed receipt_ref=msg_0002
== the mock CRM's own record of effects (ground truth) ==
  lead_update:act_c1dc26d8868217e7c6a82bfae9808fb1 -> {'provider_object_id': 'crm_upd_0001', 'lead_id': 'lead_dana01', 'applied': True}
  send_outreach:act_c1687b305213f7d56c4f00e9d822643c -> {'provider_object_id': 'msg_0002', 'sent': True, 'contact': 'lead_dana01'}
== journal chain verified: True (13 events) ==
== unreceipted claims by the bot: 0 ==
```

The check at the end is mechanical: every sentence the bot spoke that claims an outcome must contain the fixed sentence for a status that occurred in the journal, and
"completed" requires a completed action carrying a downstream receipt.

## How it is built

- `VoiceActionBridge` (no Pipecat import) accepts a typed request: session, tenant, tool name, bounded arguments, evidence references, confirmation state. Only tools on a closed allowlist exist; arguments are checked against strict schemas with size limits; the action id is derived from the intent, so the same request is the same action however many times it arrives.
- Tenant and identity come from a session the host creates after its own authentication. The caller's number is recorded as metadata and is never used to authenticate. Account tools need a verified session and an account the session was verified for.
- Accepted requests go to `AgentGateway.invoke`. The bridge adds no permissions of its own; the owner's grants (authority envelope), the approval inbox and the idempotency key are the gateway's.
- A write is journaled before it is dispatched and again when it ends, in the repository's hash-chained event log. A result is `completed` only if the downstream system returned a receipt. A lost reply or a crash leaves the action `outcome_unknown`; it is not sent again until a named person reconciles it with evidence.
- Cancelling the caller (a barge-in, a framework timeout) never cancels a dispatched write. Asking again returns the same receipt.
- Pipecat tool handlers receive only an `ActionProxy` for their own session. No backend credential is in their reach.
- Distress or emergency wording in a transcript is kept from the model; the agent speaks a fixed script that requests a hand-off and promises nothing, and no further tool runs on that call.

Design record: [docs/VOICE_PIPECAT_ADR.md](../../docs/VOICE_PIPECAT_ADR.md).

## Evidence

Labels follow the repository's convention. Nothing here is *Externally reproduced* or *Field validated*.

**Test suite (Internally reproduced, synthetic inputs).** 98 tests in `integrations/voice-pipecat/tests` and 5 boundary tests in `tests/test_voice_pipecat_boundary.py`:

| Area | What is asserted |
|---|---|
| authorisation | anonymous and merely-claimed callers are refused account data; a caller verified for one account cannot reach another; wrong tenant, unknown, closed and expired sessions are refused; authority revoked or expired and permissions revoked during a call stop the next action; values outside the owner's grant are denied; transcript-injection attempts (unlisted tools, widened status, extra fields, other accounts, an unapproved send) produce no unauthorised effect |
| idempotency | repeated, concurrent (tasks and threads), cross-session and post-restart requests for one intent give one effect and one receipt; a changed argument is a new action; a duplicate "call started" delivery opens no second session |
| crash boundaries | the repository's `CrashingStore` is armed before and after the bridge's journal writes; a kill inside the downstream call; a lost reply; a failure before any effect; journal unavailable. In each case the mock CRM's own count of effects is checked: no duplicate |
| cancellation | cancelling during a write leaves it to finish once; a deadline during a write reports in flight, never completed; a request cancelled before dispatch has no effect; Pipecat's real interruption frame cancels a read and does not cancel a write |
| approvals | a person approves, once; an approval is bound to the exact request; rejected and expired approvals do not run; an unavailable approval service fails closed and leaves nothing reserved; a hand-off requested after a message was parked stops it even if approved |
| distress | phrase screen; the model never gets a turn for the matching transcript; the spoken script contains no promise; later tools are refused |
| profile | the bridge refuses a gateway without an authority guard; the whole call also runs under `AGENTIC_OS_PROFILE=production`; no provider service import, no transport import, no key read, no extras, no tool that dials; the demo blocks connect, bind and listen |

**Governance overhead of the action path (Internally reproduced, synthetic).** Time spent in the bridge and the gateway for one action, with the mock CRM's own time subtracted,
n = 300 per row after a 20-call warm-up, Python 3.13.18, macOS-27.0.1-arm64-arm-64bit-Mach-O, one run. In-memory stores:

| Path | p50 | p95 | p99 | max |
|---|---|---|---|---|
| read (lookup) | 0.091 | 0.106 | 0.169 | 0.276 |
| write (new intent) | 0.126 | 0.174 | 0.216 | 0.265 |
| replay (completed write asked again) | 0.016 | 0.018 | 0.024 | 0.142 |
| refusal (not allowlisted) | 0.009 | 0.011 | 0.012 | 0.018 |

Durable stores (hash-chained JSONL journal with fsync, SQLite authority ledger), milliseconds:

| Path | p50 | p95 | p99 | max |
|---|---|---|---|---|
| read (lookup) | 2.899 | 3.478 | 4.153 | 4.598 |
| write (new intent) | 1.377 | 2.453 | 5.884 | 6.116 |
| replay (completed write asked again) | 0.086 | 0.104 | 0.143 | 0.163 |
| refusal (not allowlisted) | 0.078 | 0.103 | 0.126 | 1.179 |

These numbers describe this code on one machine. They are not a speech-path latency, not a comparison with any other product, and not a statement about any deployment. Most of the durable cost is the existing SQLite authority ledger opening a connection per transaction. Reproduce with `python bench/measure_action_path.py --n 300`; the evidence card is `docs/evidence/voice_action_path_overhead.yaml`.

## What is mocked, and what is not tested

Mocked or fake in every run: speech-to-text, the language model, text-to-speech, the transport (frames are queued in-process), the CRM, the approval reviewer, the identity verification step, the time.

Not tested, and no claim is made about them:

- real speech recognition or synthesis, accents, noise, or recognition errors (the distress screen sees clean fixture text);
- a real language model, including how a real model behaves when given the allowlist;
- telephony, WebRTC, WebSocket or any other real transport, and any provider SDK;
- network impairment, reconnects, packet loss or jitter;
- load, many concurrent calls, or several replicas of the bridge (one process per tenant is assumed; per-action locks are in-process);
- any latency of the speech path;
- a real CRM, a real approval service, a real identity provider, or durable approvals (the approval inbox used here is in memory and does not survive a restart);
- macOS and Linux only; Windows is untested; Python 3.11, 3.12 and 3.13 are covered by the hosted workflow, 3.14 only locally.

## Licences of every component

Quoted from each installed distribution's own metadata by `scripts/list_licenses.py` in the environment created by `uv sync --frozen --extra dev` (the locked versions). "-" means the field is empty. The table is not an interpretation: read the licence files themselves.
`pytest`, `pytest-asyncio`, `pluggy`, `iniconfig`, `packaging` and `Pygments` are test-only.

Pipecat 1.12.0 ships a `LICENSE` file titled "BSD 2-Clause License" ("Copyright (c) 2024–2026, Daily"); its metadata field `License-Expression` is `BSD-2-Clause`. The same `LICENSE` text is at the `v1.12.0` tag of the Pipecat repository.
This package imports agentic-os and carries the repository's licence files (`LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`).

| Distribution | Version | License-Expression | License field | License classifiers | Licence files shipped |
|---|---|---|---|---|---|
| agentic-os | 0.4.0 | - | AGPL-3.0-or-later WITH Commons-Clause | - | LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md |
| agentic-os-voice-pipecat | 0.1.0 | - | AGPL-3.0-or-later WITH Commons-Clause | - | LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md |
| aiofiles | 25.1.0 | - | Apache-2.0 | Apache Software License | LICENSE, NOTICE |
| aiohappyeyeballs | 2.7.1 | - | PSF-2.0 | Python Software Foundation License | LICENSE |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | - | - | LICENSE, LICENSE.txt |
| aiosignal | 1.4.0 | - | Apache 2.0 | Apache Software License | LICENSE |
| annotated-doc | 0.0.5 | MIT | - | - | LICENSE |
| annotated-types | 0.8.0 | MIT | - | MIT License | LICENSE |
| anyio | 4.15.1 | MIT | - | - | LICENSE |
| attrs | 26.1.0 | MIT | - | - | LICENSE |
| audioop-lts | 0.2.2 | PSF-2.0 | - | - | LICENSE |
| certifi | 2026.10.23 | - | MPL-2.0 | Mozilla Public License 2.0 (MPL 2.0) | LICENSE |
| cffi | 2.1.1 | MIT-0 | - | - | LICENSE |
| click | 8.5.0 | BSD-3-Clause | - | - | LICENSE.txt |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | - | - | LICENSE, LICENSE.APACHE, LICENSE.BSD |
| discovery-runtime | 0.1.19 | - | AGPL-3.0-or-later WITH Commons-Clause | - | LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md |
| docopt | 0.6.2 | - | MIT | MIT License | LICENSE-MIT |
| docstring_parser | 0.18.0 | - | MIT | MIT License | LICENSE.md |
| fastapi | 0.162.0 | MIT | - | - | LICENSE |
| flatbuffers | 25.12.19 | - | Apache 2.0 | Apache Software License | - |
| frozenlist | 1.8.0 | - | Apache-2.0 | - | LICENSE |
| h11 | 0.16.0 | - | MIT | MIT License | LICENSE.txt |
| httpcore | 1.0.9 | BSD-3-Clause | - | BSD License | LICENSE.md |
| httpcore2 | 2.16.0 | BSD-3-Clause | - | BSD License | LICENSE.md |
| httpx | 0.28.1 | - | BSD-3-Clause | BSD License | LICENSE.md |
| httpx2 | 2.16.0 | BSD-3-Clause | - | BSD License | LICENSE.md |
| idna | 3.20 | BSD-3-Clause | - | - | LICENSE.md |
| iniconfig | 2.3.1 | MIT | - | - | LICENSE |
| jiter | 0.18.0 | MIT | - | - | LICENSE |
| knowledge-frontier | 0.2.0 | - | AGPL-3.0-or-later | - | - |
| llvmlite | 0.50.0 | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception | - | - | LICENSE, LICENSE.thirdparty |
| loguru | 0.7.3 | - | - | MIT License | - |
| loudness | 0.2.0 | - | MIT | - | LICENSE |
| Markdown | 3.11.2 | BSD-3-Clause | - | - | LICENSE.md |
| mpmath | 1.3.0 | - | BSD | BSD License | LICENSE |
| multidict | 7.2.1 | Apache-2.0 | - | - | LICENSE |
| num2words | 0.5.14 | - | LGPL | GNU Library or Lesser General Public License (LGPL) | COPYING |
| numba | 0.68.1 | - | BSD | BSD License | LICENSE, LICENSES.third-party |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | - | - | COPYING, LICENSE, LICENSE.md, LICENSE.txt |
| onnxruntime | 1.24.4 | - | MIT License | MIT License | LICENSE |
| openai | 3.41.0 | Apache-2.0 | - | Apache Software License | LICENSE |
| opentelemetry-api | 1.45.2 | Apache-2.0 | - | - | LICENSE |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | - | - | LICENSE, LICENSE.APACHE, LICENSE.BSD |
| pillow | 12.3.0 | MIT-CMU | - | - | LICENSE |
| pipecat-ai | 1.12.0 | BSD-2-Clause | - | - | LICENSE |
| pluggy | 1.7.0 | - | MIT | MIT License | LICENSE |
| propcache | 0.5.4 | - | Apache-2.0 | - | LICENSE, NOTICE |
| protobuf | 6.33.6 | - | 3-Clause BSD License | - | LICENSE |
| pycparser | 3.12 | BSD-3-Clause | - | - | LICENSE |
| pydantic | 2.14.2 | MIT | - | - | LICENSE |
| pydantic_core | 2.50.2 | MIT | - | - | LICENSE |
| Pygments | 2.21.0 | BSD-2-Clause | - | - | LICENSE |
| pytest | 9.2.0 | MIT | - | - | LICENSE |
| pytest-asyncio | 1.4.1 | Apache-2.0 | - | - | LICENSE |
| python-dotenv | 1.2.4 | - | BSD-3-Clause | - | LICENSE |
| PyYAML | 6.0.3 | - | MIT | MIT License | LICENSE |
| resampy | 0.4.3 | - | ISC | ISC License (ISCL) | LICENSE.md |
| runtime-contracts | 0.3.14 | - | AGPL-3.0-or-later WITH Commons-Clause | - | LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md |
| sentencex | 1.0.31 | MIT | - | MIT License | - |
| sniffio | 1.3.1 | - | MIT OR Apache-2.0 | MIT License; Apache Software License | LICENSE, LICENSE.APACHE2, LICENSE.MIT |
| soundfile | 0.13.1 | - | BSD 3-Clause License | BSD License | COPYING, LICENSE |
| soxr | 1.0.0 | LGPL-2.1-or-later | - | - | COPYING.LGPL, LICENSE-PFFFT.txt, LICENSE-libsoxr.txt, LICENSE.txt |
| starlette | 1.10.0 | BSD-3-Clause | - | - | LICENSE.md |
| sympy | 1.14.0 | - | BSD | BSD License | LICENSE, LICENSE.txt |
| truststore | 0.10.5 | MIT | - | - | LICENSE |
| typing-inspection | 0.4.4 | MIT | - | - | LICENSE |
| typing_extensions | 4.16.0 | PSF-2.0 | - | - | LICENSE |
| uvicorn | 0.54.0 | BSD-3-Clause | - | - | LICENSE.md |
| websockets | 17.3 | BSD-3-Clause | - | - | LICENSE |
| yarl | 1.25.1 | - | Apache-2.0 | - | LICENSE, NOTICE |

## Calling, recording and disclosure

Rules about automated calls, call recording, consent, caller disclosure, retention and the handling of emergency calls differ by country, region, industry and use. Nothing in this integration, its tests
or its documentation addresses them or establishes that any use is lawful. The synthetic call speaks a short disclosure that it is automated; that is a demo line, not compliance. Anyone who connects real callers needs their own legal advice and procedures first.

## Outbound calling

Outbound calling is disabled and out of scope. There is no tool, capability or code path that places a call or sends a message on its own; the only message in the example is parked for a person's approval. A test asserts that no tool in the allowlist dials, texts or calls out.

## Not claimed

This page does not say the integration is secure or compliant, does not rank it against other voice frameworks or vendors, and does not claim any speed. It describes what the code does and what was run.
