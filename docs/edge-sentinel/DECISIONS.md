# Edge Sentinel: decision log, ES-01 breakdown and risk register

Status: PROPOSED. Each entry gives a recommended default and the consequence of the alternatives. Nothing is decided until the owner answers; entries marked "owner" cannot be settled by engineering.

Companion documents: [SBOM_CVE_DESIGN.md](SBOM_CVE_DESIGN.md) and [COMPLIANCE_READINESS_DESIGN.md](COMPLIANCE_READINESS_DESIGN.md).

Effort figures are GUESSES in engineer-days, made from the size of the existing modules and tests, not from a measured velocity. They are for ordering the work and are not commitments.

Source of the questions: the internal read-only audit ES-00 (2026-11-01), section 11, quoted here by number. The audit's security-sensitive findings are kept in a private note and are not repeated in this public repository.

## 1. Recommended defaults at a glance

| # | Question | Recommended default | Needs the owner |
|---|---|---|---|
| Q1 | Source of truth | the public tree `apps/edge-sentinel`; the deployed copy becomes a thin overlay on a pinned release | yes |
| Q1b | Standalone repository or `apps/edge-sentinel` | stay in `apps/edge-sentinel` through ES-01a to ES-01g; the R0 core imports nothing from the kernel, so it can be split later without a rewrite | yes |
| Q2 | Licence and the AGPL-3.0-only versus Commons Clause import issue | settle the §7 additional permission before any public package; until then use "source-available (family licence)" wording and publish no package | yes (counsel) |
| Q3 | Relabelling the demo | finish the remaining public-tree items listed below; do not touch the site again | yes |
| Q4 | Whitepaper gate sentence | describe it as planned until a capability exists | yes |
| Q5 | Does the running image match HEAD | owner provides the deployed tag or digest; engineering adds an image-identity line to `/health` | yes |
| Q6 | Demo log acquisition and seeding | owner decision; label the demo "no live traffic" if none | yes |
| Q7 | Doris, NodePort and Wazuh exposure | owner answers privately; not part of the public design | yes |
| Q8 | One shared Basic credential | acceptable for the first single-operator package only, stated in the package README | yes |
| Q9 | Export vocabulary | keep the assessor's six states internally; map to qa-evidence native statuses on export; never emit verdicts from the assessor | QA owner |
| Q10 | R0 scope | vulnerability evaluation core plus host probes; the ban workflow stays in the app, out of the R0 package | yes |
| Q11 | Which of the several "Edge Sentinel" code bases carries the name | this app only | yes |
| Q12 | Active scanning (nmap, ZAP, nuclei) | exclude from R0 | yes |

## 2. Questions from ES-00 section 11

### Q1. Which tree is the source of truth?

Evidence from the divergence (ES-00 section 2.1):

* Of the 27 same-named modules, 13 differ (`app`, `core`, `live`, `ipguard`, `ratelimit`, `evaluation`, `statefile`, `operator`, `response`, `attack`, `incident`, `case_store`, `evidence`); 14 are identical.
* The deployed (private, vendored) tree holds hardening the public tree lacks: alert reading through an authenticated watcher login instead of `docker exec`, state-store health in `/health`, and a global daily POST cap. The public tree still shells out to `docker exec cscli` and its README still tells users to mount the Docker socket.
* The public tree holds modules the deployed tree lacks: the route-authorisation registry and permissions seam, governed model egress, other input guards, the whole SOC 2 library, the agent-event observer, the manifest and producers modules, and the architecture and readiness documents.
* The vendored README says it is generated from the public app and must not be hand-edited. That is not true today. A synchronisation play copies the public `.py` files over the vendored ones (no deletes), which would overwrite the deployed-only modules.
* CI: the public repository runs every `apps/*/` test directory as a merge gate (417 tests for Edge Sentinel, 15 s, offline); the private stack's CI runs only its root `tests/` and does not run the vendored app's tests, so 6 of its 18 vendored tests fail against the pinned kernel unnoticed.

