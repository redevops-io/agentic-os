# Edge Sentinel — architecture & capability boundary

Edge Sentinel is a **governed security investigation, threat-intelligence, detection-engineering and
incident-response agent**. It owns security *investigations* and *response missions*; it does **not** own
control/compliance/risk (that is Agentic Compliance) or privacy obligations (Agentic Privacy).

This document records the product boundary the codebase has reached, so claims about it stay honest.

## Three tiers

| Tier | Phases | What it is |
|------|--------|-----------|
| **Security Intelligence Core** | 0–4 | The deterministic security-reasoning substrate. Self-contained; no external systems required to run or test. |
| **Operational Integrations** | 5–9 | Attaches the substrate to external systems and other ReDevOps runtimes/apps (DFIR sources, external CTI/attack-surface, Mission-Runtime governed response, Agentic Compliance, the agentic runtime's own events). |
| **Evaluation & Learning** | 10 | Measures the resulting system on frozen labelled corpora, then allows bounded adaptation. |

The tier boundary is architectural, not cosmetic: the Core reasons over an immutable evidence graph and
depends on nothing outside this package; the Integrations tier is where Edge Sentinel consumes **real**
platform contracts (Mission Runtime, operators, runtime events) and must never quietly reimplement them.

## The Core shape (Phases 0–4, delivered)

```
runtime/security signal
        │  (immutable, content-addressed)
        ▼
   EvidenceArtifact ──► SecurityObservation ──► Finding ──► SecurityCase
        │                     │                    │
        │                     ▼                    ▼
        │              CTI graph (STIX)      ATT&CK techniques
        │              IOC ↔ indicator       (deterministic map,
        │              provenanced edges      matched-token basis)
        ▼                     │
   every finding / CTI edge / detection traces back to the SAME evidence
        │
        ├─ Investigation / Hunt: multi-source-evidence-or-ABSTAIN; queries validate before execution
        └─ Detection engineering: validate → replay(labelled corpus) → approve → publish (all gates required)
```

Modules: `evidence.py` `case_store.py` `incident.py` (P1) · `stix.py` `attack.py` `cti.py` (P2) ·
`hunt.py` (P3) · `detection.py` (P4). All pure/deterministic; run with
`PYTHONPATH=apps python -m pytest apps/edge-sentinel/`.

## What can be claimed today — and what cannot

**Delivered (true today):** evidence-native incident **cases**; structured **CTI / ATT&CK** reasoning
with provenance; evidence-sufficient **investigation/hunting with abstention**; **gated detection
engineering** (no publish without validation + replay + approval).

**NOT yet delivered — do not claim:** DFIR, external attack-surface intelligence, *governed remediation
executed end-to-end*, GRC/risk integration, agent-security monitoring, or self-learning. (The live
CrowdSec triage + approval-gated `block_ip` exist in the SOC agent, but the Core's governed-response
*integration* — Phase 7 — is not wired yet.)

## The next architectural target (not "finish 5–10")

The first serious end-to-end milestone is one invariant, proven through the real platform:

> runtime security event → immutable evidence → investigation → ATT&CK/CTI correlation → finding →
> proposed remediation → **Governance approval** → `sentinel.*` action → ActionReceipt → verification

## Phase decisions (Operational Integrations + Eval)

Implementation order is **5 → 7 → 9 → 6 → 8 → 10** (complete the evidence/investigation core, prove
safe end-to-end action, make our own stack a datasource, then add the messier external ecosystem, the
cross-app projection, and finally evaluation before adaptation).

- **5 DFIR** — build the deterministic core now: acquisition metadata, chain of custody, artifact refs,
  timeline construction/correlation. `SandboxProvider` is an **interface**; no malware sandbox is chosen
  or built. Narrative may change; source evidence never does.
- **6 CTI / attack surface** — provider-neutral enrichment **contracts** first: `DnsProvider`,
  `CertificateProvider`, `Whois`/`RdapProvider`, `ThreatIntelProvider`, `AssetExposureProvider`. Tests use
  fixtures; live providers (OpenCTI/Shodan/etc.) come afterward and are **never** architectural dependencies.
- **7 Governed response** — the first phase integrated with the real stack. **Mission Runtime owns
  `SECURITY_INCIDENT`**; Edge Sentinel only *proposes* `ActionRequest`s; existing **`sentinel.*`**
  capabilities execute **after Governance**. CACAO is import/export **interoperability, not execution
  authority**. Consume the actual mission/operator contracts — do not replicate them here.
- **8 RiskTranslation** — implement the typed contract in Edge Sentinel (evidence-backed technical facts,
  scenario, affected service, assumptions, uncertainty). **Agentic Compliance owns interpretation /
  calculation / reporting.** No FAIR engine inside Edge Sentinel; no invented financial inputs.
- **9 AI/agent security** — define a generic `RuntimeSecurityEvent` / adapter boundary rather than coupling
  to today's event serialization; adapters map Mission / Context / Discovery runtime events →
  `SecurityObservation`. This **dogfoods** Edge Sentinel on ReDevOps itself. Consume the actual runtime
  event contracts — do not invent a plausible replica.
- **10 Eval + Learn** — build **evaluation before Learn**: freeze labelled corpora, metrics, splits and a
  baseline first. Then consume `discovery_runtime.learn`; lessons may alter investigation / enrichment /
  query strategy, but **never** authorization policy or the deterministic security gates.

## Invariant that holds across all phases

Authorization and the deterministic security gates (evidence sufficiency, query read-only validation,
detection validate/replay/approve, response approval) are **not** model-decidable and **not** learnable.
A model may draft, rank, or enrich; it may never approve, publish, or act.
