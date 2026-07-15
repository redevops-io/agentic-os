"""Mission templates — Mission Type -> Template -> Planner fills the gaps.

A template is a partial Execution Intent (the known outcomes + their dependency shape) that
the planner specializes to a concrete instance. Cheaper and more reliable than planning from
scratch, and a reusable, versioned, permissioned asset. `workflows.py` formalized.
"""
from __future__ import annotations

from .types import ExecutionIntent, IntentStep


# Each template is a function mission_id -> ExecutionIntent so step ids are fresh per mission.
def onboarding(mission_id: str) -> ExecutionIntent:
    """New customer onboarding — the seed cross-app mission (was workflows.NEW_CUSTOMER_ONBOARDING).

    subscription -> onboarding_sent ; revenue_recorded -> consent_filed. The revenue/consent
    branch depends on the subscription; onboarding can go in parallel once billed."""
    s_sub = IntentStep(outcome="subscription", need="create a paid subscription for the customer",
                       value_hint="high")
    s_welcome = IntentStep(outcome="onboarding_sent", need="send the customer a welcome/onboarding message",
                           inputs_from=["subscription"], value_hint="medium")
    s_rev = IntentStep(outcome="revenue_recorded", need="record the subscription revenue in the books",
                       inputs_from=["subscription"], value_hint="medium")
    s_consent = IntentStep(outcome="consent_filed", need="file the customer's consent record for compliance",
                           inputs_from=["revenue_recorded"], value_hint="medium",
                           constraints=["must be human-reviewed"])
    return ExecutionIntent(mission_id=mission_id, rationale="onboarding template",
                           steps=[s_sub, s_welcome, s_rev, s_consent])


def invoice_recovery(mission_id: str) -> ExecutionIntent:
    s_find = IntentStep(outcome="overdue_invoice", need="find the customer's overdue invoice", value_hint="high")
    s_reach = IntentStep(outcome="dunning_sent", need="send a payment-reminder to the customer",
                         inputs_from=["overdue_invoice"], value_hint="high",
                         constraints=["never contact a customer twice in 24h"])
    s_book = IntentStep(outcome="payment_recorded", need="record the payment once received",
                        inputs_from=["dunning_sent"], value_hint="high")
    return ExecutionIntent(mission_id=mission_id, rationale="invoice-recovery template",
                           steps=[s_find, s_reach, s_book])


def deploy_app(mission_id: str) -> ExecutionIntent:
    """Deployment as a governed mission (v6 Phase 6.1) — CD run *through* the mission runtime.

    supply-chain scan → terraform plan → [approval] terraform apply → ansible configure → verify.
    The provision step is the highest-consequence capability in the fleet, so it carries the
    approval gate; provision + configure carry saga undos (destroy-delta / rollback-release) so a
    failed verify (or a rejected gate) rolls the deploy back instead of leaving a half-applied stack."""
    s_scan = IntentStep(outcome="image_scanned", value_hint="high",
                        need="supply-chain scan the app image (Trivy/SBOM) for CVEs, secrets and misconfigurations")
    s_plan = IntentStep(outcome="infra_planned", inputs_from=["image_scanned"], value_hint="medium",
                        need="terraform plan the infrastructure delta for the app")
    s_provision = IntentStep(outcome="infra_provisioned", inputs_from=["infra_planned"], value_hint="high",
                        need="terraform apply to provision the app infrastructure",
                        constraints=["highest-consequence — requires human approval"])
    s_configure = IntentStep(outcome="app_configured", inputs_from=["infra_provisioned"], value_hint="high",
                        need="ansible configure and roll out the app operator and its core")
    s_verify = IntentStep(outcome="deploy_verified", inputs_from=["app_configured"], value_hint="high",
                        need="verify the deployed app health and capabilities")
    return ExecutionIntent(mission_id=mission_id, rationale="deploy-app template",
                           steps=[s_scan, s_plan, s_provision, s_configure, s_verify])


def revenue_rescue(mission_id: str) -> ExecutionIntent:
    """Revenue Rescue — recover a failed/late payment across billing → support → lifecycle → books.

    Maps onto the real app operators' capabilities (billing.dunning · support.draft_reply ·
    lifecycle.compose_campaign · books.reconcile). Dunning is the money-moving step, so it carries
    the approval gate; the proactive reach + win-back run in parallel once dunning is approved, and
    the books adjustment folds them back in. (agentic-apps-overview #2.)"""
    s_dun = IntentStep(outcome="dunning_attempted", value_hint="high",
                       need="chase the overdue invoice and retry the failed payment (dunning)",
                       constraints=["money-moving — requires human approval"])
    s_reach = IntentStep(outcome="reply_drafted", inputs_from=["dunning_attempted"], value_hint="medium",
                         need="proactively reach out to the customer about the failed payment")
    s_winback = IntentStep(outcome="campaign_drafted", inputs_from=["dunning_attempted"], value_hint="medium",
                           need="compose a win-back lifecycle campaign for the at-risk customer")
    s_adjust = IntentStep(outcome="reconciliation_staged", inputs_from=["reply_drafted", "campaign_drafted"],
                          value_hint="medium",
                          need="reconcile and adjust the books once the payment is recovered")
    return ExecutionIntent(mission_id=mission_id, rationale="revenue-rescue template",
                           steps=[s_dun, s_reach, s_winback, s_adjust])


