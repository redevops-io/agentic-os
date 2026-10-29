# agentic-os-voice-pipecat

An optional voice-agent integration for agentic-os. It is **not part of the base install**: `pip install agentic-os` does not
include it, import it, or depend on it. Design record: [docs/VOICE_PIPECAT_ADR.md](../../docs/VOICE_PIPECAT_ADR.md). Draft
integration page (what is and is not tested): [INTEGRATION_PAGE_DRAFT.md](INTEGRATION_PAGE_DRAFT.md).

## Purpose

A voice framework decides how a conversation goes: turns, interruptions, speech. It must not decide what a business system may do. This package
lets [Pipecat](https://github.com/pipecat-ai/pipecat) run the conversation while every action a model asks for crosses one typed bridge into the
existing Governed Agent Gateway, which already provides the permission check, the authority envelope, budgets, approvals, idempotency and audit.

## What is here

| Module | Imports Pipecat | What it does |
|---|---|---|
| `agentic_os_voice.contracts` | no | typed request/result, bounded argument schemas, deterministic action ids |
| `agentic_os_voice.bridge` | no | `VoiceActionBridge`: allowlist, session binding, journal, cancellation and replay rules, hand-off to the gateway |
| `agentic_os_voice.capabilities` | no | the example CRM capabilities, tool allowlist and owner grants |
| `agentic_os_voice.handoff` | no | the distress screen and the fixed hand-off script |
| `agentic_os_voice.pipecat_adapter` | yes | Pipecat tool schemas and handlers (proxy only), a Flows function, `DistressGuard` |
| `agentic_os_voice.demo` | yes | the synthetic call: fake speech, scripted model, mock CRM, call simulator |

`contracts`, `bridge`, `capabilities` and `handoff` do not import Pipecat, so another voice framework can sit on the same bridge.

## Install

agentic-os is not on PyPI; install it from a release tag first, then this package. In a checkout of this repository:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
pip install -e integrations/voice-pipecat
```

To install exactly the versions that were tested (hashes recorded in `uv.lock`):

```bash
cd integrations/voice-pipecat
pip install uv
uv sync --frozen --extra dev
```

The profile asks for `pipecat-ai==1.12.0` with **no extras**: no provider SDK, no telephony or WebRTC transport, no listening server, no key. It
downloads no model (the voice-activity and turn models that the Pipecat wheel ships inside itself are not loaded by anything here).

## Run

```bash
python -m agentic_os_voice.demo.run                      # the synthetic call, then the operator's review
AGENTIC_OS_PROFILE=production python -m agentic_os_voice.demo.run   # same, with the production profile's durable-guard requirement
python bench/measure_action_path.py --n 200              # governance overhead of the action path
python scripts/list_licenses.py                          # declared licences of everything installed
cd integrations/voice-pipecat && pytest -q tests         # the suite (APPROVAL_SECRET unset)
```

Nothing in these commands contacts a network, a provider or a phone line, and the demo refuses to if asked to.

## Limits

Read the limits section of [INTEGRATION_PAGE_DRAFT.md](INTEGRATION_PAGE_DRAFT.md) before using any of this near real callers. In short: speech, telephony, a real model, network
impairment and load are not exercised; calling, recording and disclosure rules are not addressed by this code; outbound calling is not implemented.

## Licence

The licence files of the repository are copied here (`LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`): this package imports agentic-os.
Pipecat and every other dependency keep their own licences; `scripts/list_licenses.py` quotes what each declares.
