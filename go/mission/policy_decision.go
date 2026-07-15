package mission

// P8 — the Policy & Human-Decision Plane. Approval is a decision, not a static flag: the gate is the
// OR of Mandatory-capability, Mission-policy and Dynamic-risk rules (+ verification escalation,
// handled in the runtime). The dynamic decision weighs reversibility, uncertainty, monetary value,
// permissions, regulatory sensitivity, novelty, blast radius and verification coverage, and builds
// an evidence-backed approval packet. Go port of agentic_os/mission/policy_decision.py.

import (
	"math"
	"strings"
)

var regulatoryMarkers = []string{"pii", "gdpr", "hipaa", "kyc", "aml", "legal", "compliance",
	"consent", "sanction", "tax", "medical", "financial-report"}

type PolicyDecisionPlane struct {
	registry      Registry
	learning      *LearningRouter
	grantedOf     func(*Mission) map[string]bool
	RiskThreshold float64
	evidence      *EvidenceLog
	markers       []string
}

func NewPolicyDecisionPlane(registry Registry, learning *LearningRouter,
	grantedOf func(*Mission) map[string]bool, evidence *EvidenceLog) *PolicyDecisionPlane {
	if grantedOf == nil {
		grantedOf = defaultGrantedOf
	}
	return &PolicyDecisionPlane{
		registry: registry, learning: learning, grantedOf: grantedOf,
		RiskThreshold: 0.5, evidence: evidence, markers: regulatoryMarkers,
	}
}

func defaultGrantedOf(m *Mission) map[string]bool {
	s := map[string]bool{}
	for _, g := range m.PolicyRefs {
		s[g] = true
	}
	return s
}

// Assess weighs the dimensions a single risk×value threshold can't capture.
func (p *PolicyDecisionPlane) Assess(node *Node, world *WorldState, mission *Mission, graph *ExecutionGraph) RiskFactors {
	cap := p.registry.Get(node.Capability)
	reversible := node.Undo != "" || (cap != nil && cap.Undo != "")
	reversibility := 0.0
	if reversible || !node.SideEffecting {
		reversibility = 1.0
	}

	confs := []float64{0.9}
	if cap != nil {
		confs[0] = cap.Confidence
	}
	learned, hasLearned := 0.0, false
	if p.learning != nil {
		learned, hasLearned = p.learning.CapabilityConfidence(node.Capability)
	}
	if hasLearned {
		confs = append(confs, learned)
	}
	for _, v := range node.Inputs {
		if fw, ok := v.(map[string]any); ok && world != nil {
			if key, ok := fw["$from_world"].(string); ok {
				if b := world.Belief(key); b != nil {
					confs = append(confs, b.Confidence)
				}
			}
		}
	}
	uncertainty := roundN(1.0-minFloat(confs), 3)

	budgetUSD, nodeUSD := mission.Budget.USD, node.Cost.USD
	monetary := 0.0
	if budgetUSD != 0 {
		monetary = roundN(math.Min(1.0, nodeUSD/budgetUSD), 3)
	} else if nodeUSD != 0 {
		monetary = 1.0
	}

	novelty := 0.7
	if hasLearned {
		novelty = roundN(1.0-learned, 3)
	}

	blastRadius := 0.0
	if graph != nil {
		blastRadius = roundN(p.downstream(node, graph), 3)
	}

	coverage := 0.0
	if !node.SideEffecting || needsVerification(node) {
		coverage = 1.0
	}

	granted := p.grantedOf(mission)
	permissioned := granted["*"] || cap == nil
	if !permissioned {
		permissioned = true
		for _, perm := range cap.Permissions {
			if !granted[perm] {
				permissioned = false
				break
			}
		}
	}
	regulatory := p.regulatory(node, cap)

	score := 0.35*(1.0-reversibility) + 0.15*uncertainty + 0.15*monetary +
		0.10*novelty + 0.15*blastRadius + 0.10*(1.0-coverage)
	if !permissioned {
		score = 1.0
	} else if regulatory {
		score = math.Max(score, 0.8)
	}
	return RiskFactors{
		Reversibility: reversibility, Uncertainty: uncertainty, Monetary: monetary,
		Novelty: novelty, BlastRadius: blastRadius, VerificationCoverage: coverage,
		Permissioned: permissioned, Regulatory: regulatory, Score: roundN(math.Min(1.0, score), 3),
	}
}