def product_launch(mission_id: str) -> ExecutionIntent:
    """Product Launch — ONE mission across EIGHT apps (the killer demo). The value isn't any one
    app; it's the Mission Runtime orchestrating them as a single coherent business process — one
    execution graph, one human approval before it goes live, shared evidence, replayable.

        market-radar.brief → growth-assistant.announce → guide.blog
                                    ↓  (fan out to prep every channel)
          social.draft · lifecycle.email · crm.leads · support.brief
                                    ↓
                  [APPROVAL] social.publish  →  control-tower.track
    """
    research = IntentStep(outcome="competitor_brief", value_hint="high",
                          need="research competitors and the market before the launch")
    announce = IntentStep(outcome="announcement_drafted", inputs_from=["competitor_brief"], value_hint="high",
                          need="generate the launch announcement copy")
    blog = IntentStep(outcome="blog_drafted", inputs_from=["announcement_drafted"],
                      need="create the launch blog post from the knowledge base")
    social = IntentStep(outcome="social_drafted", inputs_from=["announcement_drafted"],
                        need="draft the LinkedIn launch post")
    email = IntentStep(outcome="email_drafted", inputs_from=["announcement_drafted"],
                       need="compose the subscriber launch email campaign")
    leads = IntentStep(outcome="leads_scored", inputs_from=["announcement_drafted"],
                       need="create and score inbound leads for the launch")
    support = IntentStep(outcome="support_briefed", inputs_from=["announcement_drafted"],
                         need="notify and brief the support team about the launch")
    publish = IntentStep(outcome="launch_published", value_hint="high",
                         inputs_from=["blog_drafted", "social_drafted", "email_drafted",
                                      "leads_scored", "support_briefed"],
                         need="publish the launch across all channels",
                         constraints=["public launch — requires human approval before it goes live"])
    track = IntentStep(outcome="conversions_tracked", inputs_from=["launch_published"],
                       need="track launch conversions on the analytics dashboard")
    return ExecutionIntent(mission_id=mission_id, rationale="product-launch template",
                           steps=[research, announce, blog, social, email, leads, support, publish, track])


def teardown_app(mission_id: str) -> ExecutionIntent:
    """Tear-down as a governed mission (v6 Phase 6.1) — the inverse of deploy_app, so you stop
    paying for a deployment you're done with. Roll the release back, then [approval] destroy the
    provisioned infrastructure. The destroy is the highest-consequence step, so it carries the gate."""
    s_rollback = IntentStep(outcome="release_rolled_back", value_hint="medium",
                            need="roll back and stop the running app release before destroying its infrastructure")
    s_destroy = IntentStep(outcome="infra_destroyed", inputs_from=["release_rolled_back"], value_hint="high",
                           need="terraform destroy the app infrastructure to stop incurring cost",
                           constraints=["destroys running infrastructure — requires human approval"])
    return ExecutionIntent(mission_id=mission_id, rationale="teardown-app template",
                           steps=[s_rollback, s_destroy])


def cost_audit(mission_id: str) -> ExecutionIntent:
    """Cost & performance audit as a governed mission (v6 Phase 6.2) — the DevOps Sidekick for the
    'nobody has read the code in 8 months' problem. Engineering decisions usually go un-audited until
    the stack breaks or the bill gets too high; this mission does the looking *continuously*: read the
    slowest page end to end, find the hotspots (N+1 queries, missing indexes, no caching), price the
    waste, then [approval] apply the cheap fix and verify the bill dropped.

    Follows the ordered audit playbook (each stage de-risks the next): inventory/visibility → SLO
    baseline (the error budget is the risk allowance for auto-applying) → cross-dimension waste scan →
    price the findings → auto-apply the safe reversible fixes → [approval] apply the risky/irreversible
    ones → verify. Still contains the real story — the N+1 dashboard (300+ queries/load, no index, no
    cache) is the single biggest finding, ~$9.1k/mo of the total. The risky-apply step changes a live
    database + infra, so it carries the approval gate; the safe-apply step is reversible and runs un-gated."""
    inventory = IntentStep(outcome="inventory_taken", value_hint="medium",
                           need="take a cost & resource inventory — allocation/tagging coverage and the spend map")
    slo = IntentStep(outcome="slo_baselined", inputs_from=["inventory_taken"], value_hint="medium",
                     need="baseline reliability — SLOs and remaining error budget (the risk allowance for auto-applying fixes)")
    scan = IntentStep(outcome="waste_found", inputs_from=["slo_baselined"], value_hint="high",
                      need="scan for waste across dimensions — idle/orphaned, database/query hotspots, k8s rightsizing, drift, stale dependencies")
    estimate = IntentStep(outcome="savings_estimated", inputs_from=["waste_found"], value_hint="high",
                          need="price the findings — monthly and annual, split into safe-to-auto-apply vs must-gate")
    safe = IntentStep(outcome="safe_fixes_applied", inputs_from=["savings_estimated"], value_hint="high",
                      need="auto-apply the safe, reversible fixes (non-prod scheduling, patch dependency bumps)")
    risky = IntentStep(outcome="risky_fixes_applied", inputs_from=["safe_fixes_applied"], value_hint="high",
                       need="apply the gated fixes — the index + cache, rightsizing, orphan cleanup",
                       constraints=["changes a live database + infra — requires human approval"])
    verify = IntentStep(outcome="audit_verified", inputs_from=["risky_fixes_applied"], value_hint="high",
                        need="re-scan and confirm the query count, latency and bill dropped")
    return ExecutionIntent(mission_id=mission_id, rationale="cost-audit template",
                           steps=[inventory, slo, scan, estimate, safe, risky, verify])


TEMPLATES = {
    "onboarding": onboarding,
    "invoice_recovery": invoice_recovery,
    "deploy_app": deploy_app,
    "teardown_app": teardown_app,
    "cost_audit": cost_audit,
    "revenue_rescue": revenue_rescue,
    "product_launch": product_launch,
}


def get(name: str, mission_id: str) -> ExecutionIntent | None:
    fn = TEMPLATES.get(name)
    return fn(mission_id) if fn else None
