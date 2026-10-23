# Side-effect path inventory (workstream A, item 4)

Question per path: does it pass through ONE central policy-enforced dispatcher, or bypass it? There are **three** governed dispatchers, not one, and none is universal:
- **D1 AgentGateway.invoke** (`agentic_os/agent_gateway/gateway.py:126`): external-agent path.
- **D2 Mission Executor.run -> Operator /invoke** (`mission/executor.py:228`, `operator_sdk.py:253`): policy plane + signed approval token.
- **D3 agent_harness ToolRegistry.run** (`~/projects/agent-harness/agent_harness/tools.py:137`): tool approval policy.
"Gate" below = approval/policy check before the effect. Labels: C = CONFIRMED (code read; most rows read by delegated sub-audits, spot-checked by me for items marked C*), I = INFERRED.
Spot-checked by me (C*): content/app.py:19, projects/app.py:70 + content_service.py:150, safe_url zero callers, approvals.py:64, gateway.py defaults, apps/billing/core.py:486-520.

## A. BYPASSES of central enforcement (ranked)
| # | file:line | Function / route | External effect | Why it bypasses | Sev |
|---|---|---|---|---|---|
| B1 | `agentic_os/projects/app.py:70` -> `projects/content_service.py:131-160` (publish at :150) | `POST /api/missions/{mid}/decide` -> `ProjectsContentService.decide` | Posts to X / LinkedIn / IG / TikTok via `self._publisher.publish(draft)` | No auth on route; publishes directly, then calls `run.approve()` with a no-op publisher (:157) so the mission only records after the fact; `actor` is client-supplied (:70 body) | **Critical** C* |
| B2 | `agentic_os/content/app.py:19` | `POST /api/projects/{pid}/missions/{mid}/approve` | Resolves the Mission publish gate, then `MultiPublisher` posts to X/LinkedIn/IG (content/fleet.py:68-91, clients.py:215/289/317) | Gate is real but the **approval decision endpoint is unauthenticated** (anyone reaching it is the approver) | **High** C* |
| B3 | `deploy/admin-console/app.py:31` -> `world/seeder.py:47`, `world/adapters.py:117-258` | `GET /api/admin` | Creates Lago customers, Chatwoot contacts, Twenty companies, ERPNext Customer/Sales Invoice/Payment Entry/Expense/Journal Entry when core env vars set | Unauthenticated GET; `ExecutionMode.SIMULATE` default not consulted by `ProjectionSeeder.project` | High (conditional on env) C |
| B4 | `agentic_os/control_plane.py:355-370` | `/m/{name}/api/{path}` GET/POST proxy | Forwards arbitrary POST bodies to module services | No auth unless `DEMO_READ_ONLY=1`; downstream auth unknown. Same shape in context-runtime `control_plane/app.py:291` | High C / downstream I |
| B5 | `integrations/execution.py:149-171`, `agent_gateway/gateway.py:233-238` | `run_test_mission`, gateway `_envelope` -> adapter `execute(..., envelope)` | Real Slack, Gmail, Stripe, HubSpot, WhatsApp via `integrations/live.py:71-106`; Google Sheets/Docs, Graph, local files | `GovernedEnvelope` is self-minted and adapters only test `envelope is None` (google_app.py:226, microsoft_app.py:231, file_formats.py:307, libreoffice_app.py:223); it proves no approval | High (design) C |
| B6 | `interaction/channels_telegram.py:141-168`, `channels_whatsapp.py:123-162`, operator `interaction/operator.py:68` | `interaction.reply` (side_effecting) | Outbound chat/voice/file to customers | No `approval_required`; file comment "base stub does not gate" (operator.py:7). Reachable via any authenticated /invoke | Medium C |
| B7 | `agentic_os/channels.py:118-231,264` + `Gateway.serve_forever :319` | Telegram/Slack/Discord/SMS send; chat poller | Chat messages; poller dispatches to fleet | Direct; poller authorizes by spoofable username allowlist (:297), open if `GATEWAY_OPEN=1` | Medium C |
| B8 | `world/connectors.py:135-139` | `HubSpotSequenceSender.send` | CRM upsert then sequence enroll (email) | Writes CRM contact (:138) before checking `available()` (:139); `world/outreach.py:200` `REDEVOPS_AUTO_SEND=1` removes human review | Medium C |
| B9 | `integrations/outreach.py:174-176` | `run_outreach` | `provider.activate()` starts email sequence | `governance="allow"` is the default = auto-activate | Medium C |
| B10 | `revenue/loop.py:73`, `visualization/governed.py:59` | `execute_quote_plan`, `create_visualization` | ERPNext quotation/opportunity writes; Metabase card/dashboard | Approval is a caller-asserted `approved: bool`; `InMemoryVizApprovals.approve(by=)` unauthenticated (governed.py:44) | Medium C |
| B11 | `social/meta_graph.py:51,63`; `intelligence/email/bridge.py:98`; `collaboration/{slack,msteams,google_chat,whatsapp}.py` | publish FB/IG; send email; chat posts/edits | external posts / email | Library calls, token-presence only; no in-package caller mostly -> **latent** (reachable by any future caller w/o gate) | Low-Med C |
| B12 | `integration/providers/http.py:118-136`, `integration/obligations.py:68-95` | generic REST write; retry-until-discharge | ERP create/update | No gate; retries even when verification found conflicts | Medium C |
| B13 | `apps/edge-sentinel/operator.py:38,51` vs `core.py:290,375`; `apps/growth-assistant/core.py:577`; `apps/edge-sentinel/app.py:707-710` | `block_ip`/`unblock_ip`, `push_asset`, console `ban_ip` | CrowdSec decisions; Postiz/Listmonk/ERPNext pushes | Gate lives in the operator wrapper, `core.*` execution functions are ungated ("Caller gates"); console tool relies on harness staging (I) | Medium C |
| B14 | `apps/agentic-privacy/operator.py:46 -> core.py:733-763, 270-272` | `privacy.intake` | Emails a verification link to a caller-chosen address; link host taken from caller `base_url` when `PRIVACY_PUBLIC_URL` unset | No approval; phishing relay through company mail | Medium C |
| B15 | `apps/agentic-crm/operator.py:32-58`, `apps/social-autopilot core.py:294-345`, `apps/support core.py:295-345`, `apps/lifecycle core.py:145-161`, `apps/infra operator.py:113,132` | score_lead/research/draft_outreach; social.draft; support.draft_reply; compose_campaign; infra.plan/drift | ERPNext comment/PUT; Postiz DRAFT INSERT; Chatwoot private note; Listmonk draft; terraform plan / ansible --check | Ungated by design (draft/annotate/read-only tooling) but all external writes or process execution on any authenticated /invoke | Low-Med C/I |
| B16 | `agentic_os/projects_api.py:1635` (`Today.approve`, today.py:455) | `POST /api/projects/{pid}/today/{item}/approve` | `ExternalAgentOperator(adapter or FakeExternalAgentAdapter())` | Unauthenticated self-approval; safe only because default adapter is fake | Low now / High if real adapter injected C |
| B17 | `secrets/bootstrap.py:133-156`, `cli.py:61-71`, `secrets/stores.py:201` | rc-file edits (shell injection if path has `"`/`$()`), `secrets get` prints value, Vault URL no `..` sanitation | local config / secret disclosure | CLI/library, no gate | Low C |
| B18 | `control_plane.py:492,497`, `fleet.py:65-88` | `/up` `/down` | `git clone`, `docker compose up/down` | Gated by `require_api_key` only, no approval, argv lists (no shell) | Low (authn OK) C |
| B19 | `apps/video-ad/operator.py:78-111` | `video.render_segment`, `regenerate_shots`, `unpublish` | paid render, retract social post | Handlers in external `reel` package, not auditable here; `unpublish` not `approval_required` | Unknown I |

