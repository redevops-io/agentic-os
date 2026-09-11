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
        keywords=("contact support", "contact a human", "contact the team", "contact you",
                  "get help", "reach you", "reach the team", "reach support", "reach a human",
                  "talk to a human", "i'm stuck", "im stuck", "who do i ask", "customer support",
                  "slack channel", "join slack", "ask a human"),
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
    # ── Proactive intelligence (the "what should happen next" direction). STATUS-HONEST: most of
    #    this is the roadmap, NOT shipped — every answer says so plainly. One kernel ships today
    #    (Growth trend scoring, synthetic-validated) and the Support primitives ship today; those
    #    say so specifically. Grounds the LLM fallback too, so it inherits the same honest framing. ─
    KnowledgeEntry(
        id="proactive-overview",
        topic="Proactive intelligence (roadmap)",
        keywords=("what needs me", "needs my attention", "what should happen next",
                  "what should i do next", "proactive", "proactively", "surface what matters",
                  "tell me what to work on", "3 things need you", "three things need you",
                  "attention layer"),
        question="Can Sidekick tell me what needs my attention — what should I work on next?",
        answer=(
            "Yes — the surface itself now ships: ask me 'what needs me?' and I return a live, "
            "prioritised 'what needs you today' list built by the Priority Engine. What's still "
            "growing is the set of detectors that feed it: Growth trend scoring has a "
            "synthetic-validated kernel and the Support follow-up/lead signals ship today, while the "
            "other apps' detectors (CRM, Projects, Research, …) are on the roadmap. The pattern: each "
            "app observes its own state, detects an opportunity/risk/anomaly/gap, proposes candidate "
            "actions, estimates each one's value/confidence/urgency/cost/risk, and then acts, asks you "
            "to approve, defers, or stays silent — and the engine gathers the ones worth your time "
            "into one list and handles or defers the rest. The point isn't to run more agents; it's to "
            "complete more useful work per unit of your attention, under your policies."),
        detail=(
            "The loop is: observe → detect opportunity/risk/anomaly → generate candidate "
            "interventions → estimate expected value, confidence, urgency, execution cost, human-"
            "attention cost and operational risk → a Context-Runtime optimiser chooses act / request-"
            "approval / defer / abstain → an OutcomeEvent feeds learning. Human attention is treated "
            "as a scarce resource alongside compute and tool cost. Shipping order on the roadmap: "
            "Growth Opportunity Radar first (clear external data, low side-effect risk, strong "
            "calibration), then CRM Next-Best-Action, then Projects Risk Radar, Research, LearnerBot, "
            "and finally the cross-app Sidekick attention layer."),
        source="Shipped: agentic_os/priority_engine.py (the decision spine + 'what needs me?' surface) · "
               "Growth kernel: trend_intelligence.py · Support: support_autonomy.py · Roadmap: more detectors"),
    KnowledgeEntry(
        id="priority-engine",
        topic="Proactive intelligence (roadmap)",
        keywords=("priority engine", "attention runtime", "opportunity planner",
                  "intervention planner", "how does it prioritize", "how does it prioritise",
                  "how are things ranked", "intervention candidate", "priority score",
                  "how does it decide what's important"),
        question="What is the Priority Engine and how would it rank what matters?",
        answer=(
            "The Priority Engine is the shared primitive that ranks what matters, and it now ships "
            "(the decision spine — the detectors that feed it are still growing). Every app uses it "
            "instead of re-inventing its own alerting: rather than emit a raw alert, a detector "
            "produces an *intervention candidate* — a possible action with its subject, the evidence "
            "behind it, an expected value, a confidence, an urgency, a cost, a risk, and the "
            "capabilities/approval-tier it needs. Crucially it does NOT rank by confidence alone: a "
            "very certain but low-value item rightly loses to a less-certain high-impact one. It runs "
            "inside the existing orchestration, not as a new microservice."),
        detail=(
            "Priority is a transparent function of probability, expected upside/downside, urgency, "
            "reversibility, execution cost, human-attention cost, operational risk and information "
            "value — every term is explainable, and the objective stays app-specific behind a shared "
            "contract. What ships: Opportunity, InterventionCandidate, InterventionDecision, "
            "PriorityScore/PriorityPolicy and OutcomeEvent, plus the decision logic (do-nothing is a "
            "first-class candidate, so it can't over-act) and the cross-app 'what needs me?' surface. "
            "It's deterministic and tested; it makes no outcome-learning claim yet — OutcomeEvent is "
            "the telemetry contract a future learning loop will consume."),
        source="Shipped: agentic_os/priority_engine.py (contracts + optimiser + what_needs_me) · "
               "reuses agent_gateway RiskTier/ApprovalPolicy for governance"),
    KnowledgeEntry(
        id="opportunity-vs-intervention",
        topic="Proactive intelligence (roadmap)",
        keywords=("opportunity vs intervention", "opportunity versus intervention",
                  "difference between an opportunity", "what is an intervention",
                  "do nothing", "do-nothing", "counterfactual", "over-act", "overact",
                  "confidence is not", "confidence vs value", "rank by confidence"),
        question="What's the difference between an opportunity and an intervention — and won't a proactive agent over-act?",
        answer=(
            "They're kept deliberately separate. An *opportunity* is something discovered ('Acme "
            "looks ready for a technical follow-up'); an *intervention* is something the system could "
            "do about it ('send Acme the deployment proposal'), and one opportunity can spawn several "
            "candidate interventions that an optimiser chooses between. To stop over-acting, **doing "
            "nothing is a first-class candidate** evaluated like any other — as is 'gather more "
            "evidence first' and 'ask a human'. So the system has to justify acting against the "
            "option of staying quiet, rather than acting by default. (This is the designed behaviour; "
            "the machinery is on the roadmap.)"),
        detail=(
            "Where practical the planner compares do-nothing vs action A vs action B vs ask-human vs "
            "acquire-evidence-E, so evidence acquisition can itself be the chosen move. The system "
            "learns two separate things: whether its reading of the world was correct, and whether "
            "the response it picked was effective — because a right diagnosis with the wrong action "
            "is still a failure."),
        source="Roadmap — opportunity/intervention split + counterfactual (do-nothing first-class) planning"),
    KnowledgeEntry(
        id="crm-next-best-action",
        topic="Proactive intelligence (roadmap)",
        keywords=("next best action", "next-best-action", "deal radar", "which deals need",
                  "stalled deal", "neglected lead", "renewal risk", "expansion opportunity",
                  "buying signal", "deal that needs attention", "crm proactive"),
        question="Will the CRM tell me the next best action / which deals need attention (Deal Radar)?",
        answer=(
            "The mechanism ships: CRM is a producer for the shared decision/outcome loop. From a deal's "
            "state it proposes several candidate actions (send proposal, schedule a call, nurture, "
            "renewal outreach, or just monitor) with a predicted value and — because they're outbound "
            "— an approval gate; the shared runtime selects the highest-utility one and LEARNS from "
            "observed outcomes (reply → meeting → conversion → loss) which action actually works, "
            "correcting its priors over time. Two honesty caveats: this is validated IN SIMULATION "
            "(observing outcomes measurably improves selection on a controlled environment) — it is "
            "NOT a real-world commercial-lift claim, which needs live deal outcomes; and the loop keeps "
            "governance intact (a consequential send still needs approval) no matter what it learns."),
        source="Shipped producer: agentic_os/crm_nba.py → shared loop (priority_engine select_action + "
               "outcome_learning UtilityModel), simulation-validated · Roadmap: live commercial outcomes"),
    KnowledgeEntry(
        id="support-resolution-intelligence",
        topic="Proactive intelligence (roadmap)",
        keywords=("resolution intelligence", "resolution path", "likely resolution",
                  "predict the resolution", "escalation prediction", "escalation probability",
                  "duplicate incident", "macro recommendation", "resolution planner",
                  "support proactive"),
        question="Can Support predict the likely resolution before an agent works it out (Resolution Intelligence)?",
        answer=(
            "Partly today, more on the roadmap. What ships now are the Support building blocks: "
            "opt-out detection, a follow-up policy that respects thread risk, lead qualification, a "
            "self-improving knowledge base that learns from resolutions, and sentiment-based handoff "
            "to a human. The planned extension is a resolution planner that predicts the likely issue "
            "and resolution path up front — with confidence, the evidence (error signature, account "
            "event, KB article, similar resolved cases) and an escalation probability — plus "
            "similar-case retrieval, duplicate-incident detection, macro recommendation, KB-gap "
            "detection and safe auto-resolution only where policy permits. It would learn from "
            "resolution success, edits, draft-kept rate, FRT/TTR, reopen rate and CSAT."),
        source="Shipped: agentic_os/support_autonomy.py (opt-out/follow-up/lead-scoring/self-improving "
               "KB/handoff) · Roadmap: resolution-path predictor + escalation prediction"),
    KnowledgeEntry(
        id="projects-execution-risk-radar",
        topic="Proactive intelligence (roadmap)",
        keywords=("execution risk", "risk radar", "delivery risk", "milestone slip",
                  "milestone slippage", "blocked dependency", "will the project slip",
                  "project risk", "slip window", "execution intelligence", "owner overload"),
        question="Can Projects warn me about execution risk before a milestone slips (Risk Radar)?",
        answer=(
            "The scoring kernel for this ships today (validated on a controlled synthetic replay, not "
            "yet on live project data) and already feeds the 'what needs me?' surface — it's the "
            "project-management equivalent of the trend kernel: spot emerging execution failures "
            "before they become visible failures. From signals like schedule pace, dependency lag, "
            "blocked dependencies, decision debt, requirement ambiguity, owner overload, stale "
            "approvals and under-testing it flags a risk with an estimated slip window, a calibrated "
            "confidence, the primary cause and a recommended mitigation (which parks on your "
            "approval). On the synthetic benchmark it beats a naive burndown baseline — which MISSES "
            "dependency-driven slips that look on-pace — with useful lead time. That proves the "
            "scoring LOGIC, NOT real-world accuracy; the live-data version is the next step. It's "
            "intelligence layered above your PM tool, not another task manager."),
        source="Shipped kernel: agentic_os/execution_risk.py + execution_risk_backtest.py "
               "(synthetic-validated) · Roadmap: live project-data validation"),
    KnowledgeEntry(
        id="growth-trend-intelligence",
        topic="Proactive intelligence (roadmap)",
        keywords=("trend intelligence", "future trend", "emerging trend", "opportunity radar",
                  "growth radar", "emerging topic", "before it's saturated", "before it is saturated",
                  "trend forecast", "spot trends", "emerging-content", "will this trend"),
        question="Can it find emerging trends before they're saturated (Growth Opportunity Radar)?",
        answer=(
            "This is the furthest-along piece — but be clear on what exists. The scoring KERNEL ships "
            "today: it measures early emergence across independent sources, calibrates a confidence "
            "against outcomes, and abstains when evidence is weak, rather than asking a model to "
            "guess. It's been validated on a controlled synthetic replay (its logic beats simple "
            "momentum / search-only baselines with useful lead time) — which proves the LOGIC, NOT "
            "real-world accuracy. The live Growth radar over real Reddit/YouTube/search data, with "
            "stored forecasts scored against what actually happened, is the next step on the roadmap. "
            "By design it avoids 'nearly 100% accuracy' claims and reports calibration and precision-"
            "at-threshold instead."),
        detail=(
            "The kernel scores velocity, acceleration, cross-source confirmation, source "
            "independence, small-creator outliers, audience-question growth and geographic spread, "
            "minus competition/saturation/manipulation penalties; an isotonic calibrator turns the "
            "raw score into a probability; STRICT/BALANCED/EXPLORATORY modes abstain below threshold. "
            "A candidate is meant to carry title, description, confidence, forecast horizon, "
            "lifecycle state (weak signal → emerging → accelerating → mainstream → peaking → "
            "saturated → declining), proof of early emergence/acceleration/low-competition, "
            "counter-evidence and a recommended opportunity."),
        source="Shipped kernel: agentic_os/trend_intelligence.py + trend_backtest.py "
               "(synthetic-validated logic) · Roadmap: live-data Growth radar + stored-forecast calibration"),
    KnowledgeEntry(
        id="creator-intelligence",
        topic="Proactive intelligence (roadmap)",
        keywords=("creator intelligence", "semrush for", "for content creators", "rising topics",
                  "small-creator outlier", "small creator outlier", "content portfolio",
                  "youtuber", "underserved question", "pre-post analysis", "content ideas"),
        question="Is there a 'Semrush for content creators' — rising topics, outliers, content ideas?",
        answer=(
            "It's the planned creator-facing specialisation of the Growth intelligence (not shipped "
            "as a product yet; it shares the trend-scoring kernel that does exist). For YouTubers, "
            "short-form creators, podcasters and newsletter publishers it would surface rising "
            "topics/sounds/formats, underserved audience questions, low-competition/high-interest "
            "concepts, and small-creator outliers — where an outlier is judged against that creator's "
            "OWN expected performance, not absolute views. It would also do pre-post analysis (hook "
            "strength, retention weak spots, topic demand, saturation, title/thumbnail fit) and a "
            "portfolio optimiser over topic/format/platform/timing — always without presenting a "
            "forecast as guaranteed performance."),
        source="Roadmap — Creator Intelligence (a Growth/Social package over the trend kernel; not yet shipped)"),
    KnowledgeEntry(
        id="outreach-intent-radar",
        topic="Proactive intelligence (roadmap)",
        keywords=("intent radar", "timing radar", "who is worth contacting", "who to contact now",
                  "outreach radar", "outreach intelligence", "who should i reach out to",
                  "best time to contact", "do not contact before", "when to reach out"),
        question="Can outreach tell me who's worth contacting now and when (Intent & Timing Radar)?",
        answer=(
            "The mechanism ships: like CRM, Outreach is a producer for the shared decision/outcome "
            "loop. It answers 'who is worth contacting now, and why now' — proposing contact now, a "
            "different channel, waiting for a stronger trigger, or just monitoring (an opted-out "
            "prospect gets no outbound option at all), always with an approval gate on a send. Its "
            "reward COUNTS the downside — unsubscribes and negative replies push utility down — so the "
            "loop learns to stop contacting when outreach backfires rather than maximise send volume. "
            "Honesty caveats: validated IN SIMULATION (the loop learns to prefer the actions that pay "
            "off and to back off when they don't), NOT a real-world reply-rate claim (that needs live "
            "outcomes); governance is preserved regardless of what it learns."),
        source="Shipped producer: agentic_os/outreach_nba.py → shared loop (select_action + UtilityModel), "
               "simulation-validated · Roadmap: live outreach outcomes"),
    KnowledgeEntry(
        id="recruiting-fit-engine",
        topic="Proactive intelligence (roadmap)",
        keywords=("opportunity fit", "fit engine", "job fit", "job-seeker", "job seeker mode",
                  "capability graph", "tailor my resume", "job search workflow", "candidate fit",
                  "recruiter mode", "apply to jobs"),
        question="Can it match me to jobs and run the job-search workflow (Opportunity Fit Engine)?",
        answer=(
            "That's the planned recruiting capability (not shipped yet), mapping onto the intended "
            "Sidekick-driven job search. In job-seeker mode it would compare a listing against your "
            "resume, projects, portfolio and repos using a capability graph rather than keyword "
            "overlap — giving a fit score, strong evidence, weak/missing areas and a differentiating "
            "pitch. Sidekick would coordinate the chain: find listing → evaluate fit → research "
            "employer → tailor resume → draft cover letter → request approval where required → submit "
            "→ update the tracker → watch for a meaningful reply. Employer mode would rank "
            "demonstrated capability the same way. It would learn from responses, screens, "
            "interviews, progression, rejections and offers."),
        source="Roadmap — Jobs/Recruiting Opportunity Fit Engine (not yet shipped)"),
    KnowledgeEntry(
        id="research-info-gain-planner",
        topic="Proactive intelligence (roadmap)",
        keywords=("information gain", "information-gain", "info-gain", "reduce uncertainty",
                  "what should i investigate", "research planner", "evidence acquisition",
                  "which investigation", "stopping rule", "belief state", "when to stop researching"),
        question="Does research plan what to investigate next to reduce uncertainty (Information-Gain Planner)?",
        answer=(
            "The planner kernel ships today (validated on controlled synthetic tasks, not yet on real "
            "research). Most research agents optimise 'what answers the question'; this one also asks "
            "'what investigation reduces uncertainty the most per unit cost'. From a belief over "
            "hypotheses it scores each candidate investigation's expected information gain (the "
            "expected reduction in entropy), picks the best per cost, updates the belief with Bayes' "
            "rule, and repeats — stopping when a hypothesis passes the decision threshold, remaining "
            "gain is low, the budget is spent, or the evidence is irreducibly ambiguous. On the "
            "synthetic benchmark, information-gain planning reaches confident, correct conclusions far "
            "more often and more cheaply than naive (random / round-robin) investigation. That proves "
            "the planning LOGIC, NOT real-world research skill; the live version feeds the Context "
            "Runtime."),
        source="Shipped kernel: agentic_os/research_planner.py + research_planner_backtest.py "
               "(synthetic-validated) · Roadmap: real research questions/evidence/outcomes"),
    KnowledgeEntry(
        id="knowledge-debt-radar",
        topic="Proactive intelligence (roadmap)",
        keywords=("knowledge debt", "stale document", "stale docs", "knowledge gap",
                  "contradiction", "duplicate document", "broken reference", "unanswered topic",
                  "obsolete procedure", "knowledge quality", "out of date docs"),
        question="Can it find stale/contradictory docs and knowledge gaps (Knowledge Debt Radar)?",
        answer=(
            "That's the planned Knowledge capability (not shipped yet): automatically detect stale "
            "documents, contradictions, duplicates, unsupported claims, broken references, missing "
            "provenance, frequently-searched-but-unanswered topics, knowledge trapped in "
            "conversations and conflicting versions. Rather than just flagging debt, it's meant to "
            "generate a proposed correction or a new KB article — with evidence lineage — and route "
            "it through approval. For example: 'users asked variants of X 37 times, retrieval "
            "confidence was low in 29, no authoritative doc covers it → draft an article covering "
            "A/B/C.'"),
        source="Roadmap — Knowledge Debt Radar (not yet shipped)"),
    KnowledgeEntry(
        id="knowledge-frontier",
        topic="Proactive intelligence (roadmap)",
        keywords=("knowledge frontier", "learnerbot", "learner bot", "socratic", "concept map",
                  "what to learn next", "next concept", "mastery", "misconception",
                  "learning frontier", "what to teach next", "onboarding path", "skill gap"),
        question="How does the stack decide what to learn or teach next (Knowledge Frontier)?",
        answer=(
            "The Knowledge Frontier kernel ships today (validated on controlled synthetic learners, "
            "not yet on real ones) — a DOMAIN-NEUTRAL engine, not a tutoring product. 'Establish the "
            "map before traversing every branch': it models a body of knowledge as a prerequisite "
            "graph of concepts, tracks each concept's state (not-encountered, exposed, uncertain, "
            "probably-understood, misconception, retention-at-risk, mastered), and picks the next "
            "concept by structural importance, prerequisites met, uncertainty, misconception risk and "
            "retention need — choosing to TEACH, REVIEW, ASSESS (probe an unverified belief before "
            "investing in teaching) or STOP, and optimising demonstrated mastery, not content consumed. "
            "On the synthetic benchmark it reaches far more mastery per teaching step than naive "
            "orderings (never wasting a step on a prereq-blocked concept), and — for an entity that may "
            "already know things — probing first reaches more true mastery per unit effort than "
            "teaching blindly. That proves the selection LOGIC, NOT real-world pedagogy. It's a "
            "'learner'-neutral primitive (a person, a new hire onboarding, an agent building "
            "competence, a team closing a skill gap); for agents, ASSESS generalises to ACQUIRE_EVIDENCE "
            "(inspect docs, run a test, query an app, ask a human) where a governed Mission decides HOW "
            "to close the gap. It is unrelated to the separate learnerbot.ai product."),
        source="Shipped kernel: agentic_os/knowledge_frontier.py + knowledge_frontier_backtest.py "
               "(synthetic-validated) · domain-neutral, distinct from the separate learnerbot.ai product"),
    KnowledgeEntry(
        id="analytics-anomaly-action",
        topic="Proactive intelligence (roadmap)",
        keywords=("anomaly", "why did the metric", "metric changed", "explain the metric",
                  "anomaly explanation", "metric moved", "analytics proactive",
                  "decision infrastructure", "root cause of the change"),
        question="Will analytics explain why a metric changed and what to do (Anomaly → Explanation → Action)?",
        answer=(
            "That's the planned analytics direction (not shipped yet). A conventional dashboard says "
            "'metric changed → alert'. The agentic version would decide whether the change matters, "
            "investigate the probable cause, gather evidence, generate candidate interventions, "
            "estimate each one's expected effect, recommend or (where policy allows) execute, and "
            "then measure the result — turning analytics into decision infrastructure rather than an "
            "automated dashboard narrator."),
        source="Roadmap — Analytics Anomaly→Explanation→Action (not yet shipped)"),
    KnowledgeEntry(
        id="wealth-assumption-drift",
        topic="Proactive intelligence (roadmap)",
        keywords=("assumption drift", "assumptions drift", "drift monitor", "assumptions changed",
                  "reconsider the plan", "portfolio drift", "strategy assumption", "plan assumption",
                  "plan's assumptions", "re-evaluate allocation", "reevaluate allocation",
                  "review my allocation"),
        question="Would the wealth manager tell me when my plan's assumptions no longer hold (Drift Monitor)?",
        answer=(
            "That's the planned wealth-manager function (not shipped yet) — and note it's "
            "deliberately NOT constant trading advice. It would watch for when reality has changed "
            "enough that a prior plan's assumptions deserve reconsideration: portfolio drift, risk "
            "exposure, cash flow, goals, market assumptions, tax constraints, horizon and material "
            "evidence. The message is 'your strategy was based on assumption X; evidence Y has "
            "materially changed; it's still within policy, but the assumption should be reviewed' — "
            "with a recommendation to re-evaluate. Any financial action stays fully permissioned and "
            "governed."),
        source="Roadmap — Wealth Manager Decision/Assumption-Drift Monitor (not yet shipped)"),
    KnowledgeEntry(
        id="proactive-governance-learning",
        topic="Proactive intelligence (roadmap)",
        keywords=("act on its own", "acts on its own", "how is that governed", "how is it governed",
                  "autonomy policy", "approve interventions", "how does it learn", "outcome telemetry",
                  "how is it evaluated", "how do i know it works", "calibration", "100% accuracy",
                  "does it improve over time", "who approves the action", "proactive governance"),
        question="If it acts on its own, how is that governed — and how do I know it actually works?",
        answer=(
            "Two firm principles in the design. First, every proactive intervention enters the SAME "
            "governance path as any other action regardless of which app raised it: permissions → "
            "guardrails → risk/approval classification → execute or request-approval or deny → audit. "
            "Low-risk work (retrieval, internal analysis, drafts) can run automatically; state "
            "changes depend on policy; high-risk actions (external comms, submissions, financial or "
            "production actions) need approval. Autonomy is a policy decision you set, never an "
            "inherent property of an agent. Second, no self-improvement is claimed without evidence: "
            "each capability must pass offline and online evaluation — precision, recall, false-"
            "positive rate, CALIBRATION (predicted vs observed), precision-above-threshold and "
            "acceptance/regret — and 'nearly 100% accuracy' claims are explicitly avoided. The "
            "closed loop now ships: the runtime observes outcomes and changes future action selection "
            "(a learned utility over the shared OutcomeLog), and — proven on a controlled environment "
            "— this measurably improves selection and even learns to abstain when actions backfire, "
            "while keeping approval gates intact, staying replayable (the model is a pure function of "
            "the log) and explainable (it states the prior, the observed reward and the blend). Wiring "
            "it to live production outcomes is the deployment step; no real-world lift is claimed yet.)"),
        detail=(
            "Every capability is meant to emit common telemetry — OpportunityDetected, "
            "CandidateGenerated, CandidateScored, InterventionSelected, ApprovalRequested/Accepted/"
            "Rejected/Edited, InterventionExecuted, OutcomeObserved, RewardAssigned — enabling "
            "learning at detection, routing, retrieval, evidence acquisition, candidate generation, "
            "ranking, timing, execution strategy and approval prediction, without blindly optimising "
            "one reward. Learned policies are compared against static ones with controlled cohorts or "
            "replay, and the runtime must explain why a learned strategy changed."),
        source="Governance path shipped: integrations/execution.py GovernedEnvelope (tiers+approval+audit) · "
               "Roadmap: common outcome telemetry + offline/online evaluation before any self-improvement claim"),
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
