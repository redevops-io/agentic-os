# Projects + Sidekick — orchestration node (no cores, no fleet)

Deploy **just the Projects UI + Sidekick** to compose and run missions that orchestrate **3rd-party agents**
(over MCP / A2A) across messengers and social — *without* the agentic-apps fleet (Chatwoot, ERPNext, Lago,
Postiz, …). One public image, one model endpoint.

## Run it

```bash
cp .env.example .env            # set a model endpoint (local or cloud)
docker compose up -d
# Projects UI       → http://localhost:8290/
# Workflows console → http://localhost:8290/workflows
```

That's the whole deployment. The image is pulled from GHCR — **nothing to build**.

## What you get

| Surface | What it does |
|---|---|
| Projects UI (`/`) | the Projects + Sidekick app — projects, missions, approvals, the Sidekick supervisor |
| Workflows console (`/workflows`) | compose a **parallel** workflow, preview its execution waves, launch it on the governed Mission Runtime, watch branches merge |
| A2A / MCP interop | external agents (Agentforce, ServiceNow, OpenAI Agents SDK, …) run as **governed branches**; your workflows are also callable **by** those orchestrators (AgentCard / MCP tool) |
| Collaboration triggers | a Slack / WhatsApp / Teams / Google Chat message can start a workflow and get the result back in-thread |

## What it needs

1. **A model endpoint — the only hard dependency.** Local (ollama / llama.cpp on `host.docker.internal:11434`, the default — ≈free) **or** a cloud endpoint + key. Set `REDEVOPS_LLM_BASE_URL` / `REDEVOPS_LLM_MODEL`.
2. **(Optional) cloud keys** — `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` to dispatch Claude + GPT agents in parallel.
3. **(Optional) 3rd-party agent endpoints + channel tokens** — bring only what you use. External agents connect over MCP/A2A at runtime; channel tokens (`SLACK_BOT_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, …) enable triggers/delivery/approvals.

**Not required:** the agentic-apps fleet, cores, context-runtime, Vault, Kafka, or a database. The Mission event
log is in-memory by default; durable storage (JSONL / Postgres) is a production config step, not a prerequisite.

## Open-core / licensing

- The **image is public** and runnable by anyone from GHCR.
- The **AGPL kernel source** (`agentic-os`) is open; the **enterprise overlay source is private** — you get the
  binary, not the code.
- Review licensing terms before production use; enterprise features in the public image are subject to the
  ReDevOps enterprise license.

## Even simpler: the Mission SDK

For embedding orchestration in your own app (rather than running the console), the **Mission SDK**
(`redevops-io/mission-sdk`) is the thin client — compose and launch missions from code, and connect 3rd-party
agents, against this node.