## B. Paths that DO go through a gate (central-ish)
| file:line | Effect | Gate | Dispatcher | Residual weakness |
|---|---|---|---|---|
| `apps/billing/core.py:504`, :306, :554, :618 | Lago refund credit note, dunning retry, subscription create/cancel | `_approval` truthiness inside core | D2 | bare truthy check; approvals re-trusted in-process (`Operator.invoke` passes `_approval` through, operator_sdk.py ~330) C |
| `apps/books/core.py:333,540,605,666,783` | ERPNext categorize/close/journal/reverse | same | D2 | same C |
| `apps/support/core.py:392,508,541` | Chatwoot customer message/resolve/assign | `gated()` + core | D2 | `conv_id` interpolated unvalidated into URL path (I) |
| `apps/compliance/core.py:413,551` | ticket POST; consent ledger append | same | D2 | consent idempotent via ledger scan (durable) C |
| `apps/infra/operator.py:55,123,135,139` | terraform apply/destroy, ansible, rollback | `_approval`/`gated()` | D2 | `plan_digest` optional: apply unbound to approved plan (operator.py:60-62) C |
| `apps/sky/operator.py:38,61,77,83` | cluster launch/serve/down | `_approval` | D2 | user `run` string executes on cluster (approved) |
| `apps/social-autopilot/core.py:482` | Postiz `UPDATE ... state='QUEUE'` (publish) | `_approval` + content-hash binding (:506-522) | D2 | unapproved staging call records the binding anyone can seed |
| `apps/document-editor/operator.py:108,130,144` | doc apply_patch/publish/label | `_approval`; restricted-label convert/export re-verify token (`classification.py:209`) | D2 | only app re-verifying a token in-handler |
| `apps/outreach-engine/core.py:360,376` | Twenty sync; send_all (no-op) | `_approval` | D2 | send_all approval covers whatever is queued at exec time |
| `agent_gateway/write_capabilities.py:49-68` | CRM outreach send, projects change | `CONSEQUENTIAL` -> REQUIRED approval, envelope | D1 | approval never consumed/expired (approvals.py:64); idempotency in-memory; **no live wiring of writes found in projects_api (read-only grants)** C |
| `content/fleet.py:36,68` | social publish via mission | `approval_required` | D2 | see B2 |
| Harness tools flagged `side_effecting` | any | `ApprovalPolicy.decide` (tools.py:73-85) | D3 | gate keys off the tool's *self-declared* `side_effecting` (default False, tools.py:28, :170) |

