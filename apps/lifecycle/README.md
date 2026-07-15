# lifecycle — email/SMS lifecycle marketing over a self-hosted Listmonk core

The open answer to **Klaviyo** (its **Composer** "campaign from one prompt" + predictive/
assistive marketing) — **own distribution, no SaaS, no 250-profile free cap**. Listmonk is
the high-volume OSS sender/list core; this module adds the agent + an MD3 dashboard.

## Endpoints
| route | what |
|---|---|
| `GET /` | MD3 dashboard: subscribers, lists, recent campaigns (live Listmonk) |
| `GET /api/activity` | same data as JSON · `GET /health` |
| `POST /agent/run` | agent actions (below) |

## Agent actions
| action | body | effect |
|---|---|---|
| `compose_campaign` | `{"prompt":"...","list_id":N}` | LLM writes subject + HTML → creates a Listmonk **draft** (human sends) |
| `segment` | `{"goal":"..."}` | LLM proposes a Listmonk advanced-SQL subscriber segment |
| `suggest_flow` | `{"trigger":"welcome\|abandoned_cart\|winback"}` | LLM designs a multi-step flow + drafts each message |

## Config (env)
`LISTMONK_API_URL`, `LISTMONK_API_USER`, `LISTMONK_API_TOKEN` (Basic auth), `LISTMONK_FRONT_URL`,
`REDEVOPS_LLM_BASE_URL` + `REDEVOPS_LLM_MODEL`, optional `ANTHROPIC_API_KEY`.

## The core
Bring up Listmonk + its Postgres with `listmonk.compose.yml` (repo root), then create an
API user in the Listmonk UI (Settings → Users) and set `LISTMONK_API_USER`/`LISTMONK_API_TOKEN`.
Klaviyo's free plan can be used as a stop-gap benchmark, but self-hosting removes the
250-profile / branding limits and keeps the data on hardware you own.
