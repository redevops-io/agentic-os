# outreach-engine — pilot outreach & pipeline over a self-hosted Twenty CRM core

An agent layer + MD3 dashboard over a real **Twenty CRM** core: rank accounts, pick an outreach
play, draft the opener, summarize the pipeline, and explain the CRM — every action shows its work.
Generic across workspaces (configure `OUTREACH_TENANT` / `OUTREACH_PITCH` / `OUTREACH_VERTICALS`);
defaults to the demo tenant. The money/send-moving steps pause for human approval.

## Endpoints
| route | what |
|---|---|
| `GET /` | MD3 dashboard: ranked accounts, picked play, drafted opener, approval state |
| `GET /api/pipeline` | same data as JSON · `GET /health` (Twenty `/healthz`) |
| `POST /agent/run` | agent actions (source · personalize · sequence), gated on send |

## Files
`core.py` pipeline/scoring logic · `operator.py` the gated surface · `app.py` the service ·
`test_operator.py` tests. Follows the shared operator pattern — see [../README.md](../README.md).
