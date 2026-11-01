# Edge Sentinel: compliance-readiness architecture (SOC 2 technical readiness)

Status: DESIGN. Nothing here is an implemented capability unless a row says Implemented or CI-tested and names the repository evidence. Probe names, file layouts and the report structure are design targets. The owner approved the R0 scope on 2026-11-02: vulnerability assessment plus 4 to 6 deterministic read-only Linux host probes, with no ban enforcement, no nmap, ZAP or nuclei, and no SOC 2 certification claim (see [DECISIONS.md](DECISIONS.md) Q10).

Companion documents: [SBOM_CVE_DESIGN.md](SBOM_CVE_DESIGN.md) and [DECISIONS.md](DECISIONS.md). The existing library is described in [the app's SOC 2 readiness note](../../apps/edge-sentinel/SOC2_READINESS.md).

## 0. Reading rules

Status labels: Implemented (code exists and runs, no CI test exercises it end to end), CI-tested (a test that runs in CI exercises it), Experimental (library or sample, not wired or not run against a real target), Planned (this document only). External facts cite a URL and the retrieval date (2026-11-01) or are marked UNVERIFIED; section 12 collects them.

Wording rules that bind this design and everything derived from it:

* A technical control test is never described as a completed SOC 2 audit, an attestation, or a statement that a criterion is satisfied.
* A scanner's CVE result is never described as independently confirmed exploitation.
* "Passed" in a report means: this probe, run once against this subject at this time under this version of its definition, found every checked item matching its declared expectation, with the evidence recorded. It means nothing else.

## 1. Honest scoping

In scope: technical control evidence for a selected, named set of Trust Services Criteria, gathered by deterministic read-only probes and collectors, kept as verifiable evidence, and reported with its limits.

Out of scope, and stated as such in every report (the certification claim, active scanning and ban enforcement are also excluded from R0 by the owner's decision of 2026-11-02):

* Organisational controls and policies (HR, vendor management, board oversight, risk assessment process, incident response procedures). They remain manual dependencies; a control that depends on one ends MANUAL_REVIEW, never PASS (the existing assessor already works this way).
* Operating effectiveness over time. A probe run is a point in time. A series of runs is supporting evidence; the product does not conclude that a control operated effectively over a period. Design effectiveness (the procedure is sound) and operating effectiveness (it held) are separate claims, and the library's own notes already say so.
* Attestation. Only a licensed CPA firm issues a SOC 2 report; Edge Sentinel produces readiness evidence for a reviewer, with a banner saying so. (That CPA firms issue SOC 2 reports is general knowledge; the AICPA pages I retrieved did not state it: UNVERIFIED as a cited fact.)
* Any overall score, "compliance percentage", "compliant" or "certified" flag.
* The text of the criteria (release gate RG8, owner: counsel, covers any crosswalk wording beyond identifiers). The catalog holds identifiers only; the criteria are AICPA content with their own terms (UNVERIFIED: the AICPA copyright page was not retrieved; the existing library already avoids reproducing criteria text and this design keeps that).

Criteria version: the catalog references the 2017 Trust Services Criteria with the revised points of focus of 2022. The AICPA states the 2022 revision does not change the criteria themselves (secondary extract of the AICPA resource page, retrieved 2026-11-01; https://www.aicpa-cima.com/resources/article/2017-trust-services-criteria-with-revised-points-of-focus-2022 appeared in the search results and was not read directly: UNVERIFIED at the primary page). Each crosswalk entry records the criteria version so a later AICPA revision is a visible change, not a silent one.

## 2. What exists today

From the read-only audit ES-00 (2026-11-01) and the public tree at commit a94323a. The library lives in `apps/edge-sentinel/soc2/` (1,755 lines) with 169 tests in `test_soc2_*.py`, which run in the repository's CI (`tests.yml` runs one pytest per `apps/*/` directory).

| Component | Status | What it does | What it does not do |
|---|---|---|---|
| Evidence vault (`vault.py`) | CI-tested (20 tests) | append-only, tenant-scoped SQLite; producers authenticated by a registered key (HMAC over header and payload digest); single-use nonce; `known_at` set by the vault so late evidence cannot be back-dated; hash chain; reads re-verified and logged | not WORM: someone who owns the database file can rebuild it consistently; keys live with the deployment; a stolen key signs false evidence until revoked; the head must be anchored somewhere the writer cannot reach |
| Control catalog (`controls.py`) | CI-tested | 11 controls: 8 with a code procedure (privileged MFA, access review, leaver revocation, change management, monitoring heartbeat, host baseline scan, backup and restore, reconciliation of unknown outcomes), 3 manual only (incident, secrets, data-subject requests); AICPA ids as a starting hypothesis the owner and an auditor must confirm | no criteria text; no mapping rationale field; no criteria version field |
| Assessment engine (`assess.py`) | CI-tested (46 tests); period-based, see the end of this section | results PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW; PASS needs verified evidence of the right kind, period coverage, a producer-stated complete population, every item meeting the procedure, no pending manual dependency; a producer's own "compliant" claim is never read; NOT_APPLICABLE only from an explicit applicability record | no local probes: every procedure reads evidence a collector produced; no aggregate score by design |
| Ledger (`ledger.py`) | CI-tested | hash-chained assessments, reviews, exceptions; a reviewer cannot be the control owner or an evidence producer; an exception records who accepted the risk, until when and the remediation, expires, never changes the result | |
| Export (`export.py`) | CI-tested | restricted pack (controls, latest assessment, evidence references with digests, exceptions, integrity state; not-an-attestation banner); counts-only benchmark summary | `assert_no_claim` inspects dictionary keys and one literal string; a free-text "compliant" inside a value is not caught |
| Pipeline (`pipeline.py`) | CI-tested (31 tests) | a collector reads one source for one completed window, the pipeline signs, ingests, skips covered windows, records failures; a failed collection yields no evidence, which the assessor reads as INCONCLUSIVE or NOT_TESTED | |
| Collectors: GitHub changes and MFA; heartbeat; Azure PostgreSQL backups; Entra and Azure RBAC; retained host scans | CI-tested against fakes; Experimental against real targets | each is read-only with a restricted transport; written from API documentation and tested against fakes; the Entra lifecycle and the restore-drill log never ran live | no ticketing collector; nothing outside the listed sources |
| Host baseline control `SEC-CFG-01` | CI-tested over retained reports | reads retained node-benchmark and package-scan reports produced by a script that lives in a private repository, re-hashes them, refuses an edited report, treats a failed scan or an empty window as a gap | the product runs no local host benchmark; the only baseline content is output of that external script |
| HTTP routes for the library | none | the library has no routes; it is not deployed in the public demo | |
| Real runs | Experimental | one real scan of a development node (2026-10-24) made the host control end in FAIL, which the library reports as the honest state; no full period of operating evidence exists | |

What this means for R0: the vault, the ledger and the export machinery are reusable as storage, integrity and reporting. The assessor is not reusable as it is for point-in-time probes, and the work needed is specific:

* Coverage. `Assessor.assess` requires evidence to cover at least `min_coverage` (0.95 by default) of the assessed period, so one probe run against a 30-day period covers about none of it and ends INCONCLUSIVE. Probe evidence needs its own procedure that treats a run as instant evidence (an explicit "as of" time, no period coverage), with a stated maximum age.
* Applicability. NOT_APPLICABLE in the assessor is a person's declaration (`declare_not_applicable` with a named person and a reason, per control, scope and tenant), not a probe-level positive observation. A probe-level "does not apply" therefore stays a probe-level record inside the new procedure's outcome; a control-level NOT_APPLICABLE remains a person's declaration.
* Aggregation. The multi-probe rule of section 3.3 has no home today, because a procedure returns one outcome per control. The new procedure takes a set of probe results and applies that rule.
* Payload shape. `host_baseline_scan` reads `critical_open`, `target_type` and `scanned_at`, which a probe result does not have, and `SEC-CFG-01` means "no open critical finding" from a retained scan. The probes therefore get a new control (for example `HST-CFG-01`, "host configuration matches the declared profile") with its own procedure and criteria hypotheses, and `SEC-CFG-01` keeps its meaning.

The remaining missing piece is the one the R0 bar asks for: local, read-only probes on one host, with a deliberately misconfigured fixture that fails, a corrected one that passes, and an absent-evidence case that is inconclusive. The probes, the new procedure, the new report formats and the qa-evidence export are the work.

## 3. Control-probe contract

A probe is a deterministic, read-only procedure that observes one property of one declared subject and records what it saw. It is the unit of control testing for local and agent controls; existing collectors stay as they are, and a probe is a collector with a different source.

### 3.1 Declaration

Every probe is declared as data, versioned, and reviewed like code:

```text
probe_id            stable id, for example HOST-SSH-01
version             integer; any change to what is read or how it is judged bumps it
title               what property is observed, in neutral words
subject_kind        host | container | agent | service
reads               exact files, commands (argv list) and API calls it may use; nothing else is allowed
requires            prerequisites: operating system family, privileges, binaries, files; each checkable read-only
applicability       how "this probe does not apply here" is established from a positive observation
expected            the declared expectation (values, sets, bounds), owned by the operator's profile, not hard-coded in the probe
evidence            what is captured: raw observation bytes (redacted), parsed value, the comparison made
criterion_refs      crosswalk entries (section 8), a hypothesis only
manual_dependency   what a person must still attest, if anything
```

### 3.2 Execution rules

* Read-only: argv lists, no shell, no write outside the probe's own scratch and output directory, no network unless the probe declares a read-only API read and the operator's scope allows it. A probe whose `reads` it exceeds at run time is a defect and the run is failed.
* Deterministic: the same subject state and the same profile give the same result and the same evidence bytes (sorted keys, no clock in the evidence; the run time is recorded by the runner separately).
* Bounded: a wall-clock and output-size limit; a timeout is INCONCLUSIVE.
* Isolated: no probe depends on another's output. A probe that needs a second observation declares it in `reads`.
* Redaction: values that match secret patterns or are declared sensitive are replaced by a digest and length before they enter evidence; the raw bytes are never stored.

### 3.3 Result states

The six states of the existing assessor are kept. Probe-level rules, written to be implemented as procedure code:

| Situation | Result |
|---|---|
| a prerequisite cannot be checked (binary missing, permission denied, file unreadable, parse error, timeout, unexpected output shape) | INCONCLUSIVE, with the reason code; never PASS, never FAIL |
| a prerequisite is checked and not met because a positive observation shows the probe does not apply (for example the package database shows the SSH server is not installed and nothing listens on its port) | NOT_APPLICABLE, with that observation stored as the applicability record. Never inferred from the absence of evidence |
| the observation was parsed and one or more items differ from the expectation | FAIL, listing the items |
| the observation was parsed, every item in the declared population was checked, and all match | PASS for that probe |
| a probe was defined and never run for the subject in the period | NOT_TESTED |
| the control needs a person's attestation | MANUAL_REVIEW unless an unexpired attestation from someone other than the control owner exists |

Absent evidence is INCONCLUSIVE at probe level when the probe ran and recorded that it could not see (a recorded INCONCLUSIVE record exists), and NOT_TESTED when no record exists at all because no probe ran. The R0 bar says "absent evidence INCONCLUSIVE"; the existing assessor says "no evidence is NOT_TESTED". The line between them is whether a record exists. This is proposed decision D3 in [DECISIONS.md](DECISIONS.md); the owner has not confirmed it. Neither is ever PASS.

A control with several probes passes only if every applicable probe passes and none is INCONCLUSIVE; a single FAIL fails the control; the control's reason lists each probe's state. There is no weighting and no score.

### 3.4 Evidence capture

Each probe run produces three records, each with a digest: the raw observation (redacted), the parsed observation, and the comparison with the expectation (including the expectation's own digest, so a changed profile is visible). The runner wraps them in the existing signed evidence envelope with the producer being the local runner, kind `probe_result`. A failed probe run is evidence of a failure; a probe that could not run is recorded as an INCONCLUSIVE record, not skipped.

### 3.5 Fixtures every probe ships with

The R0 bar is the test: for each probe, a deliberately misconfigured fixture produces FAIL, the corrected fixture produces PASS, and a fixture with the evidence absent (file missing, command missing, unreadable, unparsable) produces INCONCLUSIVE; a fixture that proves the probe does not apply produces NOT_APPLICABLE where applicability is defined. Fixtures are directory trees and recorded command outputs fed through a test-only reader, so the probes run in CI without privileges and without touching the CI host. A probe also has a test that fails if it reads anything outside its declared `reads`.

### 3.6 Subject binding

A probe declares its subject (host, container, agent or service) and verifies that the subject it observes is the one declared before it reads anything else. A probe that reads `/proc/net`, `/proc/sys`, the mount table or the filesystem observes the current namespaces: run from the shipped container image, it would see the container, not the host, and could PASS a host it never looked at. The subject check compares the process's network, mount and PID namespaces with those of PID 1 and looks for container control groups; when the observed subject is a container and the declared subject is a host (the default), or the check cannot be made, every probe of that subject returns INCONCLUSIVE with reason `subject-mismatch`, never PASS, and never NOT_APPLICABLE. A probe targets a container only when the profile says so explicitly. The check result is part of the evidence. The package install (not the image) is the supported way to probe a host; the image's `doctor` says so.

## 4. First pack: original Linux baseline, not CIS-certified

### 4.1 Licence position (read 2026-11-01)

| Fact | Source |
|---|---|
| CIS non-member terms: the benchmark PDFs are under CC BY-NC-SA 4.0; the controls under CC BY-NC-ND 4.0; posting a non-member product on a website, FTP server or similar is prohibited; a derivative work directly based on a non-member product or a component of one is prohibited; users may not represent or claim a particular level of compliance or consistency with a non-member CIS product | https://www.cisecurity.org/terms-of-use-for-non-member-cis-products (the page at that address answered for me as summarised here; read through the page reader, so treat the wording as a summary and have counsel read the page) |
| Machine-readable CIS content (SCAP, XCCDF, OVAL) and the right to integrate Benchmark content into a product are tied to CIS SecureSuite membership tiers | secondary: CIS SecureSuite pages and a CIS product-vendor announcement surfaced by search, 2026-11-01; not read as a primary term: UNVERIFIED |
| ComplianceAsCode/content licence BSD-3-Clause; the project reworded one profile so that it does not claim CIS compliance because it could not reconcile CIS terms with a freely available profile | LICENSE file in https://github.com/ComplianceAsCode/content (primary); the rewording from a 2020 pull request seen through search (secondary, dated: UNVERIFIED as current) |
| OpenSCAP licence LGPL-2.1 | https://github.com/OpenSCAP/openscap (GitHub licence metadata) |
| Lynis (an auditing script) GPL-3.0 | https://github.com/CISOfy/lynis (GitHub licence metadata) |

### 4.2 Recommendation

Write Edge Sentinel's own probes for a short list of widely documented hardening properties. The package carries no CIS text, no CIS recommendation numbers and no statement of CIS conformance; the pack is called "original Linux baseline, not CIS-certified", and the disclaimer appears once per report and in the package description, with probe titles using their own neutral names (a label that put the word CIS into every title would invite a reading of consistency with CIS content, which the non-member terms forbid claiming). A crosswalk to CIS Benchmark recommendation numbers would be derivative of CIS content or at least requires reading the terms against that use, so it is not shipped and is blocked by release gate RG6 (owner: counsel; DECISIONS section 5 and D6).

Optional adapter, Planned: if an operator already holds SCAP content they are licensed to use (a CIS member's datastream, or the BSD-3-Clause ComplianceAsCode content for a non-CIS profile), Edge Sentinel can run `oscap` over that operator-supplied content on the operator's host and keep the results as a native artifact (never PASS by itself), mapping nothing to criteria automatically. Edge Sentinel ships no SCAP content. Rejected alternative: consuming CIS-derived content on the operator's behalf, because of the redistribution and no-compliance-claim terms above and because it would put third-party content in the verifier's path. Rejected alternative: bundling ComplianceAsCode profiles as the default, because the content is large, changes often and would make the first pack depend on a moving dataset.

### 4.3 Candidate probes (R0 ships four to six of these eight; the rest are later)

Criterion references are hypotheses for the control owner and the auditor to confirm; none is a statement that a criterion is satisfied. The R0 selection is made in ES-01e1; the first four by value are HOST-SSH-01, HOST-FS-01, HOST-NET-01 and HOST-ACC-01, with HOST-SYS-01 and HOST-DKR-01 as the next two.

| Probe | Reads | Prerequisites | Expectation (profile) | Not applicable when | Criteria hypothesis |
|---|---|---|---|---|---|
| HOST-SSH-01 effective SSH daemon settings | the daemon's effective configuration through its test mode (`sshd -T`); the first-match rule, `Include` files, `Match` blocks and compiled-in defaults apply, and test mode without connection parameters does not evaluate `Match` blocks, so the result says so. When test mode is not permitted the probe may return FAIL (a parsed setting that violates the expectation) or INCONCLUSIVE, never PASS, because an absent directive falls back to a default the static parse does not know | root or a readable config; SSH server present | root login disabled; password authentication per profile; no empty passwords; an allowed-users or allowed-groups restriction per profile | package database shows no SSH server and no listener on its port | CC6.1, CC6.6 |
| HOST-FS-01 permissions of sensitive files | `stat` on a declared list (account database, shadow file, SSH daemon config, the host's own secret files named by the operator) | the files exist | owner and mode within the declared bounds | a listed path is declared optional and absent by a positive observation (the parent directory is readable and the path is not there) | CC6.1 |
| HOST-NET-01 listening services | listening sockets from `/proc/net` (or `ss` with a fixed argv) compared with the declared service list | readable `/proc/net` | no listener outside the declared allow-list; wildcard binds listed | none | CC6.6, CC7.1 |
| HOST-SYS-01 kernel network parameters | read-only reads under `/proc/sys` for a declared set (forwarding, redirects, source routing, reverse-path filter) | readable `/proc/sys` | each value equals the declared value | a parameter does not exist on this kernel (positive: the file is absent while its parent directory is readable) | CC6.6 |
| HOST-ACC-01 local privileged and empty-password accounts | the local account database and, where readable, the shadow file. Accounts supplied by NSS, LDAP or SSSD are not visible to this probe, and the result is labelled "local files only"; a fixed-argv `getent passwd` read may be added where the profile allows it | readable account database; shadow file readable for the empty-password part, otherwise that part is INCONCLUSIVE | exactly the declared accounts have UID 0; no account with an empty password | none | CC6.1, CC6.2 |
| HOST-DKR-01 container-runtime socket exposure | mode, owner and group of the runtime socket; members of its group; mounts of the socket into running containers only if the runtime API is declared reachable read-only (otherwise this part is INCONCLUSIVE) | runtime installed | socket not world-accessible; group membership within the declared list; no container mounting it unless declared | package database and filesystem show no runtime | CC6.1, CC6.6 |
| HOST-LOG-01 persistent logging (configuration probe) | journald configuration and the audit daemon's configuration; it judges configuration, not state (for example, journald persists only if its directory exists), and the result text says "configuration" | readable config | persistence per profile | none | CC7.2 |
| HOST-UPD-01 update configuration (configuration probe) | the package manager's automatic update configuration and the time since the last successful update recorded in its own log; "enabled" is configuration, and the last-success time is the log's statement, not a verified state | readable config and log | automatic security updates enabled per profile; last success within the declared bound | none | CC7.1 |

Subject: HOST-NET-01, HOST-SYS-01, HOST-FS-01 and HOST-DKR-01 are subject-sensitive (section 3.6) and refuse to run as PASS from a container. Probe titles and report headings use these original names without the word CIS; the disclaimer "original baseline, not CIS-certified" appears once per report and in the package description, not in every probe title.

Platform: Linux only for the first pack. macOS and Windows hosts return NOT_APPLICABLE for every probe only if a positive operating-system observation says so; otherwise INCONCLUSIVE. Unsupported platforms are not silently skipped.

The existing host-baseline control (`SEC-CFG-01`) keeps reading retained reports and keeps its meaning; the probes feed a new control with its own procedure (section 2). A container-image scan still does not satisfy `SEC-CFG-01`.

## 5. Agent-specific control tests (interface only)

The controls these probes would test belong to the identity workstream (agent identity, delegation and policy binding, credential brokering, revocation and the kill switch, evidence receipts). This design does not define them, change them or assume a date. It defines the probe interface and the states a probe returns while the control is absent.

Interface (Planned):

* An `ObservationSource` is a narrow, read-only adapter that returns recorded observations about an agent runtime: its declared tool list, the policy decisions it recorded, the approval records, the egress records, the credential-handling records, the receipts. The identity workstream implements sources for its own components. Edge Sentinel defines only the method signatures and the record shapes it needs, as its own data; it reads no secret and starts no agent.
* An agent probe is a normal probe (section 3) whose `reads` is "calls to an ObservationSource" plus the operator's declared expectation. It never executes a tool, never sends a prompt, never approves anything.
* While no source exists, every agent probe returns NOT_TESTED, and a source that fails to answer yields INCONCLUSIVE. There is no degraded PASS.

| Control area | What the probe asks (expectation declared by the operator) | Depends on | Seeds that exist today | Status |
|---|---|---|---|---|
| Permissions | each declared agent holds only the scopes in its declared grant; no scope outside it was used in the recorded decisions | agent identity and delegated grants (identity workstream) | route-level policy registry and its CI test in the app; row-scope and column-mask decision demo | Planned (seeds CI-tested) |
| Tool allowlists | the set of tools an agent could call equals its declared allowlist; no call outside it in the records | tool inventory and policy binding | chat console with an empty side-effect allow-list (Implemented in the code; no test in the public tree exercises it); capability manifest of the response operator (Implemented) | Planned |
| Approval gates | every side-effecting action in the records has an approval bound to its operation digest; none executed on a forged or reused approval | operation-digest binding and approval records | block-action approval state machine (CI-tested for bans in the private deployment copy's own CI; the forged-approval tests for the operator `/invoke` path are not run by any CI; the public tree has `test_operator.py` for a forged approval on the open HTTP path) | Planned for the general case |
| Egress restrictions | recorded outbound destinations are within the declared allow-list | an egress record source or a proxy log source | one test that the app's LLM blurb path makes no direct HTTP call (CI-tested, public tree); the deployed copy calls its local model directly | Planned |
| Credential handling | no credential value appears in prompts, logs or evidence; leases recorded as short-lived and scoped; none reused after expiry or revocation | credential broker and revocation (identity workstream) | redaction of credentials from the collection pipeline's run records and error text (CI-tested); that is a thin seed for "no credential value in evidence", not the check itself | Planned |
| Receipt provenance | each receipt verifies against its recorded operation, identity and evidence references | independent receipts (identity workstream) and the qa-evidence format | case receipts with content-addressed ids and a read-only verification step (CI-tested library) | Planned |
| Replay | a recorded case re-ingested into a fresh store reproduces the same content-addressed id | none beyond the case spine | `_replay_proof` over a case's own raw evidence (CI-tested library) | CI-tested for cases; Planned for agent runs |

Dependencies are one-way: Edge Sentinel reads the identity workstream's records through the interface; nothing in the identity work depends on Edge Sentinel. If the identity work changes a record shape, only the corresponding source adapter changes.

## 6. Evidence, assessment and export

Flow: probes and collectors produce signed evidence envelopes; the vault stores them; the assessor evaluates one control, one scope, one period; the ledger records the assessment and any review or exception; the export builds the reports in section 7.

qa-evidence export. Edge Sentinel is an additive consumer of the merged qa-evidence bundle format (`packages/qa-evidence` in the redevops-qa repository, commit 2ad97f3 read 2026-11-01); it adds nothing to the format and changes no shared contract (`runtime-contracts` qa/v1, `agent-harness`, the QA product's evidence schema). The mapping details, the role rules and the interface points are in section 13 of [SBOM_CVE_DESIGN.md](SBOM_CVE_DESIGN.md) and apply here too. For control assessments:

| Edge Sentinel result | Lane native status (the assessor's own report) | Note |
|---|---|---|
| PASS | `NATIVE_PASS` | never becomes `VERIFIED` by itself |
| FAIL | `NATIVE_FAIL` | |
| INCONCLUSIVE | `NATIVE_ERROR` (raw status `INCONCLUSIVE`) | the lane did not run to completion, so the run outcome is capped at `INCONCLUSIVE` by the format |
| NOT_TESTED | `NATIVE_SKIPPED` | same cap |
| NOT_APPLICABLE | not a lane: recorded as an applicability record in the bundle and left out of the per-probe assertions | a lane that did not complete caps the run, so an inapplicable probe must not be a lane; the record keeps the positive observation that justifies it. A `profile` lane carries the assertion "every probe in the profile has exactly one of: a result lane, or an applicability record with a positive observation", so a producer cannot make an inconvenient probe disappear by calling it inapplicable |
| MANUAL_REVIEW | `NATIVE_SKIPPED` | |

The independent check program (a separate program from the probes) compares the recorded observations and the recorded comparison with a frozen expectation set (the profile) and writes the verdict: `VERIFIED` when the recorded evidence supports the stated result under the frozen assertion, `FAILED` when it contradicts it, `INCONCLUSIVE` when the evidence is missing or unusable. The assessor's six-state vocabulary stays the product's own; it is mapped on export and never emitted in the verdict field, and the verdict vocabulary is never emitted by the assessor (DECISIONS Q9). A verified verdict means: the frozen assertions were evaluated over the recorded observations. It does not say the host is secure or any criterion is met, and the format's own limitations (unkeyed hashes, producer-declared assertion set, role labels, no independence of the verifier beyond a name and version) are printed in every export.

The lane model constrains the design in one more way: the format requires at least one frozen assertion per lane and a completed lane for a `VERIFIED` run, so a run in which any applicable probe was INCONCLUSIVE can only be `INCONCLUSIVE` at run level. That is the intended behaviour and the report says it in those words.

## 7. Report structure

One report per subject, scope and run, rendered from the same records in four forms.

1. Executive posture summary, one page. Contents: subject and scope, run time and tool versions, the criteria version, counts by result (PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW) per control, the list of FAIL and INCONCLUSIVE items, evidence freshness, and the fixed banner: readiness evidence for a reviewer, not an attestation, not an auditor's opinion, point in time. No score, no traffic-light roll-up, no "compliant".
2. Control table. Per control: id, title, owner role, criteria hypotheses (section 7.1), result with reason, probes and their states, evidence references (id and digest, never the bytes), exceptions with owner and expiry, review state.
3. Remediation. For each FAIL or INCONCLUSIVE probe: what was observed, what was expected, the smallest correcting change described as text, and what evidence would turn it. Remediation text is guidance. It is never executed by this layer (the response workflow is a separate, approval-gated path described in the SBOM and vulnerability document).
4. Raw evidence export. A qa-evidence bundle (section 6) plus the restricted pack from the existing export (controls, evidence references, integrity state). The bundle is the verifiable artifact; the summary is derived from it.
5. Machine formats. JSON: the control table and the crosswalk, with a schema version. SARIF 2.1.0: one rule per probe; a result per FAIL and INCONCLUSIVE probe at level `error` and `warning`; PASS and NOT_APPLICABLE results use the SARIF result `kind` values for pass and notApplicable (from memory of the SARIF 2.1.0 model; the OASIS specification was not retrieved: UNVERIFIED), omitted by default; locations name the probed file or setting. SARIF output carries the same banner in its run properties.
6. Crosswalk (7.1).
7. Missing-evidence report (7.2).

### 7.1 Crosswalk

One row per probe-to-criterion mapping, as data the control owner edits:

```text
control_id, control_version, probe_id, probe_version
framework          "AICPA Trust Services Criteria"
framework_version  "2017 with revised points of focus 2022"
criterion_id       for example CC6.6
relationship       supports | partially supports | informs     (never "satisfies")
rationale          one or two sentences: why the observed property bears on the criterion, and what it leaves out
confirmed_by       the control owner or auditor who confirmed the mapping, with date; empty means hypothesis
```

An unconfirmed mapping prints "hypothesis, not confirmed". The criteria text is not reproduced.

### 7.2 Missing-evidence report

A table that is the honest counterpart of the summary: for every control in the catalog and every probe in the profile, the evidence that was expected and was not present, the reason (no collector exists, collector failed, window not covered, population not stated complete, prerequisite not met, manual attestation absent), and the result that follows. It also lists the criteria the catalog has no control for, so a reader does not mistake the catalog for a full SOC 2 scope. This report is generated even when everything passed; an empty report is a stated fact ("no missing evidence under this profile"), not an omitted section.

## 8. Wording rules

The product may say, once the corresponding capability exists and its tests pass:

* "technical control evidence for selected Trust Services Criteria", "readiness evidence for a reviewer", "a read-only probe found these settings", "this control test failed", "evidence for this check was missing, so the result is inconclusive";
* "mapped to criterion X as a hypothesis for your auditor to confirm";
* "evidence is digest-checked and exportable in a verifiable bundle";
* "supports your SOC 2 preparation".

The product may not say: "SOC 2 compliant", "SOC 2 certified", "audit-ready" (with or without a qualifier, because the qualifier is lost in copy), "passes SOC 2", "satisfies" a criterion, "audited", any percentage or score of compliance, "continuous compliance", "CIS compliant", "CIS certified" or "CIS benchmark" results for the own-checks pack (its name always carries "not CIS-certified"), "no vulnerabilities", and "confirmed exploit" for any scanner result. The demo's nav blurb that said "control-check" was flagged by ES-00 as unsupported because no control check is deployed; the owner reports the site wording was changed in separate site pull requests (not re-verified here).

Enforcement: extend the existing `assert_no_claim` guard to scan string values as well as keys, with a forbidden-phrase list covering the lines above, and run it over every export, report and public page text in CI (ES-01 PR list). Scope of the scan: generated prose (report text, summaries, remediation text, public copy), not recorded observations (raw settings, hostnames and configuration text may legitimately contain any word). The fixed banner, which says that no criterion is stated to be satisfied, is exempt by exact match, and so are quoted negations inside it. A hit in generated prose fails the export; the guard never masks text. A phrase list is a backstop; it does not replace review.

## 9. What is built, in order

The sequence is in [DECISIONS.md](DECISIONS.md) (ES-01e control probes, ES-01f qa-evidence export). Summary of the first slice: four to six probes from section 4.3 through a new instant-evidence procedure alongside the existing assessor (section 2), each with fail, pass and absent-evidence fixtures; the value-scanning wording guard; JSON and SARIF output; the missing-evidence report; and the bundle export verified offline in CI. The agent probes of section 5 are not in the first slice.

## 10. Risks specific to this layer

* A probe's judgement encodes a security opinion; the profile (the expectation) is the operator's and is part of the evidence, so a pass against a weak profile is visible.
* The existing vault is not WORM and its keys live with the deployment; a single-host R0 inherits that. The qa-evidence anchor and the vault head must be anchored outside the host to mean anything against a deliberate rewrite.
* Mapping to criteria is interpretive. Confirmation by a control owner or an auditor is recorded; unconfirmed rows say so.
* Own checks drift from common practice over time. Each probe has a version and a stated source of reasoning in its declaration; a review date is part of the profile.

## 11. Interface points

| With | Interface | Direction |
|---|---|---|
| qa-evidence | pinned bundle format, `verify` CLI, optional build API | Edge Sentinel consumes |
| Identity workstream | `ObservationSource` adapters for agent probes; absent source gives NOT_TESTED | Edge Sentinel consumes |
| Operator's SCAP content (optional) | `oscap` run by the operator's installation over operator-supplied content; native artifact only | optional adapter |
| Auditor or reviewer | the restricted pack and the bundle, read-only | out |

## 12. External facts register

All retrieved 2026-11-01.

| Fact | Source | Read how |
|---|---|---|
| CIS non-member terms (licences, posting, derivative works, compliance claims) | https://www.cisecurity.org/terms-of-use-for-non-member-cis-products | page reader summary; counsel to read the page |
| CIS SecureSuite membership gates tailoring and machine-readable content; product-vendor membership integrates Benchmark content | https://www.cisecurity.org/cis-benchmarks (tailoring needs SecureSuite membership: primary); SCAP gating and product-vendor tier from search extracts of CIS pages | partly primary, partly UNVERIFIED |
| ComplianceAsCode content BSD-3-Clause | https://github.com/ComplianceAsCode/content (LICENSE) | primary |
| ComplianceAsCode reworded a profile to avoid a CIS compliance claim (2020) | pull request seen through search | secondary, dated: UNVERIFIED as current |
| OpenSCAP LGPL-2.1; Lynis GPL-3.0 | GitHub licence metadata | primary |
| AICPA: 2017 TSC with revised points of focus 2022, criteria unchanged by the revision | search extracts of AICPA and firm pages; https://www.aicpa-cima.com/resources/article/2017-trust-services-criteria-with-revised-points-of-focus-2022 not read directly | secondary: UNVERIFIED at the primary page |
| AICPA copyright and reuse terms for the criteria text; that only CPA firms issue SOC 2 reports | not retrieved | UNVERIFIED |
| SARIF 2.1.0 result `kind` values | OASIS specification not retrieved | UNVERIFIED |
| qa-evidence format, roles, vocabularies, limitations | `packages/qa-evidence` README, `constants.py` and schemas in the redevops-qa repository at commit 2ad97f3 | primary |
