package mission

// P9b — three learners kept SEPARATE, behind a promotion ladder. Routing / model-selection /
// plan-policy have distinct feedback signals; none mutates production until a policy promotes it
// (Observe → Recommend → Shadow → Policy-constrained promotion). Go port of learners.py.

// Recommendation is a proposed option change with the evidence backing it.
type Recommendation struct {
	Kind        string  `json:"kind"`
	Subject     string  `json:"subject"`
	Current     string  `json:"current"`
	Recommended string  `json:"recommended"`
	Support     int     `json:"support"`
	Lift        float64 `json:"lift"`
	Status      string  `json:"status"`
	ID          string  `json:"id"`
}

func newRecommendation(kind, subject, current, recommended string, support int, lift float64) Recommendation {
	return Recommendation{Kind: kind, Subject: subject, Current: current, Recommended: recommended,
		Support: support, Lift: lift, Status: "recommended", ID: newID("rec")}
}

// statTable tracks success rate + support per (subject, option), preserving first-seen order.
type statTable struct {
	s         map[string]map[string][]int
	subjOrder []string
	optOrder  map[string][]string
}

func newStatTable() *statTable {
	return &statTable{s: map[string]map[string][]int{}, optOrder: map[string][]string{}}
}

func (t *statTable) record(subject, option string, ok bool) {
	if _, e := t.s[subject]; !e {
		t.s[subject] = map[string][]int{}
		t.subjOrder = append(t.subjOrder, subject)
	}
	if _, e := t.s[subject][option]; !e {
		t.optOrder[subject] = append(t.optOrder[subject], option)
	}
	t.s[subject][option] = append(t.s[subject][option], b2i(ok))
}

func (t *statTable) hist(subject, option string) []int { return t.s[subject][option] }

// Recommender observes outcomes and recommends a better option once evidence shows a real lift.
type Recommender struct {
	kind       string
	tbl        *statTable
	MinSupport int
	MinLift    float64
}

func newRecommender(kind string) *Recommender {
	return &Recommender{kind: kind, tbl: newStatTable(), MinSupport: 5, MinLift: 0.1}
}

func (r *Recommender) Observe(subject, option string, ok bool) { r.tbl.record(subject, option, ok) }

func (r *Recommender) Recommend() []Recommendation {
	var recs []Recommendation
	for _, subj := range r.tbl.subjOrder {
		opts := r.tbl.optOrder[subj]
		if len(opts) < 2 {
			continue
		}
		current := opts[0] // production = the most-used option (first-seen wins ties)
		for _, o := range opts {
			if len(r.tbl.hist(subj, o)) > len(r.tbl.hist(subj, current)) {
				current = o
			}
		}
		curRate := avgInts(r.tbl.hist(subj, current))
		var best *Recommendation
		for _, opt := range opts {
			hist := r.tbl.hist(subj, opt)
			if opt == current || len(hist) < r.MinSupport {
				continue
			}
			lift := roundN(avgInts(hist)-curRate, 3)
			if lift >= r.MinLift && (best == nil || lift > best.Lift) {
				rec := newRecommendation(r.kind, subj, current, opt, len(hist), lift)
				best = &rec
			}
		}
		if best != nil {
			recs = append(recs, *best)
		}
	}
	return recs
}

// LearningStack holds the three learners and the promotion gate. Recommendations are shadow-only
// until a policy promotes them — the one place production routing can change.
type LearningStack struct {
	Routing          *Recommender
	Models           *Recommender
	Plans            *Recommender
	AllowAutoPromote bool
	promoted         map[[2]string]string
}

func NewLearningStack() *LearningStack {
	return &LearningStack{
		Routing: newRecommender("routing"), Models: newRecommender("model_selection"),
		Plans: newRecommender("plan_policy"), promoted: map[[2]string]string{},
	}
}

func (s *LearningStack) Recommendations() []Recommendation {
	var out []Recommendation
	for _, lr := range []*Recommender{s.Routing, s.Models, s.Plans} {
		for _, r := range lr.Recommend() {
			key := [2]string{r.Kind, r.Subject}
			if s.promoted[key] == r.Recommended {
				r.Status = "promoted"
			} else if s.AllowAutoPromote {
				r.Status = "shadow"
			}
			out = append(out, r)
		}
	}
	return out
}

// Promote is the ONLY path that changes production routing.
func (s *LearningStack) Promote(rec *Recommendation, actor string) *Recommendation {
	s.promoted[[2]string{rec.Kind, rec.Subject}] = rec.Recommended
	rec.Status = "promoted"
	return rec
}

func (s *LearningStack) PromotedChoice(kind, subject string) (string, bool) {
	v, ok := s.promoted[[2]string{kind, subject}]
	return v, ok
}