Recommended default: the public `apps/edge-sentinel` is the source of truth. Reasons: it is the tree with a merge gate that runs the tests; it holds the library layer the R0 plan needs (the SOC 2 assessor, vault, ledger, case spine); public reproducibility is an R0 requirement and a private source cannot meet it; and the private tree's extra hardening is small and well delimited (three items) and can be ported with tests. The deployed copy then becomes deployment configuration plus a pinned release of the public tree, and the synchronisation play is replaced by a drift check in CI.

Consequences of the alternatives:

* Private tree as the source: the public package cannot be reproduced; the 417 tests stop gating; the SOC 2 library would have to be ported down; the "generated from open-core" notice stays false.
* A new repository now: the divergence is copied rather than resolved, the 417 tests do not move with it unless moved deliberately, and the dependency on the kernel (and its licence) comes along. See Q1b.
* Keeping both and syncing: the failure that exists today. Rejected.

### Q1b. Standalone repository or `apps/edge-sentinel`

Recommended default: stay in `apps/edge-sentinel` through ES-01a to ES-01g and enforce a package boundary: the new R0 core (`vuln/`, `probes/`, the report and export code) imports only the standard library and the existing `soc2/` modules, which already import only the standard library (checked: no import of `agentic_os` or `context_runtime` anywhere under `soc2/`). The existing web app, the chat console and the response operator keep their kernel imports and are outside the R0 package. A CI test fails if the R0 core imports anything outside that set.

Consequences: with that boundary a later split into its own repository is a move, not a rewrite, and the R0 package carries no Commons-Clause code (the qa-evidence adapter is the one optional exception, see D7). Splitting now would buy a clean release cadence and a separate issue tracker at the price of a second CI, a second licence file to keep in step, and a fork in the middle of the convergence work. Staying forever would tie the package's install story to the monorepo's.

### Q2. Licence wording, and the AGPL-3.0-only versus Commons Clause import issue

Facts: the repository is source-available under AGPL-3.0-or-later with the Commons Clause (`LICENSE.md`); `apps/*` are declared AGPL-3.0-only with a plain AGPL `LICENSE` each; reconciling the two needs an AGPL §7 additional permission that the file says is being finalised; until then the app's own LICENSE is "authoritative" and the Commons Clause "is not intended to attach". The Edge Sentinel app imports Commons-Clause code (the operator SDK, the app gate and the Context Runtime integrations). `context-runtime` and `redevops-qa` carry the Commons Clause as well. Note for the owner: `LICENSE.md` itself calls the apps "open source and sellable", which conflicts with the wording rule below until the permission text exists.

Recommended default:

1. Do not publish a standalone Edge Sentinel package until the §7 permission text is final and reviewed by counsel.
2. Wording until then: "source-available (family licence)" for anything under the Commons Clause, and for Edge Sentinel "licensed under the AGPL-3.0-only terms of the app's own LICENSE, subject to the pending additional permission". Never "open source" for the family.
3. Keep the R0 core kernel-free (Q1b) so that the package's own licence does not depend on the permission text, leaving only the optional qa-evidence adapter and the web app to resolve.

Consequences of the alternatives: publishing now would ship an app whose declared licence and imported code conflict, which downstream users and counsel will find. Relicensing the app under the family licence removes the conflict but makes the app non-sellable by others, which contradicts the repository's own statement for `apps/*`. Dropping the Commons Clause from the kernel is rejected by the repository's own reasoning (it would let an older licence set the terms of the stack). Third-party notices are needed in every case (nothing in the trees carries a third-party notice file today).

### Q3. Relabelling the demo

