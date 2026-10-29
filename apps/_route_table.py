"""The route table of the 17 apps that join a permissions plane: every route that executes a tool or returns tool/activity data, with the tool it needs; plus the routes
that are PUBLIC BY DESIGN and the framework/operator routes. Shared by the basic-mode tests and the route walk (enterprise gate on, anonymous requests)."""
P, G = "POST", "GET"
START = {"name": "Acme", "product": "p", "icp": "i", "stage": "seed", "problem": "q"}
# (method, path, json body, tool, kind): kind "route" = a 403 JSON body; "console" = a 200 body that says it was denied
ROUTES = {
    "control-tower": [(G, "/api/activity", None, "bi.summary", "route"), (G, "/api/ask?q=hello", None, "bi.ask", "route"),
                      (G, "/", None, "bi.summary", "route"), (P, "/agent/run", {"action": "ask", "question": "x"}, "bi.ask", "route"),
                      (P, "/agent/run", {"action": "refresh"}, "bi.refresh", "route")],
    "growth-assistant": [(P, "/api/chat", {"message": "hello"}, "assistant.ask", "route"), (G, "/api/chat/job/abcd", None, "assistant.ask", "route"),
                         (G, "/api/activity", None, "assistant.ask", "route"), (G, "/api/assets", None, "assistant.ask", "route"),
                         (G, "/api/assets/x", None, "assistant.ask", "route"), (G, "/", None, "assistant.ask", "route"),
                         (P, "/agent/run", {"action": "ask", "q": "x"}, "assistant.ask", "route"),
                         *[(P, "/agent/run", {"action": a, "startup": START}, f"assistant.{a}", "route")
                           for a in ("playbook", "subreddit_plan", "founder_content", "community_blueprint", "cold_outreach", "hire_brief")]],
    "guide": [(P, "/api/ask", {"question": "x"}, "guide.retrieve", "route"), (G, "/api/walkthrough?app=agentic-billing", None, "guide.walkthrough", "route"),
              (G, "/", None, "guide.retrieve", "route")],
    "infra": [], "document-editor": [],
    "outreach-engine": [(G, "/api/pipeline", None, "outreach.refresh", "route"), (G, "/", None, "outreach.refresh", "route"),
                        (P, "/agent/run", {"action": "refresh"}, "outreach.refresh", "route"), (P, "/api/run", {"action": "refresh"}, "outreach.refresh", "route"),
                        (P, "/agent/run", {"action": "approve", "account": "x"}, "outreach.approve", "route"),
                        (P, "/api/run", {"action": "send_all"}, "outreach.send_all", "route"),
                        (P, "/api/discover", {"instruction": "x"}, "outreach.discover", "route"), (G, "/api/synced", None, "outreach.synced", "route"),
                        (P, "/api/agent", {"message": "find leads for rag startups"}, "outreach.discover", "console-ok-false")],
    "social-autopilot": [(G, "/api/activity", None, "social.activity", "route"), (G, "/", None, "social.activity", "route"),
                         (G, "/api/reel/status", None, "social.activity", "route"), (G, "/api/reel/video", None, "social.activity", "route"),
                         (G, "/api/vibex/status", None, "social.activity", "route"),
                         (P, "/api/reel/record", None, "social.record_reel", "route"), (P, "/api/reel/schedule", None, "social.draft", "route"),
                         (P, "/api/vibex/trends", {"topic": "x"}, "social.vibex_trends", "route"),
                         (P, "/api/vibex/generate", {"prompt": "x"}, "social.vibex_generate", "route"),
                         (P, "/api/vibex/draft", {"topic": "x"}, "social.draft", "route"),
                         (P, "/agent/run", {"action": "draft", "topic": "x"}, "social.draft", "route"),
                         (P, "/agent/run", {"action": "publish", "post_id": "x"}, "social.publish", "route"),
                         (P, "/agent/run", {"action": "record_reel"}, "social.record_reel", "route"),
                         (P, "/agent/run", {"action": "vibex_trends"}, "social.vibex_trends", "route"),
                         (P, "/agent/run", {"action": "vibex_generate", "prompt": "x"}, "social.vibex_generate", "route")],
    "agentic-crm": [(G, "/api/activity", None, "pipeline_summary", "route"), (G, "/", None, "pipeline_summary", "route"),
                    *[(P, "/agent/run", {"action": a, "lead": "x"}, t, "route") for a, t in
                      (("score_lead", "crm.score_lead"), ("research", "crm.research"), ("draft_outreach", "crm.draft_outreach"),
                       ("qualify", "crm.qualify"), ("ask", "pipeline_summary"))],
                    (P, "/api/agent", {"message": "pipeline kpi summary"}, "pipeline_summary", "console")],
    "lifecycle": [(G, "/api/activity", None, "lifecycle_summary", "route"), (G, "/", None, "lifecycle_summary", "route"),
                  *[(P, "/agent/run", {"action": a}, t, "route") for a, t in
                    (("compose_campaign", "lifecycle.compose_campaign"), ("segment", "lifecycle.segment"), ("suggest_flow", "lifecycle.suggest_flow"))],
                  (P, "/api/agent", {"message": "recent campaign"}, "list_campaigns", "console")],
    "support": [(G, "/api/activity", None, "inbox_summary", "route"), (G, "/", None, "inbox_summary", "route"),
                *[(P, "/agent/run", {"action": a, "conversation_id": "1"}, t, "route") for a, t in
                  (("draft_reply", "support.draft_reply"), ("resolve", "support.resolve"), ("escalate", "support.escalate"),
                   ("send_onboarding", "support.send_onboarding"))],
                (P, "/api/agent", {"message": "list the queue"}, "list_conversations", "console")],
}

