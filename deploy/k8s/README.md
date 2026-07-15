# Local LLM for the agents (self-hosted)

The agent layer can use a **local OpenAI-compatible LLM** instead of (or before) a paid API — e.g.
a `llama.cpp`/`ollama` model on your own box. `redevops-llm-svc.yaml` is an example of exposing one
to the agents as a dedicated Kubernetes service:

- a selector-less `Service` (type `LoadBalancer`) + `Endpoints` that map a load-balancer IP to a
  host running an OpenAI-compatible server. It gets its own address/API, isolated from other
  workloads.

## Wiring the agents
Each agent's `_llm_blurb()` (the optional LLM narration on `/agent/run`) prefers a local
OpenAI-compatible endpoint when these env vars are set, then falls back to a hosted API, then to a
deterministic blurb — so it never breaks if the LLM is down:

```
REDEVOPS_LLM_BASE_URL=http://<llm-host>:8000/v1
REDEVOPS_LLM_MODEL=<your-model>
```

Set them per agent service. The LLM is only called on `/agent/run` (agentic actions), never on
`/api/activity` (dashboards), so dashboards stay fast even when the model is a modest CPU host.