Done, as reported by the owner and in the public tree: the fleet catalogue tagline, the app manifest tagline and the guide card (public PR #601, merged); the navigation blurb and Apps Demo labels were changed in separate site pull requests that I have not re-read. Remaining items I can see in the public tree at commit a94323a:

* `apps/edge-sentinel/README.md` still opens with the banner that says the tenant learns which alert sources to pull, with the 0.900 versus 0.800 offline figure, in the present tense. The deployed demo does not run that learner.
* `app.py`: the "All systems normal" pill, chosen when there are zero active bans and independent of whether any sensor is acquiring data; the two chat suggestions "Scan the billing app for vulnerabilities" and "Scan a running deployment for exposure", which cannot run in the default deployment.
* The README's Docker-socket instructions (ES-00 section 2.6).

Recommended default: change these in ES-01a, with the wording rules of the two design documents. Consequence of not changing them: the demo keeps a statement the product cannot back, in the repository that the nav links to.

### Q4. Whitepaper v6 gate sentence

The sentence says "no image ships with a critical CVE" as a deploy gate of Edge Sentinel's supply-chain scan. The real operator exposes three capabilities (`sentinel.triage`, `sentinel.block_ip`, `sentinel.unblock_ip`); `supply_chain_scan` exists only as a stub in two test and demo files. Recommended default: reword as planned ("a supply-chain gate is planned") until ES-01d and ES-01h exist and a test demonstrates a gate. Alternative: leave it and implement the gate; that means asserting the capability before it exists.

### Q5. Does the running image match repository HEAD

Cannot be answered from the repository. The observed 404 on a route the pinned kernel mounts suggests it may not. Recommended default: the owner supplies the deployed image digest; ES-01a adds an image or build identity to `/health` so a third party can compare it with a release.

### Q6. Demo log acquisition and seeding

The repository's compose declares no log source for the detection engine, so no real traffic is analysed unless the host says otherwise; the demo showed zero decisions and zero alerts at audit time. Recommended default: the owner decides whether the demo is repopulated. If not, the demo says "no live traffic acquired", and the status banner (Q3) reflects it.

### Q7. Doris, NodePort and Wazuh exposure

Security-sensitive and host-specific. Recommended default: the owner answers in the private note; this repository carries no host details. Engineering's part is that the R0 package does not use Doris (D1).

### Q8. One shared Basic credential

Recommended default: acceptable for the first single-operator package if the README says so; per-user operator identity and multi-user approvals belong to the enterprise edition, and the local identity primitives (when they ship) belong to the AGPL side. Consequence of requiring per-user identity first: the package waits on the identity workstream and on an enterprise decision.

### Q9. Evidence vocabulary on export

Recommended default: keep the assessor's states (PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW) as the product vocabulary, map them to qa-evidence native lane statuses on export (table in the compliance document, section 6), and never write a verifier verdict (`VERIFIED`, `FAILED`, `INCONCLUSIVE`, `UNACCOUNTED`) from the assessor or the probes. Only the separate check program writes verdicts. Consequences: adopting the verdict vocabulary inside the assessor would let the producer grade itself, which the format forbids by design; mapping only loses the distinction between NOT_TESTED and MANUAL_REVIEW at lane level (both become `NATIVE_SKIPPED`), which is preserved in the raw status field.

### Q10. Scope of R0

Recommended default: R0 is the vulnerability evaluation core and the host probes, as the plan's one-line goal says ("find vulnerabilities, test controls, keep evidence"). The ban workflow is the best tested part (86 and 96 tests) and least related to that goal; it stays in the app and is the template for the ES-01 response workflow, not an R0 feature. Consequence of including it: the package inherits CrowdSec, the operator credential and the kernel imports, and R0 stops being a CLI-first, read-only product.

### Q11. Four or five things called Edge Sentinel

Recommended default: the name applies to this app only (`apps/edge-sentinel`). The ECR scan-and-harden operator in the demo repository and the security-compliance app with a canned-findings learner are renamed or labelled as demos in their own repositories. Consequence of leaving them: any claim about "Edge Sentinel" is ambiguous about which code stands behind it.

### Q12. Active scanning (nmap, ZAP, nuclei)

Recommended default: exclude from R0. Active scans raise scope, consent and third-party-systems questions that read-only inventory does not, and the nmap licence restricts bundling (ES-00; not re-read). Consequence of including: a consent and scope design, a rate-limit design for active tools, a licence review before an image ships nmap, and the audited scanner API's hostname-only scope check becomes a launch blocker.

## 3. Design decisions

| # | Decision | Recommended default | Consequence of the alternative |
|---|---|---|---|
| D1 | R0 store | SQLite and files for one host; a Doris adapter later for fleets | Doris in R0 adds a database server to a one-host install and cannot hold the model (SBOM design, section 9.3) |
| D2 | Who decides a finding's state | Edge Sentinel's own matcher over a frozen snapshot; scanners are native artifacts | trusting one scanner's verdict makes the state depend on its database and removes offline reproducibility |
| D3 | Absent evidence | INCONCLUSIVE at probe level; NOT_TESTED at control level when no probe ran; never PASS (compliance design, section 3.3) | using one word for both loses the difference between "ran and could not see" and "never ran" |
| D4 | Installer | one self-contained package per supported platform (Linux first), a documented install and uninstall that list every file and service created, scanner versions and checksums pinned, no private registry, no `curl \| sh`; a container image is a second artifact, not the only one | an image-only install requires a container runtime and tempts a socket mount; a script installer without checksums repeats the audited pattern |
| D5 | Feed handling | fetch on the operator's machine; never redistribute a snapshot; fixtures contain only synthetic advisories | shipping real snapshots raises per-source licence obligations (attribution, share-alike, and a no-derivatives source in the OSV set) |
| D6 | CIS content | own probes, no CIS text or numbering, no CIS conformance claim; an optional adapter runs operator-supplied SCAP content and keeps the result as a native artifact; a crosswalk to CIS numbers is a counsel question | consuming CIS content on the operator's behalf conflicts with the non-member terms read on 2026-11-01 (no derivatives, no posting, no compliance claim); bundling ComplianceAsCode content as the default makes the pack depend on a large, moving dataset |
| D7 | qa-evidence consumption | optional extra pinned to a commit or tag; Edge Sentinel adds no field, role or vocabulary; the package's `verify` is the acceptance test in CI; the adapter is the only Commons-Clause import in the R0 package | vendoring the format drifts from the QA workstream; a hard dependency couples the release cycles; extending the format would change a shared contract and is not proposed. The format's README says it is not yet released, so the pin must move with a QA tag once one exists |
| D8 | Wording guard | scan string values as well as keys in every export, report and public page text, with a forbidden-phrase list | keys-only checking (today) misses a free-text claim |
| D9 | Consent and scope | a scope file with a recorded acknowledgement; anything outside it refused | implicit scope makes misuse and accidental third-party scanning possible |

## 4. ES-01 PR breakdown

Each PR ships independently, is docs-checked and test-gated, and states what it does not claim. Effort is in engineer-days, GUESSES. "Acceptance" lists tests that must exist and pass before merge.

| PR | Scope | Acceptance tests | Effort (GUESS) | Depends on |
|---|---|---|---|---|
| ES-01a Converge and decide the home | port the deployed-only hardening (watcher-login alert path, state health, daily cap) into the public tree with tests; replace the synchronisation play with a drift check; remove the Docker-socket instructions; remaining relabelling from Q3; image identity in `/health` | existing 417 tests still pass; new tests for each ported item; a CI test fails when the deployed overlay and the public tree disagree on a hardened module; a test fails if README or UI strings contain the Q3 phrases | 4 to 6 | Q1 |
| ES-01b Hardening before any public deploy | fix the private findings list in the public tree where it applies and in the deployed overlay | each fix has a negative test; no detail in this repository | 3 to 5 | Q1, owner's private note |
| ES-01c Packaging skeleton | R0 package boundary (kernel-free, CI import test), install and uninstall with a file manifest, pinned scanner install with checksums, `doctor` (prerequisites, versions, feed and scanner freshness), offline mode, third-party notices | install on a clean Linux VM or container, `doctor` passes, uninstall leaves nothing in the manifest behind; install on a host with a missing prerequisite fails with a named reason; import test fails on a kernel import in the core | 5 to 8 | Q1b, D4, Q2 for publication |
| ES-01d1 Feed snapshot and failure semantics | snapshot builder and reader, manifest digests, freshness record, loud failure for every case in the SBOM design section 11 | stale-feed fixtures: dead feed, empty body, schema change, missing required feed, old snapshot all exit 3; snapshot digest is stable across macOS and Linux; no network in offline mode (a test blocks sockets) | 5 to 7 | |
| ES-01d2 Matcher, states and VEX | normalised advisory model, ecosystem version comparison for the first ecosystems (decide the list: for example PyPI, npm, Debian-family packages), state model, VEX import, suppression with expiry, evaluation trace | the ten fixtures of SBOM design section 10 produce the stated results; patched and not-applicable fixtures never yield `detected`; byte-identical output across two runs; CPE-only never exceeds `potentially affected` | 8 to 12 | d1 |
| ES-01d3 Inventory and scanner adapters | Syft inventory, Trivy, Grype, OSV-Scanner adapters, isolation profile, native artifact capture, redaction | each adapter against a recorded scanner output fixture and, in a separate non-gating job, against the real binary; a scanner exiting 0 with nothing scanned is exit 3; the scanner runs with no network and a read-only target; zero-component SBOM fails | 6 to 9 | c, d1 |
| ES-01e1 Probe contract and first pack | probe declaration, runner, evidence capture, four to six probes from the compliance design section 4.3, wiring into the existing assessor and ledger | per probe: misconfigured fixture gives FAIL, corrected gives PASS, absent evidence gives INCONCLUSIVE, inapplicable gives NOT_APPLICABLE where defined; a test fails if a probe reads outside its `reads`; probes never write | 8 to 12 | c |
| ES-01e2 Report formats and wording guard | JSON and SARIF output, executive summary, crosswalk with criteria version and rationale, missing-evidence report, value-scanning wording guard | SARIF validates against the published 2.1.0 schema in CI (schema source to be confirmed: UNVERIFIED); a report containing any forbidden phrase fails; a report with no missing evidence still contains the missing-evidence section | 4 to 6 | e1 |
| ES-01f qa-evidence export | optional adapter building a bundle from the output directory; independent check program; offline verification in CI | `python -m qa_evidence verify` accepts the golden bundle for each fixture; altering one byte of a native artifact, an observation or the manifest is reported; evidence copied from native bytes is refused; wrong-asset evidence is flagged by the check program; a dead-feed run cannot be `VERIFIED`; the package absent means the feature is off and nothing else breaks | 5 to 8 | d2, e1; QA tag for the pin |
| ES-01g Claims ledger and reproducibility | CI that runs everything from a clean clone offline; a test that public copy carries no unbacked claim string; the capability table with Implemented, CI-tested, Experimental, Planned labels generated from tests | clean-clone run is green with the network disabled; the claims test fails on a seeded unbacked phrase | 3 to 5 | a to f; Q2 text for the licence |
| ES-01h Response workflow, dry-run slice | proposal object with operation digest; approval binding; dry-run executor with read-only precondition checks; dry-run receipts; seeded findings; the authority check interface that refuses until the identity workstream supplies one | seeded finding in dry-run mode causes no write with all switches off, with the enable switch on and no authority, and with a forged or reused approval; a changed operation voids its approval; a dry-run receipt cannot be promoted | 6 to 10 | d2, the identity seam (refusal stub is enough to merge) |

Total: about 57 to 88 engineer-days (GUESS), dominated by d2, e1 and d3. The estimate excludes counsel time for Q2, the owner's decisions, the QA workstream's work and the identity workstream's work. ES-00's earlier total (36 to 56) covered a smaller scope (no scanner adapters, no response slice, no failure-semantics PR).

Order: a, b first (they remove the divergence and the known hardening gaps); c, d1 next; d2 and e1 in parallel; d3 and e2; f when a QA tag exists; g last; h after R0's evidence path works and the identity seam exists.

## 5. Risk register

Likelihood and impact are my qualitative estimates (low, medium, high), not measured.

| # | Risk | L | I | Mitigation | Trigger to revisit |
|---|---|---|---|---|---|
| R1 | A package is published before the §7 permission exists, shipping a licence conflict | medium | high | Q2 default: no publication; kernel-free core | any publication proposal |
| R2 | The two trees diverge again | high | medium | drift check in CI; one source of truth (Q1) | CI drift failure |
| R3 | A feed licence or API term is misread (NVD data licence, EPSS reuse, aggregated scanner databases are UNVERIFIED) | medium | medium | fetch on the operator's machine, never redistribute, synthetic fixtures, counsel review of the three UNVERIFIED items | any request to bundle data |
| R4 | A dead or partial feed produces an apparently clean report | medium | high | failure semantics (SBOM design section 11) with a test per case | any incident of an empty result |
| R5 | Report or marketing wording drifts into "compliant", "clean" or "exploitable" | medium | high | wording rules, value-scanning guard in CI, claims ledger | any new public copy |
| R6 | A scanner binary or its installer is compromised | low | high | pinned versions and checksums, no `curl \| sh`, scanners run isolated with no network | any change to the install path |
| R7 | The qa-evidence format changes before release | medium | medium | pin to a commit or tag, optional extra, golden bundles in CI; no dependency from the QA side | QA announces a version bump |
| R8 | The identity seam is late | medium | low | ES-01h merges with a refusing stub; nothing else depends on it | identity milestone slips |
| R9 | CIS terms are broader than read here | medium | medium | own checks only, no CIS text or numbering, counsel on any crosswalk (D6) | any CIS-related copy |
| R10 | Enrichment gaps in NVD make absence of a CPE look like absence of risk | high | medium | NVD as secondary hint; CPE-only never above `potentially affected`; `unassessed` for what cannot be judged | NIST status changes |
| R11 | The vault is not WORM and keys live on the host, so a local attacker can rewrite evidence | medium | medium | external anchor of the manifest digest and vault head; say so in every export; the format's own limitations printed | first customer-facing export |
| R12 | Scope misuse: a user points the scanner at systems they do not control | low | high | declared scope file with acknowledgement, allow-list, no active scanning in R0 (Q12) | any network-facing mode |
| R13 | A probe profile is too weak, so PASS gives false comfort | medium | medium | the profile digest is part of the evidence; the executive summary prints the profile name and version | profile review |
| R14 | Effort estimates are low (they are guesses) | medium | medium | the breakdown ships PRs independently so partial progress stands | after d2 |
| R15 | The demo keeps unbacked statements while the package is built | medium | medium | Q3 items in ES-01a | each demo release |

## 6. Questions the owner must answer

1. Q1 and Q1b: confirm the public tree as the source of truth and the monorepo-with-boundary home.
2. Q2: the timing and text of the §7 permission, and which wording is allowed until then; and whether `LICENSE.md` should keep calling the apps "open source".
3. Q3 and Q4: approve the remaining relabelling and the whitepaper rewording.
4. Q5 to Q7: deployed image digest, demo data policy, and the private exposure questions (answered privately).
5. Q8: confirm the single operator credential for the first package.
6. Q9: agree the vocabulary mapping with the QA owner, and ask for a qa-evidence tag to pin.
7. Q10 to Q12: confirm R0 scope, the single meaning of the name, and the exclusion of active scanning.
8. Counsel: the NVD data licence, EPSS reuse terms, redistribution terms of scanner databases, and the CIS terms for any crosswalk.