ROUTES.update({
    "books": [(G, "/api/activity", None, "financials_summary", "route"), (G, "/", None, "financials_summary", "route"),
              (P, "/api/agent", {"message": "close status"}, "close_status", "console"),
              *[(P, "/agent/run", {"action": a}, f"books.{a}", "route") for a in ("categorize", "reconcile", "close", "record_revenue", "reverse_entry")]],
    "billing": [(G, "/api/activity", None, "billing_summary", "route"), (G, "/", None, "billing_summary", "route"),
                (P, "/api/agent", {"message": "who is overdue"}, "list_overdue", "console"),
                *[(P, "/agent/run", {"action": a}, t, "route") for a, t in (("chase_overdue", "chase_overdue"), ("refund", "refund"),
                                                                           ("create_subscription", "billing.create_subscription"),
                                                                           ("cancel_subscription", "billing.cancel_subscription"))]],
    "compliance": [(G, "/api/activity", None, "scan_summary", "route"), (G, "/", None, "scan_summary", "route"),
                   (P, "/api/agent", {"message": "failing controls"}, "list_failures", "console"),
                   *[(P, "/agent/run", {"action": a}, f"compliance.{a}", "route") for a in ("scan", "explain", "remediate", "file_consent")]],
    "growth-engine": [(G, "/api/activity", None, "traffic_summary", "route"), (G, "/", None, "traffic_summary", "route"),
                      (P, "/agent/run", {"action": "analyze"}, "growth.analyze", "route"),
                      (P, "/agent/run", {"action": "reallocate_budget"}, "growth.reallocate_budget", "route")],
    "market-radar": [(G, "/api/activity", None, "watch_summary", "route"), (G, "/", None, "watch_summary", "route"),
                     (P, "/agent/run", {"action": "add_watch"}, "radar.add_watch", "route"), (P, "/agent/run", {"action": "brief"}, "radar.brief", "route")],
    "edge-sentinel": [(G, "/api/activity", None, "threat_summary", "route"), (G, "/", None, "threat_summary", "route"),
                      (P, "/api/permissions/query", {}, "vuln_db_permissions", "route"), (G, "/api/permissions/ui", None, "vuln_db_permissions", "route"),
                      (G, "/api/network/review", None, "network_access_review", "route"), (G, "/api/network/ui", None, "network_access_review", "route"),
                      (G, "/api/backups/status", None, "backup_status", "route"), (G, "/api/backups/ui", None, "backup_status", "route"),
                      (G, "/api/cases", None, "threat_summary", "route"), (G, "/api/cases/c1", None, "threat_summary", "route"),
                      (P, "/api/cases/c1/respond", {"request_id": "r"}, "ban_ip", "route"), (P, "/api/cases/c1/execute", {"request_id": "r"}, "ban_ip", "route-or-auth"),
                      (P, "/agent/run", {"action": "block_ip", "ip": "203.0.113.5"}, "ban_ip", "route"),
                      (P, "/agent/run", {"action": "approve_block"}, "ban_ip", "route"),
                      (P, "/agent/run", {"action": "triage"}, "sentinel.triage", "route"),
                      (P, "/agent/run", {"action": "propose_network_policy"}, "propose_network_policy", "route"),
                      (P, "/agent/run", {"action": "approve_network_policy"}, "propose_network_policy", "route")],
    "agentic-privacy": [(G, "/api/activity", None, "privacy_summary", "route"), (G, "/", None, "privacy_summary", "route"),
                        (G, "/audit/verify", None, "privacy_summary", "route"), (P, "/fulfil/r1", {}, "privacy.delete", "route"),
                        (P, "/retention", {}, "privacy.retention", "route"), (P, "/retention", {"confirm": True}, "privacy.delete", "route")],
})

