# Edge Sentinel: decision log, ES-01 breakdown, release gates and risk register

Status: owner decisions of 2026-11-02 are recorded below. Each question shows its status, the owner's decision where one was given, and the consequence of the alternatives. Where the decision did not cover a question, the entry says so and keeps the recommended default as a proposal.

Companion documents: [SBOM_CVE_DESIGN.md](SBOM_CVE_DESIGN.md) and [COMPLIANCE_READINESS_DESIGN.md](COMPLIANCE_READINESS_DESIGN.md).

Effort figures are GUESSES in engineer-days, made from the size of the existing modules and tests, not from a measured velocity. They are for ordering the work and are not commitments.

Source of the questions: the internal read-only audit ES-00 (2026-11-01), section 11, quoted here by number. The audit's security-sensitive findings are kept in a private note and are not repeated in this public repository.

Related pull requests (public repository): #602 (Edge Sentinel demo wording and labels, owned by another agent; this log records the dependency and changes nothing it covers) and #605 (descriptive licence wording; no legal text changed).

## 1. Status at a glance

| # | Question | Status (2026-11-02) | Decision or recommended default |
|---|---|---|---|
| Q1 | Source of truth | ANSWERED: approved | public `apps/edge-sentinel` is canonical; the R0 core stays kernel-independent, enforced by a CI import test; selectively port three hardening items; retire the vendored copy |
| Q1b | Standalone repository or `apps/edge-sentinel` | ANSWERED: approved with Q1 | stay in `apps/edge-sentinel` behind the import boundary; split later only if needed |
| Q2 | Licence wording and the AGPL-3.0-only versus Commons Clause import issue | ANSWERED: approved with changes | accurate source-available wording wherever the Commons Clause applies; AGPL-only components kept distinct; package publication on hold until legal review of the exact terms including the §7 additional permission (release blocker RG1) |
| Q3 | Relabelling the demo | ANSWERED | complete #602 (other agent); remove unsupported benchmark comparisons, nonfunctional chat actions and misleading health indicators; replace the Docker-socket instructions (ES-01a5) |
| Q4 | Whitepaper gate sentence | ANSWERED by the public-claims decision (my application of it; owner to confirm for the whitepaper) | reword as planned until a capability exists and a test demonstrates it |
| Q5 | Does the running image match HEAD | OPEN: not covered by the 2026-11-02 decision | owner supplies the deployed digest; ES-01a2 adds a build identity to `/health` |
| Q6 | Demo log acquisition and seeding | OPEN: not covered | owner decision; label the demo "no live traffic acquired" if none |
| Q7 | Doris, NodePort and Wazuh exposure | OPEN: not covered; host-specific, answered privately | not a design blocker: R0 does not use Doris |
| Q8 | One shared Basic credential | ANSWERED: conditionally approved | acceptable for single-operator, single-tenant R0 under requirements OC1 to OC8 (section 3) |
| Q9 | Export vocabulary | ANSWERED: approved as written | keep the assessor's six states internally, map to qa-evidence native statuses on export, only the check program writes verdicts |
| Q10 | R0 scope | ANSWERED: approved | vulnerability assessment plus 4 to 6 deterministic read-only Linux host probes; excludes ban enforcement, nmap, ZAP, nuclei and any SOC 2 certification claim |
| Q11 | Which code base carries the name | OPEN: implied by Q1 (this app); owner to confirm | the name applies to `apps/edge-sentinel` only |
| Q12 | Active scanning (nmap, ZAP, nuclei) | ANSWERED: excluded from R0 | |

Evidence architecture (SBOM and vulnerability design, sections 7 to 13): APPROVED as written on 2026-11-02. It keeps detected, potentially affected, not affected (VEX-backed only) and unassessed; keeps scanner artifacts distinct from independently verified findings; fails closed on missing feeds, failed scanners and unavailable evidence; starts with SQLite and files, Doris later.

## 2. Questions from ES-00 section 11

### Q1. Which tree is the source of truth? ANSWERED 2026-11-02

Owner decision: the public `apps/edge-sentinel` is canonical. The core stays kernel-independent, enforced by a CI import test. Selectively port the three extra hardening items from the deployed copy. Do not maintain two divergent implementations; the vendored copy becomes a generated or removed artifact and is never hand-edited.

Evidence behind it (ES-00 section 2.1):

