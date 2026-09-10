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
    answer: str                   # authoritative, business-readable but exact
    source: str = ""              # where the behaviour is defined/enforced


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
