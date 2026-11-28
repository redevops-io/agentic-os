# A local agent for a rAIls deployment

`rails_agent.py` runs one question through any OpenAI-compatible model endpoint (for example a self-hosted Qwen on vLLM) with
the tools of the [MCP bridge](../mcp-bridge/README.md). Model endpoint, bridge and deployment can all be on your own
machines; nothing here calls a hosted service.

```bash
RAILS_AGENTS_DIR="<agents directory>" RAILS_TOKEN_FILE="<token file>" \
  python integrations/local-agent/rails_agent.py --base-url http://HOST:PORT/v1 --model NAME --no-thinking "Which projects exist?"
```

The agent is offered exactly the tools the deployment grants its credential. A tool it was not offered is never forwarded,
and a denial from the gateway is passed to the model as a denial.

Verified: with a scripted model against the real gateway (`tests/test_mcp_bridge.py`); and once, by hand, with
Qwen3.8-27B on a local vLLM endpoint against a running desktop build and a connected agent holding `projects.read` and
`missions.read`: it listed the project and its status through two tool calls, and for a question that needed
`sources.search` it answered that it has no such tool. That is one run on one model, not a qualification.
