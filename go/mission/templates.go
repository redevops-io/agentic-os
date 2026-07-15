package mission

// Mission templates — Mission Type → Template → the planner fills the gaps. A template is a partial
// Execution Intent (known outcomes + dependency shape) specialized per mission. Go port of
// agentic_os/mission/templates.py.

func templateOnboarding(missionID string) ExecutionIntent {
	sSub := NewIntentStep("subscription", "create a paid subscription for the customer")
	sSub.ValueHint = "high"
	sWelcome := NewIntentStep("onboarding_sent", "send the customer a welcome/onboarding message")
	sWelcome.InputsFrom = []string{"subscription"}
	sRev := NewIntentStep("revenue_recorded", "record the subscription revenue in the books")
	sRev.InputsFrom = []string{"subscription"}
	sConsent := NewIntentStep("consent_filed", "file the customer's consent record for compliance")
	sConsent.InputsFrom = []string{"revenue_recorded"}
	sConsent.Constraints = []string{"must be human-reviewed"}
	intent := NewExecutionIntent(missionID, sSub, sWelcome, sRev, sConsent)
	intent.Rationale = "onboarding template"
	return intent
}

func templateInvoiceRecovery(missionID string) ExecutionIntent {
	sFind := NewIntentStep("overdue_invoice", "find the customer's overdue invoice")
	sFind.ValueHint = "high"
	sReach := NewIntentStep("dunning_sent", "send a payment-reminder to the customer")
	sReach.InputsFrom = []string{"overdue_invoice"}
	sReach.ValueHint = "high"
	sReach.Constraints = []string{"never contact a customer twice in 24h"}
	sBook := NewIntentStep("payment_recorded", "record the payment once received")
	sBook.InputsFrom = []string{"dunning_sent"}
	sBook.ValueHint = "high"
	intent := NewExecutionIntent(missionID, sFind, sReach, sBook)
	intent.Rationale = "invoice-recovery template"
	return intent
}

// templateDeployApp — deployment as a governed mission: supply-chain scan → terraform plan →
// [approval] provision → ansible configure → verify. The provision step is highest-consequence.
func templateDeployApp(missionID string) ExecutionIntent {
	sScan := NewIntentStep("image_scanned", "supply-chain scan the app image (Trivy/SBOM) for CVEs, secrets and misconfigurations")
	sScan.ValueHint = "high"
	sPlan := NewIntentStep("infra_planned", "terraform plan the infrastructure delta for the app")
	sPlan.InputsFrom = []string{"image_scanned"}
	sProvision := NewIntentStep("infra_provisioned", "terraform apply to provision the app infrastructure")
	sProvision.InputsFrom = []string{"infra_planned"}
	sProvision.ValueHint = "high"
	sProvision.Constraints = []string{"highest-consequence — requires human approval"}
	sConfigure := NewIntentStep("app_configured", "ansible configure and roll out the app operator and its core")
	sConfigure.InputsFrom = []string{"infra_provisioned"}
	sConfigure.ValueHint = "high"
	sVerify := NewIntentStep("deploy_verified", "verify the deployed app health and capabilities")
	sVerify.InputsFrom = []string{"app_configured"}
	sVerify.ValueHint = "high"
	intent := NewExecutionIntent(missionID, sScan, sPlan, sProvision, sConfigure, sVerify)
	intent.Rationale = "deploy-app template"
	return intent
}

// templateTeardownApp — the inverse of deploy: roll the release back, then [approval] destroy infra.
func templateTeardownApp(missionID string) ExecutionIntent {
	sRollback := NewIntentStep("release_rolled_back", "roll back and stop the running app release before destroying its infrastructure")
	sDestroy := NewIntentStep("infra_destroyed", "terraform destroy the app infrastructure to stop incurring cost")
	sDestroy.InputsFrom = []string{"release_rolled_back"}
	sDestroy.ValueHint = "high"
	sDestroy.Constraints = []string{"destroys running infrastructure — requires human approval"}
	intent := NewExecutionIntent(missionID, sRollback, sDestroy)
	intent.Rationale = "teardown-app template"
	return intent
}

