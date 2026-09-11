"""Sidekick's stack knowledge — authoritative, deterministic answers about how the ReDevOps
agentic app stack handles credentials, where data is processed, and how execution is governed.

Nobody should have to read a manual. Sidekick answers these questions in-product, and every
answer here is a **curated fact** about the real mechanisms (not a model paraphrase), so
security- and privacy-sensitive claims are exact and cannot drift or hallucinate. Each entry
points at where the behaviour is actually enforced in the codebase.

Deterministic and model-free: :func:`answer_stack_question` scores a question against each
entry's trigger terms and returns the best match (or ``None``), so the same question always
yields the same authoritative answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    topic: str
    keywords: Tuple[str, ...]     # lowercased trigger terms/phrases
    question: str                 # canonical phrasing (drives an in-UI "what can I ask" menu)
    answer: str                   # authoritative, business-readable but exact (the overview tier)
    source: str = ""              # where the behaviour is defined/enforced
    detail: str = ""              # optional deeper/technical tier (also grounds the LLM fallback)


STACK_KNOWLEDGE: Tuple[KnowledgeEntry, ...] = (
    KnowledgeEntry(
        id="credentials",
        topic="Credentials",
        keywords=("credential", "credentials", "how are my creds", "my creds", "password",
                  "secret", "secrets", "token", "tokens", "api key", "where do my", "stored",
                  "store my"),
        question="How are my credentials handled?",
        answer=(
            "Three separate owners, kept on separate sides of a boundary. (1) The OAuth "
            "*app secret* (client id/secret) lives only in the deployment's connect layer — it "
            "never enters a Mission, a plan, the AI model, or telemetry. (2) When you Connect, "
            "your *account token* is fetched through the provider's own consent screen (never "
            "pasted) and held in the CredentialBroker behind an opaque reference. (3) A Mission — "
            "and Sidekick, and the model — only ever receive that *reference*; it's resolved to "
            "the real token at the moment of the call and never logged. So no secret is ever "
            "visible to the model or written to a log."),
        source="integrations/hosted_oauth.py (client secret) · CredentialBroker (token) · "
               "productivity.CapabilityGrant (Mission gets a ref only)"),
    KnowledgeEntry(
        id="secrets-to-model",
        topic="Credentials",
        keywords=("model see", "llm see", "ai see", "does the model", "does the ai", "does the llm",
                  "sent to the model", "sent to the ai", "in the prompt", "leak", "exposed to the",
                  "see my token", "see my secret"),
        question="Does the AI model ever see my secrets or tokens?",
        answer=(
            "No. The model plans against capability *references*, never secrets. Access tokens "
            "and client secrets are resolved by the CredentialBroker at execution time — after "
            "planning, outside the model's context — and are kept out of prompts, plans and "
            "telemetry."),
        source="CredentialBroker resolves at egress · CapabilityGrant carries a credential_ref"),
    KnowledgeEntry(
        id="data-locality",
        topic="Data handling",
        keywords=("locally or in the cloud", "local or cloud", "local or in the cloud",
                  "where is my data", "where's my data", "data processed", "processed locally",
                  "leave my machine", "leaves my machine", "on my machine", "my data go"),
        question="Is my data processed locally or in the cloud?",
        answer=(
            "It depends on the operation, and you can tell from the app. Cloud suites (Google "
            "Workspace, Microsoft 365) send only the specific request you authorized to that "
            "provider's cloud API. Local operations — LibreOffice, desktop Office, Apple iWork, "
            "and raw file edits — run on your own machine through a local connector, so those "
            "documents never leave the box. Indexing/retrieval of your content runs on your own "
            "Context Runtime (your infrastructure), not a third party."),
        source="productivity.PhysicalStrategy: CLOUD_API vs FILE_NATIVE / LOCAL_HEADLESS / "
               "LOCAL_DESKTOP_AUTOMATION"),
    KnowledgeEntry(
        id="model-locality",
        topic="Data handling",
        keywords=("where does the ai run", "where does the model run", "model run", "ai run",
                  "inference", "local model", "cloud model", "run on my", "ollama", "pair",
                  "which model", "own hardware"),
        question="Where does the AI run — my hardware or the cloud?",
        answer=(
            "Provider-agnostic. It uses whatever inference you have — a local model (NVIDIA PAIR, "
            "Ollama, a local vLLM) or a cloud provider — and cloud inference happens *only when "
            "Mission policy allows it*. Where a request runs is a governed, recorded decision, so "
            "you can keep sensitive work on your own hardware and let policy decide when it may "
            "leave."),
        source="Mission policy plane (model resolution ladder; cloud gated by policy)"),
    KnowledgeEntry(
        id="connect-scope",
        topic="Access & scope",
        keywords=("connect google", "connect microsoft", "connect my", "ingest", "see my drive",
                  "see my files", "see my email", "whole drive", "entire drive", "read everything",
                  "read all my", "mailbox", "what can it see", "what can the app see", "scope"),
        question="If I connect Google/Microsoft, what can it see — does it ingest everything?",
        answer=(
            "Connecting is scoped and split in two, and it does NOT ingest your whole account. As "
            "an App it can only take the actions you granted (create/update/send), each "
            "least-privilege. As a Source it reads only the folders or labels you pick. Scopes are "
            "least-privilege profiles — e.g. Google's drive.file sees only files the app created "
            "or you explicitly opened; reading an existing corpus takes a separate, explicit "
            "read-only grant you choose."),
        source="productivity.ProviderRole APP/SOURCE · ScopeProfile · sources.SourceGrant"),
    KnowledgeEntry(
        id="governance",
        topic="Governance",
        keywords=("dangerous", "auto-execute", "auto execute", "automatically", "without asking",
                  "without approval", "run on its own", "delete my", "move money", "guardrail",
                  "what stops", "is it safe", "safe", "gate"),
        question="What stops it from doing something dangerous — is anything auto-executed?",
        answer=(
            "Every side-effecting action runs under a GovernedEnvelope, is risk-tiered, and "
            "high-consequence actions (moving money, provisioning, deleting) park on a human "
            "approval gate — nothing mutating runs on its own. After a step runs it is re-observed "
            "to confirm it really happened, and if a step fails a saga unwinds the earlier ones. "
            "Access is deny-by-default: an app gets only what it was granted."),
        source="integrations/execution.py GovernedEnvelope · tiers + approval gate · saga compensation"),
    KnowledgeEntry(
        id="revoke",
        topic="Access & scope",
        keywords=("revoke", "disconnect", "remove access", "unlink", "take away access",
                  "cut off", "stop an app"),
        question="How do I revoke access or disconnect an app?",
        answer=(
            "Disconnect the app in Projects and its token is dropped from the CredentialBroker; "
            "you can also revoke the grant at the provider (e.g. your Google account's app "
            "permissions). Because a Mission only ever holds a reference — not a copy of the "
            "token — revoking it cuts off access immediately, with nothing stranded elsewhere."),
        source="CredentialBroker (drop on disconnect) · provider-side revocation"),
    KnowledgeEntry(
        id="production-secrets",
        topic="Credentials",
        keywords=("production secret", "vault", "openbao", "secret manager", "secrets kept",
                  "at rest", "encrypt", "encrypted", "where are secrets"),
        question="Where are secrets kept in production?",
        answer=(
            "In a real deployment the CredentialBroker is backed by Vault or OpenBao (not a "
            "file), deny-by-default, and a capability that needs a production-grade broker fails "
            "closed if only a development one is wired. Secrets are substituted at egress and "
            "never sit in a plan, a context, or the model."),
        source="credential broker backends: local / vault / openbao (assurance fail-closed)"),
    KnowledgeEntry(
        id="self-host",
        topic="Data handling",
        keywords=("self-host", "self host", "what leaves", "air-gap", "air gap", "on-prem",
                  "on prem", "my server", "offline", "runs where"),
        question="If I self-host, what actually leaves my machine?",
        answer=(
            "For local-strategy operations, nothing — files stay on your machine. For cloud "
            "suites, only the specific API calls you authorized reach that provider. Model calls "
            "follow your policy (local by default; cloud only when allowed). The Projects UI and "
            "its API run on one origin that you host."),
        source="self-host: agentic-os-projects · PhysicalStrategy locality · model policy"),
    KnowledgeEntry(
        id="licensing",
        topic="Cost & licensing",
        keywords=("how much", "cost", "pricing", "price", "free", "open source", "open-source",
                  "license", "licence", "licensing", "agpl", "pay", "subscription", "trial"),
        question="What does it cost — is it open source?",
        answer=(
            "The core stack is open source (AGPL-3.0) and free to self-host — you run it on your "
            "own infrastructure with no licence fee. What you pay for is your own compute and any "
            "cloud model or cloud-suite usage you choose to enable. Hosted/managed options and "
            "commercial support are a separate conversation — reach the team at #agentic-apps on "
            "Slack or info@redevops.io. (No pricing tiers are quoted here so this answer can't go "
            "stale — ask the team for current commercial terms.)"),
        source="AGPL-3.0 flagship repos · self-host is free · commercial terms via contact"),
    KnowledgeEntry(
        id="compliance",
        topic="Governance",
        keywords=("compliance", "compliant", "soc2", "soc 2", "gdpr", "hipaa", "iso 27001",
                  "certified", "certification", "audit trail", "regulator", "data residency"),
        question="Is it compliant (SOC 2 / GDPR / HIPAA)? Can I meet my obligations?",
        answer=(
            "The architecture is built to support your compliance posture rather than to make a "
            "certification claim on your behalf: you can self-host so data stays in your own "
            "residency, credentials are least-privilege and never touch the model, where the AI "
            "runs is a governed choice, and every side-effecting action is risk-tiered, "
            "human-gated and re-observed — an auditable record of what ran and why. Whether that "
            "satisfies a specific framework depends on your deployment and controls; for a formal "
            "attestation or a DPA, contact the team (#agentic-apps on Slack / info@redevops.io)."),
        source="self-host residency · least-privilege creds · GovernedEnvelope tiers+approval+re-observe"),
    KnowledgeEntry(
        id="retention",
        topic="Data handling",
        keywords=("retain", "retention", "how long", "keep my data", "kept for", "delete my data",
                  "data deleted", "hold my data", "keep my documents", "purge", "erase my"),
        question="What do you retain, and for how long?",
        answer=(
            "As little as possible, and you hold it. Your account token lives in the "
            "CredentialBroker only while the app is connected — disconnecting drops it. Your "
            "documents aren't copied into the stack: cloud-suite calls act in place on the "
            "provider, and local operations act on files on your own machine. Anything indexed for "
            "retrieval lives in your own Context Runtime (your infrastructure), so retention and "
            "deletion are under your control. Execution records are the governed history of what "
            "ran — kept for audit, on your deployment."),
        source="CredentialBroker token lifecycle (dropped on disconnect) · content stays at source / on your Context Runtime"),
    KnowledgeEntry(
        id="offline-local-apps",
        topic="Access & scope",
        keywords=("work offline", "offline", "without internet", "no internet", "air-gapped app",
                  "which apps are local", "local editing", "edit locally", "works locally",
                  "libreoffice", "on my desktop", "desktop app"),
        question="Which apps work locally / offline versus needing the cloud?",
        answer=(
            "It splits by app. LibreOffice, desktop Office, Apple iWork and raw file-format edits "
            "(CSV, Markdown, XLSX, DOCX) run entirely on your machine through a local connector — "
            "no internet needed and nothing leaves the box. Google Workspace and Microsoft 365 are "
            "cloud suites: those actions go to the provider's API and need connectivity. Each app "
            "in Projects shows which surface it uses, so you can choose local-only where you want "
            "to stay offline."),
        source="productivity.PhysicalStrategy (FILE_NATIVE/LOCAL_* vs CLOUD_API) · PRODUCTIVITY_CATALOG"),
    KnowledgeEntry(
        id="add-connector",
        topic="Access & scope",
        keywords=("add a connector", "not listed", "not in the list", "custom connector",
                  "build a connector", "new integration", "integrate my own", "app isn't here",
                  "app not here", "connect something else", "isn't supported", "not supported"),
        question="How do I add an app or connector that isn't listed?",
        answer=(
            "Connectors are pluggable. Supported apps come from the connector catalog "
            "(redevops-connectors), and you describe a new integration in plain language through "
            "the Connect wizard, which compiles it into a governed connector — it interprets, you "
            "confirm, the runtime wires it. A connector implements the same AdapterPort surface "
            "(capabilities/connect/execute/observe/health) as the built-ins, so a new app is "
            "governed exactly like the rest. For one that needs building, ask at #agentic-apps."),
        source="redevops-connectors catalog · Connect Compiler wizard · AdapterPort"),
    KnowledgeEntry(
        id="byo-oauth",
        topic="Credentials",
        keywords=("my own oauth", "own oauth app", "bring your own", "byo", "own client id",
                  "own app credentials", "register my own", "use my own keys", "my own app secret",
                  "own google app", "own microsoft app"),
        question="Can I use my own OAuth app / bring my own keys?",
        answer=(
            "Yes. A deployment can register its own OAuth apps — you supply the client id/secret "
            "for Google, Microsoft, Slack, etc. via the connect layer's environment, and Connect "
            "uses them for the consent flow. Those app secrets stay in the connect layer only; "
            "they never enter a Mission, the model, or telemetry. If no app is registered for a "
            "provider, connecting falls back to a simulated connect for the demo."),
        source="integrations/hosted_oauth.py KNOWN_OAUTH · ProviderOAuthApp.from_env (BYO client id/secret)"),
    KnowledgeEntry(
        id="pick-apps",
        topic="Getting started",
        keywords=("pick which apps", "pick apps", "choose apps", "choose which apps",
                  "only install", "install everything", "install all", "have to install",
                  "install the whole", "whole suite", "don't want the whole",
                  "do not want the whole", "subset of apps", "select apps", "which apps do i need",
                  "just the apps i want", "only the apps"),
        question="Do I have to install everything, or can I pick just the apps I want?",
        answer=(
            "You pick. A deployment offers only the apps you enable — Projects shows just those, "
            "and the rest aren't installed or surfaced. So you can run a Google-only setup, or "
            "Slack+Stripe, without pulling in the whole suite. You can widen the selection later; "
            "nothing about the boundary changes — each app is still connected and governed the "
            "same way."),
        source="projects_api.enabled_apps / enabled_selection ($PROJECTS_APPS)"),
    KnowledgeEntry(
        id="mission-vs-workflow",
        topic="Getting started",
        keywords=("what is a mission", "what's a mission", "what is a workflow", "what's a workflow",
                  "mission vs workflow", "difference between a mission", "what does it do",
                  "how does it work", "what can sidekick do", "what can you do"),
        question="What's the difference between a Mission and a Workflow?",
        answer=(
            "A Mission is a single governed run — you (or Sidekick) describe an outcome across "
            "your connected apps, it's compiled into steps, high-consequence steps park on your "
            "approval, and it executes once with a recorded, verifiable history. A Workflow is a "
            "Mission pattern that recurs — the same governed steps on a schedule or a trigger "
            "(e.g. daily support triage). Sidekick proposes both from plain language; you confirm "
            "before anything runs."),
        source="Mission Runtime (single governed run) vs Workflow (scheduled/triggered pattern)"),
    KnowledgeEntry(
        id="third-party-sharing",
        topic="Data handling",
        keywords=("share my data", "sell my data", "third party", "third-party", "train on my",
                  "used for training", "send to openai", "send to anthropic", "goes to openai",
                  "share with", "who gets my data", "shared with"),
        question="Do you share or sell my data, or train models on it?",
        answer=(
            "No. The stack doesn't sell your data or send it anywhere you didn't authorize. The "
            "only external calls are the ones you explicitly connected — a cloud suite you linked, "
            "or a cloud model you allowed by policy — and each receives only the specific request "
            "for that step, not your corpus. If you run inference locally, nothing goes to any "
            "model provider at all. Whether a cloud model provider trains on prompts is that "
            "provider's policy; keep sensitive work on a local model to avoid the question "
            "entirely."),
        source="only authorized egress (connected suites / policy-allowed model) · local inference keeps data in"),
    KnowledgeEntry(
        id="support-contact",
        topic="Getting started",
        keywords=("contact", "support", "get help", "reach you", "reach the team", "talk to",
                  "stuck", "help me", "who do i ask", "customer support", "slack channel",
                  "join slack", "ask a human"),
        question="How do I get help or contact a human?",
        answer=(
            "Ask in #agentic-apps on the ReDevOps Slack, or email info@redevops.io — those are on "
            "the contact section and at the end of every deployment guide on redevops.io. For "
            "how-it-works questions (credentials, data locality, governance) you can also just ask "
            "me here; for account, commercial, or build-me-a-connector requests, the team is the "
            "right place."),
        source="#agentic-apps on Slack · info@redevops.io (redevops.io contact + deploy guides)"),
    # ── How the core components work (grounded in redevops.io/in-plain-english[-people-can-understand]
    #    and /infrastructure). answer = plain-English overview; detail = the technical tier. ──────────
    KnowledgeEntry(
        id="arch-overview",
        topic="How it works",
        keywords=("how does the stack work", "how the stack works", "whole stack", "stack work",
                  "how does it all work", "how does redevops work", "ai runtime stack",
                  "operating system", "the layers", "architecture", "how does the platform work",
                  "high level"),
        question="How does the whole stack work?",
        answer=(
            "Think of it as an operating system for getting real work done with AI: it decides "
            "what's worth doing, plans it, executes the plan safely under human approval, and gives "
            "each step only the intelligence it needs — every decision explainable, every outcome "
            "fed back so it improves. The flow: Discovery notices work worth doing → Mission runs "
            "it safely and provably → Context decides what each step needs → ReDevOps RAG fetches "
            "the actual sources → the agent-harness keeps each step safe → the Governance Plane "
            "watches the whole team → and Projects/Sidekick are where a person drives it. It runs "
            "around your existing agent framework (LangGraph, CrewAI, NeMo…), not instead of it."),
        detail=(
            "The layers: Discovery Runtime (finds the work), Execution Planner (designs it as a "
            "six-axis plan), Mission Runtime (governs and executes — the kernel), Context Runtime "
            "(supplies the intelligence, by reference), Capabilities (do the work — models, "
            "retrievers like ReDevOps RAG, SQL, property graphs, tools, humans, each behind one "
            "contract), intrinsic security (least authority + just-in-time keys + tamper-evident "
            "audit), and the Governance Plane (cross-agent trajectory). The through-line: most "
            "systems assume; this one decides, measures and routes — and can act safely."),
        source="redevops.io/in-plain-english (the AI Runtime Stack) · /infrastructure"),
    KnowledgeEntry(
        id="context-runtime",
        topic="How it works",
        keywords=("context runtime", "how does context", "prep chef", "supplies the intelligence",
                  "what to retrieve", "context engine", "context plane"),
        question="How does the Context Runtime work?",
        answer=(
            "The Context Runtime decides what intelligence each step gets. Given a question and "
            "limits (time, cost, quality) it decides what to retrieve, which representation, how "
            "hard to reason, and which model — then verifies and learns. It works by reference: the "
            "AI is handed pointers to governed data and materialises only what a decision needs "
            "instead of copying the whole world into its context — cheaper, private data stays "
            "governed, every step replayable. It optimises context inside a step; it doesn't run "
            "the workflow (that's the Mission Runtime)."),
        detail=(
            "It writes the choice down as a six-axis plan — what to represent, what to retrieve, "
            "how to reason, which model, what topology, how to verify — so any axis can be "
            "explained or changed alone. Tricks: routing (send each question to the method best at "
            "that kind), knapsack context-budgeting (fit the highest-value evidence in the token "
            "budget), recompile-on-change-only (same inputs → same plan; idempotent), and an "
            "optional contextual bandit that learns the best choice for a single leaf decision "
            "(never to reshuffle a whole plan). Retrieval is by reference so a pointer can't "
            "silently go stale."),
        source="redevops.io/in-plain-english (Context Runtime) · /in-plain-english-people-can-understand"),
    KnowledgeEntry(
        id="redevops-rag",
        topic="How it works",
        keywords=("redevops rag", "redevops-rag", "the librarian", "how does retrieval",
                  "how does search", "rag work", "how does the rag", "retrieval engine"),
        question="What is ReDevOps RAG and how does retrieval work?",
        answer=(
            "ReDevOps RAG is the retriever — the librarian. It finds the passages that actually "
            "answer a question and hands back pointers to the sources (with where each came from "
            "and why it ranked) instead of pasting in a blob of text — so a pointer to one source "
            "of truth can't quietly go stale the way a pasted copy does. It's one capability behind "
            "a contract, alongside SQL, property graphs, tools, APIs and humans; the runtime picks "
            "whichever fits the question."),
        detail=(
            "It isn't one search but several, chosen per question: keyword relevance (BM25 — reward "
            "rare matching words, don't over-reward repetition), connect-the-dots multi-hop search "
            "(Personalized PageRank / HippoRAG — walk the web of links out from facts already known "
            "relevant, e.g. 'my manager's manager'), and time-aware search (temporal — prefer the "
            "version of a fact true at the relevant moment). Measured: routing per-question beats "
            "any single fixed method (~0.883 on a mixed suite). It returns references, not copies."),
        source="redevops.io/in-plain-english-people-can-understand (ReDevOps RAG — the librarian) · /benchmarks"),
    KnowledgeEntry(
        id="mission-runtime",
        topic="How it works",
        keywords=("mission runtime", "how does the mission runtime", "execution kernel",
                  "project manager", "how do missions run", "runs the work", "how missions execute"),
        question="How does the Mission Runtime work?",
        answer=(
            "The Mission Runtime is the execution kernel — the project manager with a safety net. "
            "It turns a goal into governed steps, runs them, pauses for human sign-off where "
            "needed, records everything, and can replay the whole run and prove the record wasn't "
            "altered. If a step fails partway it rolls back the ones already taken, so a "
            "half-finished job never leaves a mess. It owns the workflow, side effects, state, "
            "order, approvals and undo."),
        detail=(
            "It borrows the safety tricks databases already figured out: a write-ahead log + replay "
            "(write down what you'll do before doing it; replay reaches the same ending, pinned to "
            "what was known at the time), saga/compensation (each step carries its own undo; fires "
            "only if a step actually ran and then failed), a two-phase approval gate (prepare, but "
            "don't commit until a human says go), leases/locking (time-limited exclusive claim so "
            "two workers don't collide), safe concurrency (parallelise independent steps, serialise "
            "ones touching the same resource, and log why), a hash-chained ledger (each record "
            "fingerprints the previous, so history can't be edited secretly), and independent "
            "verification. Same behaviour across the Python, Go and Kotlin runtimes."),
        source="redevops.io/in-plain-english (Mission Runtime) · /in-plain-english-people-can-understand"),
    KnowledgeEntry(
        id="discovery-runtime",
        topic="How it works",
        keywords=("discovery runtime", "the scout", "finds the work", "notices work",
                  "proposes missions", "how does discovery", "finds work worth"),
        question="How does the Discovery Runtime work?",
        answer=(
            "The Discovery Runtime is the scout. It notices work worth doing — 'this investigation "
            "or fix should probably happen' — and proposes it, but never runs it itself. A "
            "machine-proposed job goes through the exact same Mission pipeline as a human-requested "
            "one: validated, simulated, run, replayed and verified identically. There's no "
            "privileged back door."),
        detail=(
            "It keeps two things separate: what triggered the idea (origin) and how sure it is "
            "about each input (stated / inferred / unconfirmed), so a guess never quietly becomes a "
            "'fact.' Example: it watches support volume, deploy history and error rates, sees a "
            "spike line up with a release, and proposes a mission with the evidence attached — even "
            "though nobody filed a ticket."),
        source="redevops.io/in-plain-english (Discovery Runtime) · /in-plain-english-people-can-understand"),
    KnowledgeEntry(
        id="execution-planner",
        topic="How it works",
        keywords=("execution planner", "designs the work", "six-axis", "six axis", "plan the work",
                  "how is the plan made", "how does planning work"),
        question="How does the Execution Planner work?",
        answer=(
            "The Execution Planner designs the work. It turns a proposal into one explainable plan "
            "across six independent choices: which representations, which retrieval, how hard to "
            "reason, which model, what shape the work takes (topology), and how it's verified. "
            "Writing the plan as six separate axes means any one can be explained or changed on its "
            "own."),
        detail=(
            "The six-axis plan is like a recipe card that lists ingredients, method and oven "
            "temperature separately — you can change the temperature without rewriting the recipe. "
            "This is where measured routing lives: pick the representation/retrieval/model proven "
            "best for that question class rather than one fixed pipeline, and fall back to a safe "
            "default when confidence is low."),
        source="redevops.io/in-plain-english (Execution Planner — designs the work)"),
    KnowledgeEntry(
        id="agent-harness",
        topic="How it works",
        keywords=("agent-harness", "agent harness", "safety cage", "single step safe", "sandbox",
                  "how is each step", "smallest safe unit"),
        question="What is the agent-harness?",
        answer=(
            "The agent-harness is the safety cage for a single step. It governs how one model or "
            "tool call runs — approvals, sandboxing, guardrails, evaluation. It's the smallest safe "
            "unit; the runtimes compose many of them, so no single call can go rogue."),
        detail=(
            "Beneath the cage, intrinsic security enforces who may do what: a step can only "
            "exercise the authority it was specifically granted (least authority), secrets are lent "
            "just-in-time and taken back (like a hotel keycard that opens only your room for only "
            "your stay), and the runtime — not the agent — writes down what each step did as it "
            "happens, as tamper-evident fingerprints rather than the sensitive contents (written at "
            "the door, not by the worker)."),
        source="redevops.io/in-plain-english-people-can-understand (agent-harness) · /in-plain-english (Intrinsic security)"),
    KnowledgeEntry(
        id="governance-plane",
        topic="How it works",
        keywords=("governance plane", "the referee", "across agents", "whole team's", "trajectory",
                  "watches the whole", "cross-agent", "many agents", "team of agents"),
        question="How does the Governance Plane work?",
        answer=(
            "The Governance Plane is the referee watching the whole game. The safety cage guards "
            "one step; the Governance Plane watches the whole team — the sequence of moves across "
            "agents and missions. A fleet where every agent follows the rules can still cause a "
            "disaster: one undoes another's fix, several pile onto the same resource, or a "
            "sensitive record is reassembled from reads that each looked harmless. It evaluates the "
            "trajectory, catches the escalation no single-step check can see, and is honest about "
            "what it can actually block versus only flag and escalate."),
        detail=(
            "A new rule can run in shadow first — recording what it would have done without acting "
            "— until it's proven and promoted to actually block; it can also flag when a risky "
            "sequence follows a change in the underlying evidence. Containment: if a run of "
            "individually-fine moves adds up to a dangerous shape (read one private record, then "
            "another, then email them out), it boxes the whole thing, and a no-override stop can't "
            "be lifted by the very thing that triggered it."),
        source="redevops.io/in-plain-english (Governance Plane) · /in-plain-english-people-can-understand"),
    KnowledgeEntry(
        id="projects-sidekick-arch",
        topic="How it works",
        keywords=("control plane", "mission supervisor", "how does sidekick fit",
                  "how does projects fit", "how do projects and sidekick", "where do humans"),
        question="How do Projects and Sidekick fit into the architecture?",
        answer=(
            "Projects and Sidekick are where a person operates the stack. Projects is the durable "
            "control plane — the manifest, desired-vs-observed state, history, approvals and audit; "
            "the place you launch, watch and approve missions, and where workflows, discoveries and "
            "anything needing a person surface together. Sidekick is the conversational Mission "
            "Supervisor over all of it. Either way, every change still goes through the Mission "
            "Runtime — nothing bypasses governance."),
        detail=(
            "In Sidekick several agents coordinate to help you get something done, and the "
            "coordination is emergent (the agents work out the teamwork) rather than a rigid "
            "pre-drawn org chart — which is exactly why the Governance Plane watches the whole "
            "team's trajectory, not just single calls."),
        source="redevops.io/in-plain-english (Projects + Sidekick) · /in-plain-english-people-can-understand"),
)


def help_questions() -> Tuple[dict, ...]:
    """The canonical questions, for an in-UI 'what can I ask Sidekick' menu."""
    return tuple({"id": e.id, "topic": e.topic, "question": e.question} for e in STACK_KNOWLEDGE)


def answer_stack_question(text: str) -> Optional[KnowledgeEntry]:
    """Best-matching authoritative entry for a stack/security/privacy question, or ``None``.

    Deterministic keyword scoring: an entry scores by how many of its trigger terms appear as
    substrings of the lowercased question; the highest score wins (ties → first in declaration
    order). Terms are specific (often multi-word) so ordinary Mission requests don't match."""
    t = (text or "").lower()
    best: Optional[KnowledgeEntry] = None
    best_score = 0
    for entry in STACK_KNOWLEDGE:
        # weight by keyword length: a specific multi-word phrase ("does the ai", "see my token")
        # outweighs a generic single term ("token") that several topics legitimately share.
        score = sum(len(kw) for kw in entry.keywords if kw in t)
        if score > best_score:
            best, best_score = entry, score
    return best if best_score > 0 else None
