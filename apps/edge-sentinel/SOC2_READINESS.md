# SOC 2 readiness in Edge Sentinel

Edge Sentinel owns the SOC 2 readiness layer: the control registry, the criteria references, evidence integrity, assessments, exceptions, human review and the restricted export. Other
parts of the stack (the mission kernel, Context Runtime, Agentic Compliance, infrastructure) are evidence producers. This layer produces **readiness evidence for a reviewer**. It does not produce
an attestation, an auditor's opinion, a "certified" flag or an overall compliance score, and a test run is evidence of a tested control, not of a satisfied criterion.

## What exists (`soc2/`)

| Module | What it does |
|---|---|
| `vault.py` | A durable, append-only, tenant-scoped evidence vault. Producers are authenticated (a registered key per producer and tenant list, an HMAC over the header and the payload digest); a nonce is single-use; `known_at` is the vault's clock so late evidence cannot be back-dated; rows cannot be updated or deleted through SQL; every row carries the previous row's hash; `verify_all` recomputes digests, signatures and both hash chains; reads are tenant-checked, re-verified and recorded in a custody log. |
| `controls.py` | The control catalog: 11 controls (8 with an automated procedure, 3 manual only), each with criterion references, owner role, frequency, procedure, expected evidence kinds, minimum coverage and manual dependency. Criterion references are AICPA identifiers and a starting hypothesis for the control owner and auditor to confirm; the criteria text is not reproduced. |
| `assess.py` | The assessment engine. Results: PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW. PASS needs verified evidence of the right kind for the scope and period, coverage of the period, a population the producer states is complete and non-empty (unless the control allows empty), every item meeting the procedure and no pending manual dependency. Missing data is never a PASS; a producer's own "compliant" claim is never read, only the data. |
| `ledger.py` | Append-only, hash-chained assessments, reviews and exceptions. A reassessment supersedes and keeps the old one. A review needs someone who is neither the control owner nor a producer of the evidence; a FAIL, INCONCLUSIVE or NOT_TESTED cannot be approved. An exception records who accepted the risk, until when and the remediation, expires, and never changes the result. |
| `export.py` | `restricted_pack` (controls, latest assessment, evidence references with digests, exceptions, vault and ledger integrity; a not-an-attestation banner) and `benchmark_summary` (counts by result only). `assert_no_claim` refuses an export that carries a certification or an overall result. |
| `collectors.py` | Builders that sign evidence envelopes from what the stack already produces (sentinel events, reconciliation outcomes). |

## Inventory of what existed before (ES-1)

| Existing | Status for SOC 2 | What is still missing |
|---|---|---|
| `evidence.py` content-addressed `EvidenceArtifact` with chain-of-custody strings | **PARTIAL**: the digest is verified when evidence is put in a `CaseStore` | `CaseStore` holds evidence in process memory: nothing survives a restart, nothing is signed, custody strings are not verified. The vault above is the durable path |
| `agent_events.py` (sentinel feed to cases) | **PARTIAL**: a signal path with idempotent cases | no durable cursor of its own (the feed provides one), no authenticity of the events, no tenant authorization on ingest |
| `route_policy.py` | **VERIFIED** as access-control design evidence for the app's write routes | reachability of every write path outside these routes (jobs, webhooks) is not covered here |
| `apps/compliance` (OpenSCAP/CIS) | **PARTIAL**: a configuration evidence producer | it scans a container image by default, which `SEC-CFG-01` refuses for a host control; scanner provenance is not signed |
| `evaluation.py` | **NOT USED** for SOC 2 | an evaluation corpus for assessment accuracy is a separate piece (the benchmark repository measures it) |
| A standalone `edge-sentinel` repository | **NOT FOUND**: Edge Sentinel is `agentic-os/apps/edge-sentinel` | confirm with the owner before anything is moved |

## What is not built, and must not be claimed

* **No collectors for identity providers, CI, cloud or ticketing.** The catalog's procedures read the evidence kinds named in `controls.py`; nothing in this repository gathers them from real systems yet. A control with no evidence is NOT_TESTED, not a pass.
* **No operating-effectiveness conclusion.** The first real period of evidence has not been collected. Design effectiveness (the procedure is sound) and operating effectiveness (it held over a period) are different claims; a PASS here is one control, one scope, one period.
* **The vault is not WORM storage.** A hash chain detects edits and deletions; it does not stop someone who owns the database file from rebuilding it consistently. Anchor `EvidenceVault.head()` somewhere its writer cannot reach. Producer keys live with the deployment; a stolen key can sign false evidence until revoked.
* **Human and third-party obligations stay manual**: access reviews by people, vendor risk, incident exercises, privacy requests, the auditor's work. Controls with a manual dependency end MANUAL_REVIEW.
* **Retention is recorded, not enforced**: nothing is ever deleted from the vault; classification and retention policy are the deployment's to define.
* No HTTP routes were added; the layer is a library. Putting it behind routes needs route policies like the rest of the app.

Run the tests from `apps/` with the repository on `PYTHONPATH`: `pytest edge-sentinel/test_soc2_vault.py edge-sentinel/test_soc2_assess.py edge-sentinel/test_soc2_ledger_export.py`.