// templateCostAudit — the ordered cost/perf audit playbook: inventory → SLO → scan → price →
// auto-apply safe → [approval] apply risky → verify. Only the risky-apply step gates.
func templateCostAudit(missionID string) ExecutionIntent {
	inventory := NewIntentStep("inventory_taken", "take a cost & resource inventory — allocation/tagging coverage and the spend map")
	slo := NewIntentStep("slo_baselined", "baseline reliability — SLOs and remaining error budget (the risk allowance for auto-applying fixes)")
	slo.InputsFrom = []string{"inventory_taken"}
	scan := NewIntentStep("waste_found", "scan for waste across dimensions — idle/orphaned, database/query hotspots, k8s rightsizing, drift, stale dependencies")
	scan.InputsFrom = []string{"slo_baselined"}
	scan.ValueHint = "high"
	estimate := NewIntentStep("savings_estimated", "price the findings — monthly and annual, split into safe-to-auto-apply vs must-gate")
	estimate.InputsFrom = []string{"waste_found"}
	estimate.ValueHint = "high"
	safe := NewIntentStep("safe_fixes_applied", "auto-apply the safe, reversible fixes (non-prod scheduling, patch dependency bumps)")
	safe.InputsFrom = []string{"savings_estimated"}
	safe.ValueHint = "high"
	risky := NewIntentStep("risky_fixes_applied", "apply the gated fixes — the index + cache, rightsizing, orphan cleanup")
	risky.InputsFrom = []string{"safe_fixes_applied"}
	risky.ValueHint = "high"
	risky.Constraints = []string{"changes a live database + infra — requires human approval"}
	verify := NewIntentStep("audit_verified", "re-scan and confirm the query count, latency and bill dropped")
	verify.InputsFrom = []string{"risky_fixes_applied"}
	verify.ValueHint = "high"
	intent := NewExecutionIntent(missionID, inventory, slo, scan, estimate, safe, risky, verify)
	intent.Rationale = "cost-audit template"
	return intent
}

// templateRevenueRescue — recover a failed/late payment across billing → support ∥ lifecycle → books.
// Dunning is the money-moving (gated) step; the reach-out + win-back fan out, then books folds them in.
func templateRevenueRescue(missionID string) ExecutionIntent {
	sDun := NewIntentStep("dunning_attempted", "chase the overdue invoice and retry the failed payment (dunning)")
	sDun.ValueHint = "high"
	sDun.Constraints = []string{"money-moving — requires human approval"}
	sReach := NewIntentStep("reply_drafted", "proactively reach out to the customer about the failed payment")
	sReach.InputsFrom = []string{"dunning_attempted"}
	sWinback := NewIntentStep("campaign_drafted", "compose a win-back lifecycle campaign for the at-risk customer")
	sWinback.InputsFrom = []string{"dunning_attempted"}
	sAdjust := NewIntentStep("reconciliation_staged", "reconcile and adjust the books once the payment is recovered")
	sAdjust.InputsFrom = []string{"reply_drafted", "campaign_drafted"}
	intent := NewExecutionIntent(missionID, sDun, sReach, sWinback, sAdjust)
	intent.Rationale = "revenue-rescue template"
	return intent
}

// templateProductLaunch — ONE mission across EIGHT apps: research → announce → blog, fan out to
// social ∥ email ∥ leads ∥ support, then [approval] publish → track. One human gate before it goes live.
func templateProductLaunch(missionID string) ExecutionIntent {
	research := NewIntentStep("competitor_brief", "research competitors and the market before the launch")
	research.ValueHint = "high"
	announce := NewIntentStep("announcement_drafted", "generate the launch announcement copy")
	announce.InputsFrom = []string{"competitor_brief"}
	announce.ValueHint = "high"
	blog := NewIntentStep("blog_drafted", "create the launch blog post from the knowledge base")
	blog.InputsFrom = []string{"announcement_drafted"}
	social := NewIntentStep("social_drafted", "draft the LinkedIn launch post")
	social.InputsFrom = []string{"announcement_drafted"}
	email := NewIntentStep("email_drafted", "compose the subscriber launch email campaign")
	email.InputsFrom = []string{"announcement_drafted"}
	leads := NewIntentStep("leads_scored", "create and score inbound leads for the launch")
	leads.InputsFrom = []string{"announcement_drafted"}
	support := NewIntentStep("support_briefed", "notify and brief the support team about the launch")
	support.InputsFrom = []string{"announcement_drafted"}
	publish := NewIntentStep("launch_published", "publish the launch across all channels")
	publish.InputsFrom = []string{"blog_drafted", "social_drafted", "email_drafted", "leads_scored", "support_briefed"}
	publish.ValueHint = "high"
	publish.Constraints = []string{"public launch — requires human approval before it goes live"}
	track := NewIntentStep("conversions_tracked", "track launch conversions on the analytics dashboard")
	track.InputsFrom = []string{"launch_published"}
	intent := NewExecutionIntent(missionID, research, announce, blog, social, email, leads, support, publish, track)
	intent.Rationale = "product-launch template"
	return intent
}

var missionTemplates = map[string]func(string) ExecutionIntent{
	"onboarding":       templateOnboarding,
	"invoice_recovery": templateInvoiceRecovery,
	"deploy_app":       templateDeployApp,
	"teardown_app":     templateTeardownApp,
	"cost_audit":       templateCostAudit,
	"revenue_rescue":   templateRevenueRescue,
	"product_launch":   templateProductLaunch,
}

// getTemplate returns the named template specialized for missionID, or (zero, false).
func getTemplate(name, missionID string) (ExecutionIntent, bool) {
	if fn, ok := missionTemplates[name]; ok {
		return fn(missionID), true
	}
	return ExecutionIntent{}, false
}