## C. Cross-cutting facts
- **safe_url.py has zero callers** (`public_url_problem`, safe_url.py:61; grep over repo excluding tests finds only the definition) -> no SSRF guard on `meta_graph image_url`, projects_api source `location`, Salesforce instance URL, WhatsApp media URL (token sent to attacker-influenced URL, channels_whatsapp.py:121), `integration/providers/http.py:57`. Only market-radar has its own `_watch_url_error` (registration-time, rebinding window). C*
- `approval_gate.gated()` (approval_gate.py:16) used by operators in apps/ (support, crm, infra) but not within agentic_os package; it is a bare `inp.get("_approval")`. C
- In-process path trust: `LocalOperatorClient`/`Operator.invoke` accept `_approval` as given; edge-sentinel/response.py:88 injects it from a Governance decision. Anything importing an Operator can forge approval. C
- No `shell=True` in agentic_os; argv lists everywhere (fleet.py:81,87; secrets/bootstrap.py:216; libreoffice_app.py:164). C
- Approvals shipped as `OPERATOR_BASIC_AUTH` static shared secret minting `_approval={"approved":True,"by":"operator-basic"}` (apps/*/core.py: crm 55-83, outreach ~85-97, support ~60-77, billing/app.py:334, books/app.py:372, compliance/app.py:346): no nonce, expiry or input binding. C
