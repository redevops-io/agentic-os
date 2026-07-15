package mission

// Context Runtime — the single resolution door for ALL context (Go port of context.py; ADR:
// context-runtime-context-os). The layering is Application → Mission Runtime → Context Runtime →
// Capabilities → Models → External Systems. The Mission Runtime is pure orchestration and NEVER
// decides retrieval, representation, embeddings, routing or model selection. When it needs context
// — to bind a capability, resolve a belief, check a policy, select a model, retrieve knowledge —
// it states the NEED:
//
//	ContextRuntime.Resolve(ContextIntent{Kind, Goal, World, Mission, ...}) → ContextBundle
//
// and the Context Runtime OWNS the decision. LocalContextRuntime is the dependency-light in-repo
// default (wraps the registry, policy plane, belief fusion); production injects the real engines
// behind the SAME interface. Every resolution carries Provenance — the EXPLAIN surface.

import (
	"regexp"
	"strings"
)

// ── context kinds — every context need the Mission Runtime has ──
const (
	BindCapability    = "bind_capability"    // which capability satisfies this outcome/need
	ResolveBelief     = "resolve_belief"     // the fused belief for a world-state key
	CheckPolicy       = "check_policy"        // the approval decision for a node
	SelectModel       = "select_model"        // which model answers this step
	RetrieveKnowledge = "retrieve_knowledge"  // knowledge, routed across engines by query shape
)

// ContextIntent is a statement of NEED, not a retrieval instruction. The Mission Runtime fills it
// in and hands it to the Context Runtime; it never says how to fetch, represent, embed or route.
type ContextIntent struct {
	Kind    string
	Goal    string
	Need    string
	Outcome string
	Prefer  string // bind: force a specific provider (active-planner enumeration)
	Node    *Node  // policy: the node to gate
	World   *WorldState
	Mission *Mission
	Graph   *ExecutionGraph
	Key     string // belief: the world-state key
	Extra   map[string]any
}

// Provenance is the EXPLAIN surface: which resolver answered, over which representation.
type Provenance struct {
	Resolver       string   // capability-registry · policy-plane · belief-fusion · context-runtime · model-router
	Representation string   // provides · embedding · fused-belief · policy · model · <engine>
	Score          *float64 // nil when not scored
	Alternatives   []string
	Reason         string
}

// ContextBundle is what Resolve returns: the value plus its provenance and any candidates.
type ContextBundle struct {
	Value      any
	Provenance Provenance
	Candidates []any
}

// ContextRuntime is the one door. Production swaps LocalContextRuntime for the real Context
// Runtime — same contract.
type ContextRuntime interface {
	Resolve(intent ContextIntent) ContextBundle
}

// ── knowledge retrieval: one optimizer, routed by query shape ──────────────────────────────────

// KnowledgeRetriever is a retrieval engine (pgvector · HippoRAG · graph · SQL/DuckDB · Elastic ·
// vision). Production wires the real Context Runtime engines behind this; KeywordRetriever is the
// in-repo default.
type KnowledgeRetriever interface {
	Retrieve(query string, k int) []map[string]any
}

// The router IS the optimizer: no single retrieval regime wins across query shapes (temporal ·
// multi-hop · structured · visual · semantic), so pick the engine from the shape of the need. This
// is the seam where the real Context Runtime plugs in a learned/cost-based router; the default is a
// transparent, word-boundary heuristic (so "summarize" is not read as "sum").
type routeRule struct {
	engine string
	res    []*regexp.Regexp
}

func buildRoutes() []routeRule {
	spec := []struct {
		engine string
		kws    []string
	}{
		{"graph", []string{"when", "before", "after", "timeline", "over time", "history", "evolve", "trend"}},         // temporal
		{"hipporag", []string{"relate", "related", "connection", "how does", "multi-hop", "chain", "because",           // multi-hop
			"leads to", "depends on", "path from"}},
		{"analytical", []string{"count", "sum", "average", "group by", "how many", "total", "top", "per", "aggregate"}}, // structured
		{"vision", []string{"image", "photo", "video", "frame", "picture", "visual", "diagram", "screenshot"}},          // multimodal
	}
	rules := make([]routeRule, 0, len(spec))
	for _, s := range spec {
		res := make([]*regexp.Regexp, 0, len(s.kws))
		for _, kw := range s.kws {
			res = append(res, regexp.MustCompile(`\b`+regexp.QuoteMeta(kw)+`\b`))
		}
		rules = append(rules, routeRule{engine: s.engine, res: res})
	}
	return rules
}

