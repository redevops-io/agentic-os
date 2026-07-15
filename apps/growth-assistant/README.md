# growth-assistant (port 8213)

The AI marketing/growth strategist for first-time founders — the open,
self-hosted answer to growth agencies (Growth Division, GrowthRocks, Demand
Curve, founder-led ghostwriting shops). Instead of generic "social posts" it
produces *strategic traction* across four pillars and wires the output into the
rest of the Agentic OS.

## Four pillars

1. **Subreddit incubation** — brand subreddit setup, first-100-threads seed
   list, non-spammy parallel-subreddit outreach (self-promo-rule safe).
2. **Founder-led growth** — ghostwritten X / LinkedIn in the founder's voice
   (build-in-public; audiences buy the person before the company).
3. **Lead-magnet community** — Discord / WhatsApp / Facebook group blueprint
   centered on the *problem*, not the product.
4. **Cold outreach + accelerators** — audit-based Loom scripts for fresh
   Product Hunt launchers + the free "Community-101 workshop for referrals" play.

## Freelancer sourcing

`hire_brief` produces a JD + vetting scorecard + ready-to-run **search links**
across Upwork / Fiverr / Contra / Wellfound / PeoplePerHour / `r/forhire` /
LinkedIn, plus an outreach DM. There's no clean public talent-search API
(Fiverr has none; Upwork needs an approved app), so v1 is brief + links +
outreach — vetting stays human. Roles: `reddit_specialist`, `copywriter`,
`designer`.

## Actions — `POST /agent/run`

| action | does | push=true side effect |
| --- | --- | --- |
| `playbook` | full 4-pillar zero-to-traction plan | — |
| `subreddit_plan` | subreddit + first-100-threads + parallel subs | — |
| `founder_content` | voice profile + N X/LinkedIn posts | best-effort drafts → **Postiz** |
| `community_blueprint` | Discord/WhatsApp/FB community plan | creates a **Listmonk** list |
| `cold_outreach` | Loom audit scripts + accelerator pitch + DMs | creates **ERPNext** Leads from `targets[]` |
| `hire_brief` | freelancer JD + scorecard + search links + DM | — |
| `ask` | NL question over saved assets + core status | — |

`startup` payload: `{name, product, icp, stage, problem, founder_handle, links}`.
Every asset is saved under `GROWTH_DATA_DIR` and rendered on the dashboard
(`/`, `/api/activity`, `/api/assets`, `/api/assets/{id}`). **Nothing is
auto-published to a prospect or community** — a human approves and pushes.

## Chat (public surface)

`POST /api/chat` `{message, history[]}` — a conversational layer over the actions.
The brain **prefers `qwen-reasoning`** (`CHAT_LLM_*`; k3s NodePort, often scaled to
0 → instant fallback) and falls back to the always-on **DeepSeek-V4-Flash**, then
Claude. It either **routes** a clear request to a generation action (returning the
`asset_id`) or answers conversationally. The dashboard renders a chat box that
POSTs to the relative `api/chat` path, so it works both standalone (`/api/chat`)
and behind the control-plane proxy (`/m/growth-assistant/api/chat`).

Public-safety model: the control-plane proxy forwards **only `/m/<name>/api/*`**, so
`/agent/run` (which can push to cores) is never publicly reachable. Chat is
**generation-only by construction** (never pushes) and **rate-limited**
(`CHAT_RATE_PER_MIN`, per-IP via `X-Forwarded-For`). `GENERATION_ONLY=true` is an
extra kill-switch that disables all core writes on the instance (set on the public
demo). Config: `CHAT_LLM_BASE_URL`, `CHAT_LLM_MODEL`, `GENERATION_ONLY`, `CHAT_RATE_PER_MIN`.

## Integrations (graceful degradation)

- **ERPNext** (CRM) — `cold_outreach push=true` creates `Lead` records for the
  first-customer pipeline.
- **Listmonk** — `community_blueprint push=true` creates the community list.
- **Postiz** — `founder_content push=true` best-effort posts drafts via Postiz's
  public API; if the API port isn't bound it no-ops and the drafts stay saved
  locally for the founder to paste/schedule.

Every external call is wrapped — a down core never breaks asset generation.

## Deploy

Part of `integrated.compose.yml`; deploy with the rest of the stack:

```
make proxmox-agentic-os AGENTIC_SERVICES=growth-assistant
```