* Of the 27 same-named modules, 13 differ (`app`, `core`, `live`, `ipguard`, `ratelimit`, `evaluation`, `statefile`, `operator`, `response`, `attack`, `incident`, `case_store`, `evidence`); 14 are identical.
* The deployed (private, vendored) tree holds hardening the public tree lacks; the public tree holds modules the deployed tree lacks (route-authorisation registry and permissions seam, governed model egress, other input guards, the SOC 2 library, the agent-event observer, the manifest and producers modules).
* The vendored README says it is generated from the public app and must not be hand-edited. That is not true today. A synchronisation play copies the public `.py` files over the vendored ones without deleting, which would overwrite deployed-only modules.
* CI: the public repository runs every `apps/*/` test directory as a merge gate (443 tests for Edge Sentinel after #602, 417 before); the private stack's CI runs only its root `tests/`, so 6 of its 18 vendored tests fail against the pinned kernel unnoticed.

The three hardening items to port (public-safe description; the deployed copy is private, and details of the audit's security findings stay in the private note):

1. Read the detection engine's alerts through its authenticated local API (a machine login) instead of running commands inside its container. The public tree still shells out to the container runtime for this, which is why its README tells users to mount the runtime socket. Porting this removes the need for any socket on the alert path.
2. Report the real state of the state store and of the alert feed in `/health`, so a health indicator reflects data actually read and fails visibly when a feed or the store is unavailable. Add a build identity to the same response (Q5).
3. A global daily cap on state-changing requests, on top of the existing per-client limit. Port it with a design requirement: unauthenticated traffic must not be able to consume the authenticated operator's allowance (separate budgets, tested).

Planned as ES-01a1, ES-01a2 and ES-01a3 in section 4, each a small PR with tests, merged before anything is deployed from the public tree.

Retirement and synchronisation plan for the vendored copy:

| Phase | Action | Exit test |
|---|---|---|
| 0, freeze | no hand edits to the vendored copy from now on; its README header states it is a deployment artifact and names the public commit it must equal | a CI check in the deployment repository fails on any change to the vendored tree that is not a regeneration |
| 1, port | ES-01a1 to ES-01a3 land in the public tree with tests; ES-01b handles the private findings | the public tree passes the ported tests; a diff of every same-named module shows only intentional differences, listed |
| 2, replace the sync | delete the synchronisation play. The deployed image is built from a pinned public commit or tag; deployment-specific values live in configuration and overlay files, not in forked modules | the build is reproducible from the pin; a drift check compares the deployed overlay with the public tree and fails on any same-named module that differs |
| 3, remove | delete the vendored copy once the demo is deployed from the pinned build and its smoke checks pass; keep a tag of the last vendored state for rollback | no `agents/edge-sentinel` source remains in the deployment repository; the demo smoke checks pass on the new build |
| Rule | never two live implementations; a needed deployment difference becomes configuration or an upstream change in the public tree | the drift check stays in CI |

Planned as ES-01a4 in section 4.

Consequences of the alternatives (kept for the record): private tree as the source would make the public package unreproducible and stop the merge gate; a new repository now would copy the divergence; keeping both with a sync is the failure that exists today.

### Q1b. Standalone repository or `apps/edge-sentinel`. ANSWERED 2026-11-02 (approved with Q1)

The R0 core (`vuln/`, `probes/`, report and export code) imports only the standard library and the existing `soc2/` modules, which already import only the standard library (checked: no import of `agentic_os` or `context_runtime` under `soc2/`). A CI test fails if the core imports anything else. The web app, chat console and response operator keep their kernel imports and are outside the R0 package. A later split into its own repository is then a move, not a rewrite.

### Q2. Licence wording and the AGPL-3.0-only versus Commons Clause import issue. ANSWERED 2026-11-02 (approved with changes)

Facts: the repository is source-available under AGPL-3.0-or-later with the Commons Clause (`LICENSE.md`); `apps/*` are declared AGPL-3.0-only with a plain AGPL `LICENSE` each; reconciling the two needs an AGPL §7 additional permission that the file says is being finalised and that does not exist in the repository. The Edge Sentinel app imports Commons-Clause code (the operator SDK, the app gate and the Context Runtime integrations). `context-runtime` and `redevops-qa` carry the Commons Clause as well.

Owner decision:

1. Replace "open source and sellable" with accurate source-available wording wherever the Commons Clause restrictions apply, and keep AGPL-only components clearly distinguished. The descriptive fix is the separate pull request #605; it changes no licence grant or legal text, and it lists for counsel every file whose legal text is inconsistent (the 21 app `LICENSE` files, the `Software:` trailer of `LICENSE-COMMONS-CLAUSE`, version-expression mismatches) without changing them.
2. Hold any package publication until the exact licence terms, including the §7 additional permission, have had legal review. This is release blocker RG1 (section 5).

Wording to use in all Edge Sentinel documents and copy:

| Component | Wording |
|---|---|
| kernel, `context-runtime`, `redevops-qa` (including the qa-evidence adapter), `runtime-contracts` | "source-available under the family licence (AGPL-3.0-or-later with the Commons Clause)"; never "open source" |
| `apps/edge-sentinel` and the R0 core (standard-library-only, no kernel import) | "AGPL-3.0-only by the app's own LICENSE file, subject to the pending additional permission"; not called open source until RG1 clears |
| third-party tools (Syft, Trivy, Grype, OSV-Scanner and others) | "Apache-2.0 licensed" (or the actual licence); their licence is described, not ours |

Why keeping the R0 core kernel-free matters: its own licence then does not depend on the permission text. Only the optional qa-evidence adapter (family licence) and the web app remain to be resolved by RG1.

Consequences of the alternatives: publishing before the review would ship an app whose declared licence and imported code conflict; relicensing the app under the family licence removes the conflict but contradicts the repository's statement that apps are AGPL-only; dropping the Commons Clause from the kernel is rejected by the repository's own reasoning. Third-party notices are needed in every case (RG7).

### Q3. Public claims. ANSWERED 2026-11-02

Owner decision: complete #602's corrections (owned by another agent: this log records the dependency and does not duplicate it), remove unsupported benchmark comparisons, nonfunctional chat actions and misleading health indicators, and replace the Docker-socket instructions with least-privilege deployment guidance, never requiring unrestricted socket access by default.

What #602 covers (from its description, not re-verified line by line here): the status banner, header pills and KPI wording, the sticky banner's learned-policy sentence (reworded as a separate offline benchmark that does not run in the app), the two unrunnable chat chips, the chat prompt and tool text, sample-data labels, dry-run wording, and the README banner. It is open at the time of writing. Everything in this log that touches the same files (`apps/edge-sentinel/README.md`, `app.py`, `core.py`) is sequenced after #602 merges.

Not covered by #602 and planned here:

* The Docker-socket instructions and code path: the exact places are listed in section 6, and the guidance is in section 12.1 of the SBOM and vulnerability design. Planned as ES-01a5.
* Stale present-tense lines that #602 lists as outside its scope: the lead sentence of `ARCHITECTURE.md`, a docstring in `operator.py`, the capability yaml and an ansible comment in the deployment copy.
* A health indicator that reflects real data (the second hardening item, ES-01a2).
* The site navigation blurb and the whitepaper sentence live in other repositories (Q4).

Dependency order: #602, then ES-01a1 to ES-01a5.

### Q4. Whitepaper v6 gate sentence. ANSWERED by the public-claims decision (owner to confirm for the whitepaper)

The sentence says "no image ships with a critical CVE" as a deploy gate of Edge Sentinel's supply-chain scan. The real operator exposes three capabilities (`sentinel.triage`, `sentinel.block_ip`, `sentinel.unblock_ip`); `supply_chain_scan` exists only as a stub in two test and demo files. Applying the decision to remove unsupported claims: reword it as planned until ES-01d and ES-01h exist and a test demonstrates a gate. The whitepaper lives outside this repository; the owner applies the rewording there.

### Q5. Does the running image match repository HEAD. OPEN

Not covered by the 2026-11-02 decision. Cannot be answered from the repository: the observed 404 on a route the pinned kernel mounts suggests it may not. Proposal: the owner supplies the deployed image digest; ES-01a2 adds an image or build identity to `/health` so a third party can compare it with a release.

### Q6. Demo log acquisition and seeding. OPEN

Not covered. The repository's compose declares no log source for the detection engine, so no real traffic is analysed unless the host says otherwise; the demo showed zero decisions and zero alerts at audit time. Proposal: the owner decides whether the demo is repopulated; if not, the demo says "no live traffic acquired" (the status wording in #602 already describes a quiet reading by what was read and when).

### Q7. Doris, NodePort and Wazuh exposure. OPEN

Not covered; host-specific and security-sensitive, answered privately. This repository carries no host details. Engineering's part is that the R0 package does not use Doris (D1).

### Q8. One shared Basic credential. ANSWERED 2026-11-02 (conditionally approved)

Owner decision: one credential is acceptable for single-operator, single-tenant R0 provided it is securely stored, rotatable, never embedded in images or logs, all privileged operations are authenticated and audited, and there are no anonymous write endpoints. Multi-user identity and tenant authorisation remain Enterprise.

The conditions are written as design requirements OC1 to OC8, each with an acceptance test, in section 3.2. They are acceptance gates for ES-01c (package) and ES-01i (credential conditions).

### Q9. Evidence vocabulary on export. ANSWERED 2026-11-02 (approved as written)

Keep the assessor's states (PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW) as the product vocabulary, map them to qa-evidence native lane statuses on export (compliance design, section 6), and never write a verifier verdict (`VERIFIED`, `FAILED`, `INCONCLUSIVE`, `UNACCOUNTED`) from the assessor or the probes. Only the separate check program writes verdicts. Adopting the verdict vocabulary inside the assessor would let the producer grade itself.

### Q10. Scope of R0. ANSWERED 2026-11-02 (approved)

R0 is vulnerability assessment plus 4 to 6 deterministic, read-only Linux host probes. Excluded from R0: ban enforcement, nmap, ZAP, nuclei, and any SOC 2 certification claim. The host pack is labelled "original baseline, not CIS-certified" everywhere it appears (probe names, report headings, package copy). The ban workflow is the best-tested part (86 and 96 tests) and stays in the app as the template for the post-R0 response workflow (ES-01h), not an R0 feature.

### Q11. Four or five things called Edge Sentinel. OPEN (implied by Q1)

Not separately answered. The decision that the public `apps/edge-sentinel` is canonical implies that the name applies to this app only; the ECR scan-and-harden operator in the demo repository and the security-compliance app with a canned-findings learner would then be labelled as demos in their own repositories. Owner to confirm.

### Q12. Active scanning (nmap, ZAP, nuclei). ANSWERED 2026-11-02 (excluded from R0)

Excluded, as part of the R0 scope in Q10. Active scans raise scope, consent and third-party-systems questions that read-only inventory does not, and the nmap licence restricts bundling (ES-00; not re-read here). Any later proposal needs its own consent and scope design and a licence review.

## 3. Design decisions and requirements

### 3.1 Design decisions

| # | Decision | Default | Consequence of the alternative |
|---|---|---|---|
| D1 | R0 store | SQLite and files for one host; a Doris adapter later for fleets (approved 2026-11-02) | Doris in R0 adds a database server to a one-host install and cannot hold the model (SBOM design, section 9.3) |
| D2 | Who decides a finding's state | Edge Sentinel's own matcher over a frozen snapshot; scanners are native artifacts (approved 2026-11-02) | trusting one scanner's verdict makes the state depend on its database and removes offline reproducibility |
| D3 | Absent evidence | INCONCLUSIVE at probe level; NOT_TESTED at control level when no probe ran; never PASS (compliance design, section 3.3) | one word for both loses the difference between "ran and could not see" and "never ran" |
| D4 | Installer | one self-contained package per supported platform (Linux first), documented install and uninstall listing every file and service created, scanner versions and checksums pinned, no private registry, no `curl \| sh`; a container image is a second artifact | an image-only install requires a container runtime and tempts a socket mount; a script installer without checksums repeats the audited pattern |
| D5 | Feed handling | fetch on the operator's machine; never redistribute a snapshot; fixtures contain only synthetic advisories; restricted data is not bundled until the release gates clear | shipping real snapshots raises per-source obligations (attribution, share-alike, and a no-derivatives source in the OSV set) |
| D6 | CIS content | own probes, called "original baseline, not CIS-certified"; no CIS text or numbering; no CIS conformance claim; an optional adapter runs operator-supplied SCAP content and keeps the result as a native artifact; any crosswalk to CIS numbers is blocked by RG6 | consuming CIS content on the operator's behalf conflicts with the non-member terms read on 2026-11-01 |
| D7 | qa-evidence consumption | optional extra pinned to a commit or tag; no new field, role or vocabulary; the package's `verify` is the acceptance test in CI; the adapter is the only family-licence import in the R0 package | vendoring drifts from the QA workstream; a hard dependency couples release cycles; extending the format would change a shared contract and is not proposed. The format is not yet released, so the pin moves with a QA tag |
| D8 | Wording guard | scan string values as well as keys in every export, report and public page text, with a forbidden-phrase list | keys-only checking (today) misses a free-text claim |
| D9 | Consent and scope | a scope file with a recorded acknowledgement; anything outside it refused | implicit scope makes misuse and accidental third-party scanning possible |
| D10 | Container-runtime access | none by default and none in R0; opt-in tiers with an allow-listed API surface (SBOM design, section 12.1) | a required raw socket mount gives the container effective control of the host |

### 3.2 Operator credential requirements for R0 (from Q8)

R0 supports one operator credential for a single operator and a single tenant. Multi-user identity, per-user approvals and tenant authorisation are Enterprise and are not built here. Each requirement has an acceptance test that must pass before the package is published.

| # | Requirement | Acceptance test |
|---|---|---|
| OC1 | Securely stored. The credential is supplied as a file readable only by the service user (mode 0600) or a reference to a secret store; never a default value; never in the repository | start with a 0644 credential file: the service refuses to start or refuses every privileged call; a secret scan over the repository finds no credential; with the credential unset every privileged call refuses (503) and reads still work |
| OC2 | Rotatable. Replacing the file and reloading changes the credential without rebuilding an image or editing code; the old value stops working at once; rotation is audited | rotate: the old value gets 401, the new value succeeds, an audit record of the rotation exists |
| OC3 | Never embedded in images or logs. Build contexts exclude it; logs, audit records, error bodies, reports and evidence bundles never contain it; the `Authorization` header is redacted | CI builds the image and scans every layer and the environment for a test credential: zero hits; run with the test credential set and capture all logs, audit records, error responses and exports: zero hits |
| OC4 | All privileged operations authenticated. The privileged list is explicit: every route or command that changes state, stages an action, starts a scan, or approves or executes something | a test walks the route table: every non-GET route, and any GET with side effects, either requires the credential or is declared stateless and proven so |
| OC5 | All privileged operations audited. Each writes an append-only, hash-chained record: time, principal label, operation, operation digest, outcome, source address; failed authentication attempts are recorded | perform each privileged operation: a record exists and the chain verifies; edit a record: verification fails |
| OC6 | No anonymous write endpoints. An unauthenticated request can never change state or stage an action. Anonymous endpoints that remain are read-only or stateless, and the state digest is unchanged by them. This includes the anonymous "respond" and "stage" calls and the case synchronisation that today runs on a GET | with no credential, every POST in the route inventory returns 401 or 503 or leaves a state digest unchanged; the GET of the dashboard and the case list do not mutate state |
| OC7 | Single operator, single tenant stated, not implied. The package README and `doctor` say so; configuring a second principal or a tenant is refused with a message that names the Enterprise edition | configure two principals: refused; `doctor` output contains the single-operator statement |
| OC8 | Fail closed and no lockout. A missing credential refuses privileged calls; a wrong credential gets 401 with a constant-time comparison; failed attempts are throttled in a budget separate from the authenticated operator's, so unauthenticated traffic cannot exhaust it | exceed the failed-attempt budget anonymously: the valid credential still works; compare timings of a wrong and a nearly right credential within a stated tolerance |

## 4. ES-01 PR breakdown

Each PR ships independently, is docs-checked and test-gated, and states what it does not claim. Effort is in engineer-days, GUESSES. "Acceptance" lists tests that must exist and pass before merge. R0 is ES-01a to ES-01g plus ES-01i; ES-01h (response workflow) comes after R0.

| PR | Scope | Acceptance tests | Effort (GUESS) | Depends on |
|---|---|---|---|---|
| ES-01a1 Alert path without container exec | port the authenticated local-API alert read (hardening item 1) into the public tree; remove the container-exec path and its socket dependency | existing tests pass; a fake detection-engine API drives the alert read; the exec path is gone and a test fails if a subprocess to the container runtime is spawned on any alert path | 2 to 3 | #602 merged, Q1 |
| ES-01a2 Real health and build identity | port state-store and alert-feed health (item 2); add build identity | `/health` is `degraded` when the store is unreadable or the alert login fails, and ok otherwise; payload carries a build identity; a test fails if a health value is a static string | 1 to 2 | a1 |
| ES-01a3 Global daily cap with separate budgets | port the cap (item 3) with an authenticated-operator budget separate from anonymous traffic | 429 with `Retry-After` at the cap; exhausting the anonymous budget leaves the operator's calls working; restart behaviour stated and tested | 1 to 2 | a1 |
| ES-01a4 Retire the vendored copy | phases 0 to 3 of the retirement plan in Q1: freeze, replace the synchronisation play with a pinned build, drift check, removal | the drift check fails on any same-named module that differs; the demo builds from the pinned public commit and its smoke checks pass; no vendored source remains | 2 to 3 | a1 to a3 |
| ES-01a5 Least-privilege deployment guidance | rewrite the Docker-socket instructions (list in section 6), default compose and Dockerfile comments, and a doctor check; remove the raw socket mount from every default | a test fails if a default compose file, Dockerfile or README command mounts the runtime socket outside a labelled opt-in section; the R0 install runs with no socket present; a proxy test (fake) shows POST, exec and `containers/*/json` are refused | 2 to 4 | #602 merged |
| ES-01b Private findings | fix the private findings list in the public tree where it applies and in the deployed overlay | each fix has a negative test; no detail in this repository | 3 to 5 | Q1, owner's private note |
| ES-01i Operator credential conditions | requirements OC1 to OC8 | each acceptance test of section 3.2 | 3 to 5 | a1 to a3 |
| ES-01c Packaging skeleton | R0 package boundary (kernel-free, CI import test), install and uninstall with a file manifest, pinned scanner install with checksums, `doctor` (prerequisites, versions, feed and scanner freshness, container-runtime access reported as none), offline mode, third-party notices | install on a clean Linux VM or container, `doctor` passes, uninstall leaves nothing in the manifest behind; install with a missing prerequisite fails with a named reason; import test fails on a kernel import in the core | 5 to 8 | Q1b, D4; publication needs RG1 to RG7 |
| ES-01d1 Feed snapshot and failure semantics | snapshot builder and reader, manifest digests, freshness record, loud failure for every case in the SBOM design section 11 | stale-feed fixtures (dead feed, empty body, schema change, missing required feed, old snapshot) all exit 3; snapshot digest is stable across macOS and Linux; no network in offline mode (a test blocks sockets) | 5 to 7 | |
| ES-01d2 Matcher, states and VEX | normalised advisory model, ecosystem version comparison for the first ecosystems (list to be decided), state model, VEX import, suppression with expiry, evaluation trace | the ten fixtures of SBOM design section 10 produce the stated results; patched and not-applicable fixtures never yield `detected`; byte-identical output across two runs; CPE-only never exceeds `potentially affected` | 8 to 12 | d1 |
| ES-01d3 Inventory and scanner adapters | Syft inventory, Trivy, Grype, OSV-Scanner adapters, isolation profile, native artifact capture, redaction | each adapter against a recorded scanner output fixture and, in a separate non-gating job, against the real binary; a scanner exiting 0 with nothing scanned is exit 3; scanners run with no network and a read-only target; zero-component SBOM fails | 6 to 9 | c, d1 |
| ES-01e1 Probe contract and first pack | probe declaration, runner, evidence capture, 4 to 6 probes from the compliance design section 4.3, labelled "original baseline, not CIS-certified", wired into the existing assessor and ledger | per probe: misconfigured fixture gives FAIL, corrected gives PASS, absent evidence gives INCONCLUSIVE, inapplicable gives NOT_APPLICABLE where defined; a test fails if a probe reads outside its `reads`; probes never write; a test fails if "CIS" appears in a result, report or package string other than the disclaimer | 8 to 12 | c |
| ES-01e2 Report formats and wording guard | JSON and SARIF output, executive summary, crosswalk with criteria version and rationale, missing-evidence report, value-scanning wording guard | SARIF validates against the published 2.1.0 schema in CI (schema source to be confirmed: UNVERIFIED); a report containing any forbidden phrase fails; a report with no missing evidence still contains the missing-evidence section | 4 to 6 | e1 |
| ES-01f qa-evidence export | optional adapter building a bundle from the output directory; independent check program; offline verification in CI | `python -m qa_evidence verify` accepts the golden bundle for each fixture; altering one byte of a native artifact, an observation or the manifest is reported; evidence copied from native bytes is refused; wrong-asset evidence is flagged; a dead-feed run cannot be `VERIFIED`; the package absent means the feature is off | 5 to 8 | d2, e1; QA tag for the pin |
| ES-01g Claims ledger and reproducibility | CI that runs everything from a clean clone offline; a test that public copy carries no unbacked claim string; the capability table with Implemented, CI-tested, Experimental, Planned labels generated from tests | clean-clone run is green with the network disabled; the claims test fails on a seeded unbacked phrase | 3 to 5 | a to f, i |
| ES-01h Response workflow, dry-run slice (after R0) | proposal object with operation digest; approval binding; dry-run executor with read-only precondition checks; dry-run receipts; seeded findings; the authority check interface that refuses until the identity workstream supplies one | seeded finding in dry-run mode causes no write with all switches off, with the enable switch on and no authority, and with a forged or reused approval; a changed operation voids its approval; a dry-run receipt cannot be promoted | 6 to 10 | d2, the identity seam (a refusing stub is enough to merge) |

Total: about 64 to 101 engineer-days (GUESS), dominated by d2, e1 and d3. The estimate excludes counsel time, the owner's decisions, the QA workstream's work and the identity workstream's work. ES-00's earlier total (36 to 56) covered a smaller scope.

Order: #602, then a1 to a3, then a4, a5 and b; i and c; d1; d2 and e1 in parallel; d3 and e2; f when a QA tag exists; g last; h after the R0 evidence path works and the identity seam exists. Nothing is published before the release gates clear.

## 5. Release gates

Package publication is on hold until every blocker below is cleared. Until then: do not bundle restricted data, do not claim licensed framework coverage, and do not publish a package, a snapshot or a crosswalk. Owner "counsel" means the owner arranges legal review; engineering supplies the facts in the cited documents.

| Gate | Blocker | Owner | Cleared when |
|---|---|---|---|
| RG1 | Exact licence terms, including the §7 additional permission for the apps and the legal text of every `LICENSE` file, reviewed. The inconsistent files are listed for counsel in #605 | counsel | counsel signs off the final text; the R0 package's declared licence matches the code it imports |
| RG2 | NVD data licence and the required notice and attribution wording (data licence UNVERIFIED; the API terms are cited in the SBOM design) | counsel | counsel confirms what may be fetched, stored in evidence bundles and displayed |
| RG3 | EPSS reuse, attribution and redistribution terms (UNVERIFIED; the FIRST pages did not state them) | counsel | written terms confirmed |
| RG4 | Redistribution terms of the Trivy and Grype aggregate databases (UNVERIFIED) | counsel | terms confirmed, or the design stays at "the operator's machine downloads it" with no bundling |
| RG5 | OSV per-source terms for any bundled advisory content, including the share-alike and no-derivatives sources | counsel | none bundled, or terms confirmed per source |
| RG6 | Any CIS mapping, CIS name use in copy, or consumption of CIS-derived content | counsel | none shipped ("original baseline, not CIS-certified"), or counsel clears a specific use |
| RG7 | Third-party notices complete for every component and tool shipped | engineering with counsel review | notice file generated in CI and reviewed |
| RG8 | AICPA criteria text and reuse terms for any crosswalk wording beyond identifiers (UNVERIFIED) | counsel | identifiers only, or terms confirmed |
| RG9 | Scanner signing and checksum artefacts verified for every pinned tool (UNVERIFIED) | engineering | installer verifies each tool against a published checksum or signature |
| RG10 | Public claims corrected (#602 merged and the demo redeployed; Docker-socket guidance replaced) | owner | #602 merged, ES-01a5 merged, the deployed image identified (Q5) |
| RG11 | Operator credential requirements OC1 to OC8 pass | engineering | the acceptance tests in section 3.2 pass in CI |

## 6. Places to change for least-privilege deployment (ES-01a5)

Found in the public tree at commit a94323a by searching for the runtime socket. Edits to the Edge Sentinel files wait for #602.

| Place | Today | Change |
|---|---|---|
| `apps/edge-sentinel/README.md` (the `docker run` example and the paragraph on the socket group) | tells users to mount the runtime socket and add its group | remove; say R0 needs no runtime access; describe the opt-in tiers of the SBOM design section 12.1 under a clearly labelled heading |
| `apps/edge-sentinel/Dockerfile` (header comments) | explains mounting the socket for the alert path | rewrite once the alert path no longer uses the container runtime (ES-01a1) |
| `apps/edge-sentinel/core.py` (the `cscli` over container-exec path) | runs commands inside the detection engine's container, with `sudo` | replaced by the authenticated local-API read (ES-01a1); the exec path is deleted |
| `apps/edge-sentinel/seed.py` (comment) | `sudo` for the socket | note that seeding uses the engine's own CLI on its own host or the local API, not a socket from Edge Sentinel |
| `apps/README.md` (socket group paragraph) | generic instruction to add the socket group | reword as a risk statement with the opt-in tiers, or remove for apps that do not need it |
| `apps/compliance/README.md`, `apps/compliance/Dockerfile`, `apps/compliance/scan.sh` | mount the socket for image scans | same guidance (this is another app; list only, owner decides) |
| `deploy/docker-compose.yml` (the reverse proxy's provider mount) | mounts the socket read-only into the proxy | not Edge Sentinel; note the same risk (a read-only socket mount does not make the API read-only) and decide separately |
| `docker-compose.yml` (existing note) | already says not to mount the socket | keep; point to the new guidance |
| other apps' `seed.py` comments about `sudo` for the socket | comment only | no change needed for Edge Sentinel |

## 7. Risk register

Likelihood and impact are my qualitative estimates (low, medium, high), not measured.

| # | Risk | L | I | Mitigation | Trigger to revisit |
|---|---|---|---|---|---|
| R1 | A package is published before the legal review, shipping a licence conflict | medium | high | RG1 hold; kernel-free core; #605 wording | any publication proposal |
| R2 | The two trees diverge again | high | medium | one source of truth; drift check; retirement plan (Q1) | CI drift failure |
| R3 | A feed licence or API term is misread (NVD data licence, EPSS reuse, aggregated scanner databases are UNVERIFIED) | medium | medium | fetch on the operator's machine, never redistribute, synthetic fixtures, RG2 to RG5 | any request to bundle data |
| R4 | A dead or partial feed produces an apparently clean report | medium | high | failure semantics (SBOM design section 11) with a test per case | any incident of an empty result |
| R5 | Report or marketing wording drifts into "compliant", "clean", "exploitable" or "CIS" | medium | high | wording rules, value-scanning guard in CI, claims ledger | any new public copy |
| R6 | A scanner binary or its installer is compromised | low | high | pinned versions and checksums, no `curl \| sh`, scanners isolated with no network, RG9 | any change to the install path |
| R7 | The qa-evidence format changes before release | medium | medium | pin to a commit or tag, optional extra, golden bundles in CI; no dependency from the QA side | QA announces a version bump |
| R8 | The identity seam is late | medium | low | ES-01h merges with a refusing stub; nothing else depends on it | identity milestone slips |
| R9 | CIS terms are broader than read here | medium | medium | own checks only, no CIS text or numbering, RG6 | any CIS-related copy |
| R10 | NVD enrichment gaps make absence of a CPE look like absence of risk | high | medium | NVD as secondary hint; CPE-only never above `potentially affected`; `unassessed` for what cannot be judged | NIST status changes |
| R11 | The vault is not WORM and keys live on the host, so a local attacker can rewrite evidence | medium | medium | external anchor of the manifest digest and vault head; say so in every export | first customer-facing export |
| R12 | Scope misuse: a user points the scanner at systems they do not control | low | high | declared scope file with acknowledgement, allow-list, no active scanning in R0 | any network-facing mode |
| R13 | A probe profile is too weak, so PASS gives false comfort | medium | medium | the profile digest is part of the evidence; the summary prints the profile name and version | profile review |
| R14 | Effort estimates are low (they are guesses) | medium | medium | PRs ship independently so partial progress stands | after d2 |
| R15 | The demo keeps unbacked statements while the package is built | medium | medium | #602, then ES-01a; RG10 | each demo release |
| R16 | The single operator credential is mistaken for multi-user identity | medium | medium | OC7 states it in the README and `doctor`; second principal refused | any multi-user request |
| R17 | Porting the daily cap lets anonymous traffic lock out the operator | medium | medium | separate budgets, tested (ES-01a3, OC8) | cap change |
| R18 | #602 and the ES-01a PRs edit the same files | medium | low | sequence after #602 merges | #602 changes |

## 8. Questions still needing the owner

1. Q5: the deployed image digest.
2. Q6: whether the demo is repopulated or labelled "no live traffic acquired".
3. Q7: the private exposure questions (answered privately).
4. Q11: confirm the name applies to `apps/edge-sentinel` only.
5. Q4: apply the whitepaper rewording (the document is outside this repository) and confirm it.
6. A QA tag of qa-evidence to pin for ES-01f, and agreement with the QA owner on the vocabulary mapping (Q9 is approved as written; the QA owner has not been shown it).
7. Counsel: RG1 to RG6 and RG8.
