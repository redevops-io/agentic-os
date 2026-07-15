# agentic-crm — sales/CRM agent over a real ERPNext CRM core

The open, self-hosted answer to **Salesforce Agentforce's Sales Development Agent**
and **HubSpot Breeze's Prospecting Agent**. The CRM record system (Leads /
Opportunities / Customers / Contacts) is already running in your **ERPNext**
instance — this module adds the agent archetypes + an MD3 pipeline dashboard over
the live data. No mock data.

## Endpoints
| route | what |
|---|---|
| `GET /` | MD3 pipeline dashboard (KPIs, stage bars, lead queue) from live ERPNext |
| `GET /api/activity` | the same data as JSON |
| `GET /health` | `{status, core:"erpnext-crm", connected}` |
| `POST /agent/run` | agentic actions (below) |

## Agent actions (`POST /agent/run`)
| action | body | effect (all human-auditable; nothing is auto-sent) |
|---|---|---|
| `score_lead` | `{"lead":"<name>"}` | LLM scores 0-100 + rationale + next action → Lead comment + `lead_score` |
| `research` | `{"lead":"<name>"}` | enrichment/buying-signal brief → Lead comment (set `DEERFLOW_URL` for real web signals) |
| `draft_outreach` | `{"lead":"<name>"}` | personalised first-touch email → Lead comment (a human sends it) |
| `qualify` | `{"lead":"<name>","status":"Replied"}` | advance the Lead status |
| `ask` | `{"q":"<natural language>"}` | NL-to-CRM: answer over the live pipeline snapshot |

## Config (env)
`ERPNEXT_URL`, `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`, `ERPNEXT_FRONT_URL`,
`REDEVOPS_LLM_BASE_URL` + `REDEVOPS_LLM_MODEL` (the brain, DeepSeek-V4-Flash),
optional `DEERFLOW_URL` (real enrichment) and `ANTHROPIC_API_KEY` (Claude fallback).

## Enrichment data (the #3 gap)
There is **no open-source self-hosted B2B enrichment DB**; the market is proprietary
SaaS (Crunchbase, Lead411, Autobound, Apollo). `research` therefore reasons over known
lead fields by default, and — when `DEERFLOW_URL` is set — pulls real buying signals
(funding, hiring, launches, leadership changes) via your DeerFlow web-research agent.
Assemblable free sources: GitHub API, Apollo free (50/mo), Crunchbase free search,
Companies House, job-board / news scraping.

Seed demo data: `python seed.py` (writes `.env` too). Run: `uvicorn app:app --port 8210`.