var routes = buildRoutes()

var wordRe = regexp.MustCompile(`[a-z0-9]+`)

// RouteRepresentation picks the retrieval engine/representation for a knowledge need and returns
// (engine, why). The reason is part of the EXPLAIN surface so every routing decision is auditable.
func RouteRepresentation(query, hint string) (string, string) {
	if hint != "" {
		return hint, "caller pinned " + hint
	}
	q := strings.ToLower(query)
	for _, r := range routes {
		for i, re := range r.res {
			if re.MatchString(q) {
				return r.engine, "query shape matched " + r.engine + " (on " + kwOf(r, i) + ")"
			}
		}
	}
	return "vector", "no special shape — semantic vector default"
}

// kwOf recovers the matched keyword text for the EXPLAIN reason (the regex is \bkw\b).
func kwOf(r routeRule, i int) string {
	s := r.res[i].String()
	return strings.TrimSuffix(strings.TrimPrefix(s, `\b`), `\b`)
}

// KeywordRetriever is a dependency-free retriever over an in-memory corpus ([]{id,text}) — token-
// overlap scoring, the same role the hashing embedder plays for discovery. Stands in for a real
// engine so retrieval resolves in-process; swap for pgvector / HippoRAG / SQL / … in production.
type KeywordRetriever struct {
	Corpus []map[string]any
}

func (kr KeywordRetriever) Retrieve(query string, k int) []map[string]any {
	qt := tokenSet(query)
	if len(qt) == 0 {
		return nil
	}
	scored := make([]map[string]any, 0, len(kr.Corpus))
	for _, d := range kr.Corpus {
		text, _ := d["text"].(string)
		dt := tokenSet(text)
		ov := 0
		for t := range qt {
			if dt[t] {
				ov++
			}
		}
		if ov > 0 {
			out := map[string]any{}
			for kk, vv := range d {
				out[kk] = vv
			}
			out["score"] = roundN(float64(ov)/float64(len(qt)), 3)
			scored = append(scored, out)
		}
	}
	sortByScoreDesc(scored)
	if k > 0 && len(scored) > k {
		scored = scored[:k]
	}
	return scored
}

func tokenSet(s string) map[string]bool {
	out := map[string]bool{}
	for _, w := range wordRe.FindAllString(strings.ToLower(s), -1) {
		out[w] = true
	}
	return out
}

func sortByScoreDesc(rows []map[string]any) {
	for i := 1; i < len(rows); i++ { // stable insertion sort — corpora here are small
		for j := i; j > 0; j-- {
			if rows[j]["score"].(float64) <= rows[j-1]["score"].(float64) {
				break
			}
			rows[j], rows[j-1] = rows[j-1], rows[j]
		}
	}
}

// LocalContextRuntime is the in-repo default resolver over the capability registry, policy plane
// and belief fusion. Not a real optimizer — but the same interface as production, so wiring the
// real Context Runtime is a swap.
type LocalContextRuntime struct {
	Registry    Registry
	PolicyPlane *PolicyDecisionPlane
	Model       string
	Retrievers  map[string]KnowledgeRetriever // engine → retriever (production injects the real engines)
}

// NewLocalContextRuntime wraps a registry; policy plane, model and retrievers are optional.
func NewLocalContextRuntime(registry Registry) *LocalContextRuntime {
	return &LocalContextRuntime{Registry: registry, Model: "local:template"}
}

func (lc *LocalContextRuntime) Resolve(intent ContextIntent) ContextBundle {
	switch intent.Kind {
	case BindCapability:
		return lc.bind(intent)
	case CheckPolicy:
		return lc.policy(intent)
	case ResolveBelief:
		return lc.belief(intent)
	case RetrieveKnowledge:
		return lc.retrieve(intent)
	case SelectModel:
		model := lc.Model
		if model == "" {
			model = "local:template"
		}
		return ContextBundle{Value: model, Provenance: Provenance{
			Resolver: "model-router", Representation: "model", Reason: "configured model " + model}}
	default:
		return ContextBundle{Provenance: Provenance{Resolver: "context-runtime",
			Reason: "unknown context kind " + intent.Kind}}
	}
}

