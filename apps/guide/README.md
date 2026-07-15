# guide — the onboarding assistant that walks people through every app

A RAG help bot over the stack's own docs (the redevops-rag pattern): ask "how do I use billing?"
or "walk me through Outreach Engine" and it retrieves the relevant app cards and answers with
citations — free-form Q&A **and** guided per-app walkthroughs (what it does → open dashboard →
first action). RBAC-native: every answer is filtered by the caller's role, so the guide only
surfaces the apps a user is allowed to see.

## Endpoints
| route | what |
|---|---|
| `GET /` | MD3 dashboard: search + guided walkthroughs |
| `GET /api/activity` | same data as JSON · `GET /health` |
| `POST /agent/run` | retrieve + answer (cited) / walkthrough for an app |

## Files
`core.py` retrieval + answer logic · `operator.py` the gated surface · `app.py` the service ·
`test_operator.py` tests. Follows the shared operator pattern — see [../README.md](../README.md).
