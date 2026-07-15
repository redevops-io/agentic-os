package mission

// Capability registry — capabilities are syscalls; the planner discovers them by *need*. Apps
// publish a CapabilityManifest; the registry aggregates the fleet and ranks candidates by the cost
// model. The default HashingEmbedder is a dependency-free bag-of-words embedding so discovery works
// deterministically in-process. Go port of agentic_os/mission/registry.py.

import (
	"crypto/md5"
	"math"
	"math/big"
	"regexp"
	"sort"
	"strings"
)

type Embedder interface {
	Embed(text string) []float64
}

// HashingEmbedder is a dependency-free bag-of-words hashing embedding (feature hashing + L2 norm).
type HashingEmbedder struct{ Dim int }

func NewHashingEmbedder() *HashingEmbedder { return &HashingEmbedder{Dim: 256} }

var tokenSplit = regexp.MustCompile(`[^a-z0-9]+`)

func embedTokens(text string) []string {
	var out []string
	for _, t := range tokenSplit.Split(strings.ToLower(text), -1) {
		if t != "" {
			out = append(out, t)
		}
	}
	return out
}

func (e *HashingEmbedder) Embed(text string) []float64 {
	dim := e.Dim
	if dim == 0 {
		dim = 256
	}
	vec := make([]float64, dim)
	modDim := big.NewInt(int64(dim))
	for _, tok := range embedTokens(text) {
		h := md5.Sum([]byte(tok))
		idx := int(new(big.Int).Mod(new(big.Int).SetBytes(h[:]), modDim).Int64())
		vec[idx] += 1.0
	}
	n := 0.0
	for _, x := range vec {
		n += x * x
	}
	if n = math.Sqrt(n); n == 0 {
		n = 1.0
	}
	for i := range vec {
		vec[i] /= n
	}
	return vec
}

// cosine of two L2-normalised vectors.
func cosine(a, b []float64) float64 {
	if len(a) == 0 || len(b) == 0 || len(a) != len(b) {
		return 0.0
	}
	s := 0.0
	for i := range a {
		s += a[i] * b[i]
	}
	return s
}

// Scored is a (capability, similarity) pair from Discover.
type Scored struct {
	Cap   *CapabilitySpec
	Score float64
}

// Registry is the read surface both CapabilityRegistry and PolicyScopedRegistry satisfy, so the
// planner/compiler accept either the full registry or a policy-scoped view.
type Registry interface {
	Get(name string) *CapabilitySpec
	All() []*CapabilitySpec
	Providing(outcome string) []*CapabilitySpec
	Discover(need string, k int) []Scored
}

type CapabilityRegistry struct {
	caps     map[string]*CapabilitySpec
	order    []string // registration order → deterministic All/Providing/Discover ties
	embedder Embedder
}

func NewCapabilityRegistry(embedder Embedder) *CapabilityRegistry {
	if embedder == nil {
		embedder = NewHashingEmbedder()
	}
	return &CapabilityRegistry{caps: map[string]*CapabilitySpec{}, embedder: embedder}
}

func (r *CapabilityRegistry) Register(m CapabilityManifest) {
	for _, cap := range m.Capabilities {
		c := cap // value copy — the registry owns its specs
		if c.Operator == "" {
			c.Operator = m.Operator
		}
		if len(c.Embedding) == 0 {
			c.Embedding = r.embedder.Embed(c.Name + " " + strings.Join(c.Provides, " ") + " " +
				strings.Join(mapKeys(c.Inputs), " ") + " " + strings.Join(mapKeys(c.Outputs), " "))
		}
		if _, seen := r.caps[c.Name]; !seen {
			r.order = append(r.order, c.Name)
		}
		stored := c
		r.caps[c.Name] = &stored
	}
}

func (r *CapabilityRegistry) Get(name string) *CapabilitySpec { return r.caps[name] }

func (r *CapabilityRegistry) All() []*CapabilitySpec {
	out := make([]*CapabilitySpec, 0, len(r.order))
	for _, n := range r.order {
		out = append(out, r.caps[n])
	}
	return out
}

// Providing returns capabilities that declare they can produce outcome (exact provides match).
func (r *CapabilityRegistry) Providing(outcome string) []*CapabilitySpec {
	var out []*CapabilitySpec
	for _, n := range r.order {
		if c := r.caps[n]; strContains(c.Provides, outcome) {
			out = append(out, c)
		}
	}
	return out
}

// Discover ranks capabilities by embedding similarity to a need, best-first, top-k.
func (r *CapabilityRegistry) Discover(need string, k int) []Scored {
	q := r.embedder.Embed(need)
	scored := make([]Scored, 0, len(r.order))
	for _, n := range r.order {
		c := r.caps[n]
		scored = append(scored, Scored{Cap: c, Score: cosine(q, c.Embedding)})
	}
	sort.SliceStable(scored, func(i, j int) bool { return scored[i].Score > scored[j].Score })
	if k >= 0 && k < len(scored) {
		scored = scored[:k]
	}
	return scored
}

// PolicyScopedRegistry is a read-only view filtered to capabilities a principal MAY use — stage 1 of
// the two-stage policy model. Forbidden capabilities are never candidates, so the planner can
// neither plan over them nor leak their existence.
type PolicyScopedRegistry struct {
	base   Registry
	grants map[string]bool
}

func NewPolicyScopedRegistry(base Registry, grants []string) *PolicyScopedRegistry {
	g := map[string]bool{}
	for _, x := range grants {
		g[x] = true
	}
	return &PolicyScopedRegistry{base: base, grants: g}
}

func (p *PolicyScopedRegistry) permitted(c *CapabilitySpec) bool {
	if p.grants["*"] {
		return true
	}
	for _, perm := range c.Permissions {
		if !p.grants[perm] {
			return false
		}
	}
	return true
}

func (p *PolicyScopedRegistry) Get(name string) *CapabilitySpec {
	if c := p.base.Get(name); c != nil && p.permitted(c) {
		return c
	}
	return nil
}

func (p *PolicyScopedRegistry) All() []*CapabilitySpec {
	var out []*CapabilitySpec
	for _, c := range p.base.All() {
		if p.permitted(c) {
			out = append(out, c)
		}
	}
	return out
}

func (p *PolicyScopedRegistry) Providing(outcome string) []*CapabilitySpec {
	var out []*CapabilitySpec
	for _, c := range p.base.Providing(outcome) {
		if p.permitted(c) {
			out = append(out, c)
		}
	}
	return out
}

func (p *PolicyScopedRegistry) Discover(need string, k int) []Scored {
	var out []Scored
	for _, s := range p.base.Discover(need, k*3) { // over-fetch, drop forbidden, then trim
		if p.permitted(s.Cap) {
			out = append(out, s)
		}
	}
	if k >= 0 && k < len(out) {
		out = out[:k]
	}
	return out
}