// Decide is the OR of the rules → an ApprovalDecision (with an evidence-backed packet if required).
func (p *PolicyDecisionPlane) Decide(node *Node, world *WorldState, mission *Mission, graph *ExecutionGraph) ApprovalDecision {
	cap := p.registry.Get(node.Capability)
	var rules []string
	if node.ApprovalRequired || (cap != nil && cap.ApprovalRequired) {
		rules = append(rules, string(RuleMandatory))
	}
	if p.missionPolicyRequires(mission, node) {
		rules = append(rules, string(RuleMissionPolicy))
	}
	risk := p.Assess(node, world, mission, graph)
	if risk.Score >= p.RiskThreshold || risk.Regulatory || !risk.Permissioned {
		rules = append(rules, string(RuleDynamicRisk))
	}
	required := len(rules) > 0
	d := NewApprovalDecision(required)
	d.Rules = rules
	d.Risk = risk
	if required {
		d.Packet = p.packet(node, mission, risk, rules, graph)
	} else {
		d.Packet = map[string]any{}
	}
	return d
}

func (p *PolicyDecisionPlane) packet(node *Node, mission *Mission, risk RiskFactors, rules []string, graph *ExecutionGraph) map[string]any {
	cap := p.registry.Get(node.Capability)
	var alts []string
	if node.Produces != "" {
		for _, c := range p.registry.Providing(node.Produces) {
			if c.Name != node.Capability {
				alts = append(alts, c.Name)
			}
		}
	}
	var ledger []map[string]any
	if p.evidence != nil {
		ledger = p.evidence.Ledger(mission.ID)
	}
	var unresolved, supporting []map[string]any
	for _, e := range ledger {
		if e["type"] == "ClaimRaised" {
			matched := false
			for _, v := range ledger {
				if v["type"] == "EvidenceRecorded" && v["claim_id"] == e["id"] {
					matched = true
					break
				}
			}
			if !matched {
				unresolved = append(unresolved, e)
			}
		} else {
			supporting = append(supporting, e)
		}
	}
	undo := node.Undo
	if undo == "" && cap != nil {
		undo = cap.Undo
	}
	rollback := "no side effect"
	if undo != "" {
		rollback = "undo via '" + undo + "'"
	} else if node.SideEffecting {
		rollback = "IRREVERSIBLE — no rollback"
	}
	return map[string]any{
		"proposed_action": map[string]any{"capability": node.Capability, "operator": node.Operator,
			"produces": node.Produces, "side_effecting": node.SideEffecting},
		"why": rules,
		"expected_impact": map[string]any{"monetary_usd": node.Cost.USD,
			"downstream_nodes": p.downstreamCount(node, graph), "produces": node.Produces},
		"supporting_evidence": supporting,
		"unresolved_claims":   unresolved,
		"alternatives":        alts,
		"confidence":          roundN(1.0-risk.Uncertainty, 3),
		"rollback":            rollback,
		"risk":                jsonableMap(risk),
	}
}

func (p *PolicyDecisionPlane) missionPolicyRequires(mission *Mission, node *Node) bool {
	cap := p.registry.Get(node.Capability)
	reversible := node.Undo != "" || (cap != nil && cap.Undo != "")
	for _, c := range mission.Constraints {
		if !strings.HasPrefix(c, "approval:") {
			continue
		}
		scope := c[len("approval:"):]
		switch {
		case scope == "all":
			return true
		case scope == "side_effecting" && node.SideEffecting:
			return true
		case scope == "irreversible" && node.SideEffecting && !reversible:
			return true
		case strings.HasPrefix(scope, "cap:") && scope[len("cap:"):] == node.Capability:
			return true
		}
	}
	return false
}

func (p *PolicyDecisionPlane) regulatory(node *Node, cap *CapabilitySpec) bool {
	perms := ""
	if cap != nil {
		perms = strings.Join(cap.Permissions, " ")
	}
	blob := strings.ToLower(node.Capability + " " + node.Operator + " " + perms)
	for _, mk := range p.markers {
		if strings.Contains(blob, mk) {
			return true
		}
	}
	return false
}

// dependents is the transitive set of node ids that depend on node (bounded DAG fixpoint).
func (p *PolicyDecisionPlane) dependents(node *Node, graph *ExecutionGraph) map[string]bool {
	seen := map[string]bool{}
	frontier := map[string]bool{node.ID: true}
	for range graph.Nodes {
		grew := false
		for i := range graph.Nodes {
			cand := &graph.Nodes[i]
			if seen[cand.ID] {
				continue
			}
			for _, dep := range cand.DependsOn {
				if frontier[dep] {
					seen[cand.ID] = true
					frontier[cand.ID] = true
					grew = true
					break
				}
			}
		}
		if !grew {
			break
		}
	}
	return seen
}

func (p *PolicyDecisionPlane) downstreamCount(node *Node, graph *ExecutionGraph) int {
	if graph == nil {
		return 0
	}
	return len(p.dependents(node, graph))
}

func (p *PolicyDecisionPlane) downstream(node *Node, graph *ExecutionGraph) float64 {
	total := len(graph.Nodes) - 1
	if total < 1 {
		total = 1
	}
	return math.Min(1.0, float64(p.downstreamCount(node, graph))/float64(total))
}
