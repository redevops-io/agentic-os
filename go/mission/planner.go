package mission

// Mission Planner — the LOGICAL layer (goal + world/belief state → an ExecutionIntent of outcomes,
// NOT nodes/apps). The one place the LLM runs; everything below is deterministic. The default
// TemplatePlanner needs no model so the kernel is runnable offline. Go port of planner.py.

import (
	"encoding/json"
	"regexp"
	"strings"
)

// ThinkModel is an injected reasoning model (Qwen3-Coder-Next in prod).
type ThinkModel interface {
	Complete(prompt string) (string, error)
}

// Planner turns a goal into an ExecutionIntent.
type Planner interface {
	Plan(missionID, goal string, context map[string]any) ExecutionIntent
}

// TemplatePlanner matches the goal to a template; falls back to a single-outcome intent. No model.
type TemplatePlanner struct{}

var templateKeywords = map[string][]string{
	"onboarding":       {"onboard", "new customer", "sign up", "signup", "welcome"},
	"invoice_recovery": {"overdue", "unpaid", "invoice", "dunning", "collect payment", "recover payment"},
}

func (p TemplatePlanner) Plan(missionID, goal string, context map[string]any) ExecutionIntent {
	template, _ := context["template"].(string)
	if template == "" {
		template = p.match(goal)
	}
	if template != "" {
		if intent, ok := getTemplate(template, missionID); ok {
			return intent
		}
	}
	step := NewIntentStep(planSlug(goal), goal)
	intent := NewExecutionIntent(missionID, step)
	intent.Rationale = "generic single-outcome plan"
	return intent
}

func (p TemplatePlanner) match(goal string) string {
	g := strings.ToLower(goal)
	for _, name := range []string{"onboarding", "invoice_recovery"} { // deterministic order
		for _, kw := range templateKeywords[name] {
			if strings.Contains(g, kw) {
				return name
			}
		}
	}
	return ""
}

// ModelPlanner asks an injected thinking model for the intent as JSON, validating into an
// ExecutionIntent; falls back to TemplatePlanner if the output can't be parsed.
type ModelPlanner struct {
	Model    ThinkModel
	Fallback Planner
}

const modelPlannerPrompt = "You are the Mission Planner. Decompose the GOAL into a LOGICAL plan of " +
	"outcomes — do NOT name apps or tools, only the outcomes that must be achieved and their " +
	"dependencies.\nReturn STRICT JSON: {\"steps\":[{\"outcome\":str,\"need\":str,\"inputs_from\":[str]," +
	"\"constraints\":[str],\"value_hint\":\"low|medium|high\"}]}.\n\nGOAL: {goal}\n" +
	"Available capability needs (for grounding, do not copy verbatim): {caps}\n"

func NewModelPlanner(model ThinkModel, fallback Planner) *ModelPlanner {
	if fallback == nil {
		fallback = TemplatePlanner{}
	}
	return &ModelPlanner{Model: model, Fallback: fallback}
}

func (p *ModelPlanner) Plan(missionID, goal string, context map[string]any) ExecutionIntent {
	caps := ""
	if hints, ok := context["capability_hints"].([]string); ok {
		caps = strings.Join(hints, ", ")
	}
	prompt := strings.ReplaceAll(strings.ReplaceAll(modelPlannerPrompt, "{goal}", goal), "{caps}", caps)
	raw, err := p.Model.Complete(prompt)
	if err == nil {
		var data struct {
			Steps []struct {
				Outcome     string   `json:"outcome"`
				Need        string   `json:"need"`
				InputsFrom  []string `json:"inputs_from"`
				Constraints []string `json:"constraints"`
				ValueHint   string   `json:"value_hint"`
			} `json:"steps"`
		}
		if json.Unmarshal([]byte(extractJSON(stripThinking(raw))), &data) == nil && len(data.Steps) > 0 {
			steps := make([]IntentStep, 0, len(data.Steps))
			for _, s := range data.Steps {
				step := NewIntentStep(s.Outcome, orDefault(s.Need, s.Outcome))
				step.InputsFrom = s.InputsFrom
				step.Constraints = s.Constraints
				step.ValueHint = orDefault(s.ValueHint, "medium")
				steps = append(steps, step)
			}
			intent := NewExecutionIntent(missionID, steps...)
			intent.Rationale = "model plan"
			return intent
		}
	}
	return p.Fallback.Plan(missionID, goal, context)
}

func planSlug(text string) string {
	if text == "" {
		text = "outcome"
	}
	if s := slugify(text, 40); s != "" {
		return s
	}
	return "outcome"
}

var thinkTag = regexp.MustCompile(`(?s)<think>.*?</think>`)

func stripThinking(text string) string { return strings.TrimSpace(thinkTag.ReplaceAllString(text, "")) }

var jsonObj = regexp.MustCompile(`(?s)\{.*\}`)

func extractJSON(text string) string {
	if m := jsonObj.FindString(text); m != "" {
		return m
	}
	return text
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}