# PUBLIC BY DESIGN: reached by anyone, and carrying no tool-derived data (a liveness answer, a static form, or a data-subject endpoint authenticated by its own
# single-use emailed token). (method, path) -> why.
_LIVE = "liveness only: {status, a connectivity boolean per backend}, no business data"
PUBLIC = {app: {("GET", "/health"): _LIVE} for app in ROUTES}
PUBLIC["agentic-privacy"].update({
    ("POST", "/request"): "public intake (registered in tool_actions.PUBLIC_INTAKE_ROUTES): opens a PENDING intake record and sends one verification e-mail; strict schema, size limit, per-client / per-address / global caps; identity proven later by the emailed single-use token",
    ("GET", "/intake"): "the public data-subject intake form (static HTML)", ("POST", "/intake"): "public intake (registered in tool_actions.PUBLIC_INTAKE_ROUTES): the same constrained submission as /request",
    ("GET", "/verify"): "the confirm page for the emailed single-use link (does not consume the token)", ("POST", "/verify"): "redeems the single-use emailed token",
    ("GET", "/export"): "single-use short-TTL export protected by request id + token issued after /verify"})
PUBLIC["document-editor"][("GET", "/content/{token}")] = "short-lived signed, document-scoped token fetch (the external editor cannot send a bearer); authenticated by the token alone"
# the Operator SDK's own routes (authenticated by the operator boundary; the capabilities themselves are gated by Operator._execute) and the framework's
OPERATOR_ROUTES = {("POST", "/invoke"), ("GET", "/capabilities")}
# agent consoles built on the harness engine (context_runtime AgentConsole): the harness ToolRegistry seam consults the process authorizer with the request's principal
# (``console.respond(principal=_req_principal(request))``); without the engine the route answers that the assistant is offline and executes nothing.
_ENGINE = "agent console on the harness registry seam (process authorizer + the request's principal); offline without the engine"
ENGINE_CONSOLES = {app: {("POST", "/api/agent"): _ENGINE} for app in ("growth-engine", "market-radar", "edge-sentinel", "agentic-privacy")}
# billing's closed-loop HTTP surface: operator-only (HTTP Basic, 503 when unconfigured), "authored as mission operations, never direct core calls": it launches missions
# whose capabilities (billing.dunning ...) are gated at Operator._execute
_LOOP = "operator-only closed-loop surface (HTTP Basic, fail-closed 503); executes through the mission runtime and its operators, gated at Operator._execute"
OPERATOR_AUTH_ROUTES = {"billing": {("POST", "/api/outcomes/observe"): _LOOP, ("POST", "/api/policy/refit"): _LOOP, ("POST", "/api/decide"): _LOOP,
                                    ("POST", "/api/decide/{mission_id}/approve"): _LOOP}}
FRAMEWORK_ROUTES = {("GET", "/_security"), ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"), ("HEAD", "/openapi.json"), ("HEAD", "/docs"),
                    ("HEAD", "/docs/oauth2-redirect"), ("HEAD", "/redoc"), ("HEAD", "/health"), ("HEAD", "/"), ("HEAD", "/api/activity")}
