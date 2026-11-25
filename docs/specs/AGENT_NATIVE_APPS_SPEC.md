# Workstream B — Agent-Native Apps and Artifact Compatibility

Owner specification, received 2026-11-25. This is the authoritative implementation plan for this workstream; the text below is the owner's, reformatted as Markdown only (headings and lists), with nothing added or removed. Part of "rAIls Continual Self-Improvement and Agent-Native Application Compatibility"; the other workstream's specification is in agent-harness `docs/specs/SELF_IMPROVEMENT_MEMORY_SPEC.md`.

Study Anthropic's new installable Artifacts capabilities:

- https://claude.com/resources/articles/install-as-app
- https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop
Audit the existing Governed Agent Gateway, MCP integrations, capability registry, application APIs, Mission SDK, local runtime interfaces, Projects UI, desktop launcher and WASM distribution.

## Implementation

1. Provide a complete machine-readable capability manifest for every rAIls App, covering all applicable business functionality currently available through its UI or API.
2. Reuse the Governed Agent Gateway as the common invocation path.
3. Support cloud-hosted rAIls Apps through authenticated remote MCP and APIs.
4. Support on-premises and locally installed rAIls Apps through the native rAIls Runtime and SDK. Provide a local MCP bridge for agents that require MCP.
5. Package a Claude Desktop extension using MCPB where supported.
6. Implement and verify Claude Artifacts compatibility, including installed artifacts that use both Claude-native functionality and rAIls capabilities.
7. Implement equivalent integration paths for Gemini, Grok, Muse and other compatible external agents. Discover actual supported interfaces rather than assuming feature parity.
8. Support agent-generated dashboards, forms, editors, notifications, application state and cross-app workflows.
9. Provide native desktop integrations for notifications, tray/menu-bar features, global shortcuts and scoped filesystem access where required.
10. Keep credentials, persistent business state, background missions and governed execution in the runtime, not embedded in generated UI code.
11. Support existing applications regardless of whether they run locally, on-premises or in the cloud.
12. Preserve governance in the source-available edition and enterprise-specific multi-user permissioning in the enterprise edition.
13. Ensure an external agent cannot bypass policy by changing its UI, transport or invocation interface.
14. Produce portable, versioned application definitions and capability contracts without requiring a particular model provider or proprietary artifact format.
15. Reuse the existing lightweight WASM and desktop distribution work.

## Acceptance

Demonstrate working applications built and operated by the actual external agents:

- Multi-project QA and release-readiness dashboard.
- CRM, accounting and procurement assistant.
- Edge Sentinel security investigation and governed remediation interface.
- VibeXGen and Social Media content-generation workflow.
- Market Intelligence research and monitoring application.
For Claude, verify the actual Artifact → Install as app workflow, not just MCP connectivity from ordinary chat.

Test cloud and local deployments, authentication, state persistence, notifications, cross-app actions, permission refusals, provider changes, updates, failure recovery and offline functionality where applicable.

Use UI Audit and UI Test Agent from agentic-tests and existing QA evidence reporting.

Publish working integrations, examples, benchmark results, documentation and demonstrations. Update redevops.io and redevops.app only with verified capabilities.

## Delivery

Treat both workstreams as implementation and release tasks, not standalone research exercises.

Audit before introducing new components. Reuse existing infrastructure. Fix verified gaps, run real acceptance tests, publish the applicable releases and provide reproducible evidence.

Proceed directly without delegation. Routine engineering, merges, releases and previously authorized deployments remain authorized. Preserve required automated checks and application-level governance. Escalate only genuine external blockers.