// ── resolver: knowledge retrieval, routed across engines by query shape ──
func (lc *LocalContextRuntime) retrieve(intent ContextIntent) ContextBundle {
	query := intent.Need
	if query == "" {
		query = intent.Goal
	}
	hint, _ := intent.Extra["representation"].(string)
	engine, why := RouteRepresentation(query, hint)
	retriever, ok := lc.Retrievers[engine]
	if !ok {
		return ContextBundle{Value: []map[string]any{}, Provenance: Provenance{
			Resolver: "context-runtime", Representation: engine,
			Reason: "routed to " + engine + " — " + why + "; no " + engine +
				" retriever wired (inject the real Context Runtime)"}}
	}
	k := 5
	if kv, ok := intent.Extra["k"].(int); ok {
		k = kv
	}
	results := retriever.Retrieve(query, k)
	alts := []string{}
	for _, r := range routes {
		if r.engine != engine {
			alts = append(alts, r.engine)
		}
	}
	var score *float64
	cands := make([]any, 0, len(results))
	for _, r := range results {
		cands = append(cands, r)
	}
	if len(results) > 0 {
		if s, ok := results[0]["score"].(float64); ok {
			score = &s
		}
	}
	return ContextBundle{Value: results, Candidates: cands, Provenance: Provenance{
		Resolver: "context-runtime", Representation: engine, Score: score,
		Alternatives: alts, Reason: "routed to " + engine + " — " + why}}
}

// ── resolver: capability binding (the logic the compiler routes through) ──
func (lc *LocalContextRuntime) bind(intent ContextIntent) ContextBundle {
	candidates := lc.Registry.Providing(intent.Outcome)
	rep := "provides"
	var score *float64
	if len(candidates) == 0 {
		rep = "embedding"
		for _, s := range lc.Registry.Discover(intent.Need, 3) {
			if s.Score >= minBindScore {
				candidates = append(candidates, s.Cap)
				if score == nil {
					sc := s.Score
					score = &sc
				}
			}
		}
	}
	if len(candidates) == 0 {
		return ContextBundle{Provenance: Provenance{Resolver: "capability-registry",
			Representation: rep, Reason: "no capability provides '" + intent.Outcome + "'"}}
	}
	var chosen *CapabilitySpec
	if intent.Prefer != "" {
		for _, c := range candidates {
			if c.Name == intent.Prefer {
				chosen = c
				break
			}
		}
	}
	if chosen == nil {
		chosen = rankCandidates(candidates)[0]
	}
	alts := []string{}
	for _, c := range candidates {
		if c.Name != chosen.Name {
			alts = append(alts, c.Name)
		}
		if len(alts) >= 4 {
			break
		}
	}
	reason := "top of candidates by cost"
	if rep == "provides" && len(candidates) == 1 {
		reason = "sole provider of '" + intent.Outcome + "'"
	}
	cands := make([]any, 0, len(candidates))
	for _, c := range candidates {
		cands = append(cands, c)
	}
	return ContextBundle{Value: chosen, Candidates: cands, Provenance: Provenance{
		Resolver: "capability-registry", Representation: rep, Score: score,
		Alternatives: alts, Reason: reason}}
}

// ── resolver: policy decision ──
func (lc *LocalContextRuntime) policy(intent ContextIntent) ContextBundle {
	d := lc.PolicyPlane.Decide(intent.Node, intent.World, intent.Mission, intent.Graph)
	reason := "no gate"
	if d.Required {
		reason = "approval required"
	}
	return ContextBundle{Value: d, Provenance: Provenance{
		Resolver: "policy-plane", Representation: "policy", Reason: reason}}
}

// ── resolver: belief (state estimation) ──
func (lc *LocalContextRuntime) belief(intent ContextIntent) ContextBundle {
	b := intent.World.Belief(intent.Key)
	reason := "fused belief"
	var score *float64
	if b != nil {
		sc := b.Confidence
		score = &sc
		if b.Conflict {
			reason = "conflicting sources"
		}
	}
	return ContextBundle{Value: b, Provenance: Provenance{
		Resolver: "belief-fusion", Representation: "fused-belief", Score: score, Reason: reason}}
}
