# Edge Sentinel: SBOM and vulnerability-intelligence design

Status: DESIGN. Nothing in this document is an implemented capability unless a row below says Implemented or CI-tested and names the repository evidence. The command line, the data model and the file layouts are design targets.

Companion documents: [COMPLIANCE_READINESS_DESIGN.md](COMPLIANCE_READINESS_DESIGN.md) (control probes and SOC 2 technical readiness) and [DECISIONS.md](DECISIONS.md) (open questions, recommended defaults, the ES-01 PR breakdown, risk register).

## 0. Reading rules

Status labels, used on every capability named here:

| Label | Meaning |
|---|---|
| Implemented | The code exists in the repository and runs, but no test in this repository exercises it end to end |
| CI-tested | A test that runs in the repository's CI exercises it |
| Experimental | Exists as a library, a sample or an offline simulation; not wired into the product or not run against a real target |
| Planned | Described here only |

External facts (licences, API terms, rate limits) cite a URL and the date they were retrieved (2026-11-01). A fact I could not read from a primary page is marked UNVERIFIED, with the secondary source if one exists. Section 17 collects them.

Two wording rules apply throughout and to anything derived from this design:

* A scanner or matcher result is "an advisory database lists this installed version as affected". It is never described as confirmed exploitation, and never as "exploitable" unless a separate, named test demonstrated it.
* A technical control test is never described as a completed SOC 2 audit (see the compliance document).

## 1. Purpose, scope and non-goals

Purpose: let one operator, on one declared host or one declared artifact set, answer "which components do I run, which published advisories list those versions as affected, how fresh is the data behind that answer, and what evidence supports it", and keep the answer reproducible and verifiable offline.

In scope for R0 (the standalone, CLI-first release):

* Inventory: produce or ingest an SBOM for a declared target (a directory, a root filesystem path, a container image already present locally, or a supplied SBOM file).
* Scan: run one or more pinned scanners against the SBOM or target, read-only, with scanner output kept as a native artifact.
* Assess: evaluate the findings with Edge Sentinel's own matcher over a frozen advisory snapshot, with a state per finding, VEX statements, suppressions with expiry and owner, and caveats.
* Verify: an independent check program compares the recorded findings with a frozen expectation set and writes a verdict.
* Report: human and machine output, plus an evidence export that an unmodified qa-evidence verifier accepts.

Non-goals:

* No scanning of systems the operator has not declared and consented to. No third-party scanning.
* No automatic remediation in R0. No package upgrade, configuration change, firewall change or process kill, not even behind a flag.
* No active exploitation, no proof-of-concept execution, no DAST in R0. The existing `deploy-scan` proxy (nmap, nuclei, ZAP) is outside R0 (see DECISIONS Q12).
* No claim of independent confirmation of exploitation, and no CVSS-derived "risk score" that is presented as a measured probability.
* No general vulnerability database or mirror. The product reads feeds on the operator's machine for the operator's inventory.
* No change to the shared contracts. Evidence export is an additive consumer of the merged qa-evidence format (section 13); `runtime-contracts` qa/v1, `agent-harness` and the QA product's evidence schema are not modified or extended by this design.
* No SOC 2 or other attestation. Vulnerability data is one input to the readiness evidence described in the compliance document.

## 2. What exists today

From the read-only audit ES-00 (2026-11-01), with the labels above. "Private deployment copy" is the vendored copy that the public demo builds; it is not published.

| Item | Status | Where and how it is limited |
|---|---|---|
| Trivy shell-outs (`trivy rootfs`, `fs`, `image`) wrapped as `SupplyChainScanner`; Syft SBOM with a fallback to Trivy CycloneDX | Implemented | `context-runtime` `integrations/supply_chain.py`, imported by the app. Output is parsed into `Finding{id, pkg, installed, fixed, severity, title, target, cls}`. Not tested here against real images; the default demo image has no container runtime, so image scans are unavailable by default |
| `VulnDB` reader over an Apache Doris table with row scope and column mask | Implemented, partly CI-tested | `context-runtime` `integrations/vuln_db.py`; permissions behaviour is tested in the app's tests; the Doris schema and the ingest were never run against a real Doris in CI |
| Daily NVD and OSV ingest into one table | Implemented in a private deployment copy; not CI-tested against Doris | keyword and fixed-package lists for the owner's own stack, not a general mirror (section 9.3) |
| Freshness stamp with a 36 hour check | Implemented and CI-tested in the private deployment copy | writes a log line and a syslog warning; no page, no alert, and the application payloads carry no data age |
| Status vocabulary, VEX, suppression, false-positive workflow | none | `grep` for `vex`, `not_affected`, `cpe`, `kev`, `epss` over all audited trees finds nothing |
| Grype, OSV-Scanner, CISA KEV, EPSS | none | Grype and KEV and EPSS are not used anywhere |
| qa-evidence export | none | the app has its own evidence vault and ledger (compliance document) |

Consequences that drive this design: matching is delegated to Trivy's own database today; the stored NVD rows cannot be matched to a package; there is no data age in any payload; and a failed OSV package query is swallowed (`vuln_feeds.fetch_osv` returns an empty list on any exception), which is the kind of silent empty result section 11 forbids.

## 3. R0 CLI surface (design targets only)

None of these commands exists. They are named so the data model, exit codes and evidence layout can be reviewed before code.

```text
edge-sentinel doctor     environment and prerequisite check; read-only; also reports feed and scanner freshness
edge-sentinel inventory  build or ingest an SBOM for a declared target; write sbom/ and an asset record
edge-sentinel scan       run pinned scanners over the SBOM or target; keep raw output as native artifacts
edge-sentinel assess     evaluate scanner output and the matcher over a feed snapshot; apply VEX and suppressions
edge-sentinel verify     run the independent check program over recorded findings; write the verdict records
edge-sentinel report     render findings, caveats, feed freshness and the evidence export (human, JSON, SARIF)
```

Global design rules:

* Read-only by default. No command writes outside its declared output directory and the tool's own state directory. No command modifies the target.
* A target must be declared (`--target` or a scope file) and the operator must acknowledge scope and consent once per scope file (a recorded acknowledgement, not a prompt that can be bypassed by a pipe). A target that resolves outside the declared allow-list is refused (section 12).
* Two feed modes: `--feeds-dir DIR` (offline: read only a frozen snapshot, open no network connection) and `--refresh-feeds DIR` (online: download into a new snapshot directory, never into the one an earlier run used). Mixing them in one invocation is refused. `doctor` and `report` print the snapshot age separately from the evaluation result.
* Exit codes: 0 completed with the stated coverage; 1 findings above the operator's gate; 2 usage or scope error; 3 a feed, scanner or database was unavailable, stale beyond the configured limit, or implausibly empty (never 0); 4 internal error. A crash is never reported as a clean run.
* Every run writes a `run.json` naming tool versions, scanner and database versions, snapshot digest, target digest, and the exit code.

## 4. Pipeline

```text
declared target
   |  inventory (Syft or supplied SBOM)         read-only
   v
sbom/<asset>.cdx.json           (CycloneDX; SPDX optional)      -> digest
   |  scan (Trivy, Grype, OSV-Scanner: each isolated)
   v
native/<scanner>/output.json    (scanner's own words; native artifact)
   |  assess (Edge Sentinel matcher + frozen feed snapshot + VEX + suppressions)
   v
records/findings.json           (normalised findings, state, caveats, evidence digests)
   |  verify (independent check program + frozen expectation set)
   v
records/verdicts.json -> qa-evidence bundle (optional adapter) -> report
```

The scanners produce native artifacts. The matcher is Edge Sentinel's own code, so a state per finding does not depend on any one scanner's database. The check program is a separate program with its own parser; section 13 states exactly how far that independence goes.

## 5. SBOM formats and generators

### 5.1 Formats

| Format | Role in this design | Licence of the specification (retrieved 2026-11-01) |
|---|---|---|
| CycloneDX (JSON), current schema 1.7 in the specification repository (latest release 1.7.2, 2026-09-17) | Primary SBOM and VEX format. Chosen because one document type carries components, purls, hashes, and `vulnerabilities[].analysis` VEX data | Apache-2.0: https://github.com/CycloneDX/specification (GitHub licence metadata) |
| SPDX (JSON), specification 3.0.1 (2024-12-17) | Secondary: ingest and export where a consumer requires it | Community Specification License 1.0, with pre-existing portions under CC-BY-3.0: https://github.com/spdx/spdx-spec/blob/main/LICENSE |
| OpenVEX | Ingest and export of standalone VEX statements | CC0-1.0 (GitHub licence metadata): https://github.com/openvex/spec |
| SARIF 2.1.0 | Findings export for code-scanning consumers | OASIS TC repository; terms are in https://github.com/oasis-tcs/sarif-spec/blob/master/LICENSE.md (read as a pointer only: UNVERIFIED which terms apply to reuse of the schema) |

CycloneDX VEX states, read from the checked-in 1.6 schema (`impactAnalysisState`): `resolved`, `resolved_with_pedigree`, `exploitable`, `in_triage`, `false_positive`, `not_affected`. Justifications (`impactAnalysisJustification`): `code_not_present`, `code_not_reachable`, `requires_configuration`, `requires_dependency`, `requires_environment`, `protected_by_compiler`, `protected_at_runtime`, `protected_at_perimeter`, `protected_by_mitigating_control`. OpenVEX statuses: `not_affected`, `affected`, `fixed`, `under_investigation`; a `not_affected` statement must carry a justification or an impact statement, with justifications `component_not_present`, `vulnerable_code_not_present`, `vulnerable_code_not_in_execute_path`, `vulnerable_code_cannot_be_controlled_by_adversary`, `inline_mitigations_already_exist` (https://github.com/openvex/spec/blob/main/OPENVEX-SPEC.md, retrieved 2026-11-01).

The ECMA standardisation of CycloneDX and a 1.7 schema date are not read from a primary page here: UNVERIFIED beyond the repository metadata above.

### 5.2 Generators

| Tool | Version checked | Licence | Use |
|---|---|---|---|
| Syft | v1.55.0 (2026-10-19) | Apache-2.0: https://github.com/anchore/syft | Default generator: directory, filesystem, image archive; CycloneDX and SPDX output |
| Trivy (SBOM mode) | v0.76.1 (2026-10-21) | Apache-2.0: https://github.com/aquasecurity/trivy | Fallback generator, already the existing fallback in `supply_chain.py` |
| cdxgen | v13.6.0 (2026-10-31) | Apache-2.0: https://github.com/cdxgen/cdxgen | Optional, for language projects where lockfile resolution is the question; not R0 |
| cyclonedx-python | v7.6.0 (2026-10-28) | Apache-2.0: https://github.com/CycloneDX/cyclonedx-python | Optional, Python environments; not R0 |

Licence and release facts: GitHub repository licence metadata and the latest-release API, retrieved 2026-11-01. A licence file in a repository states the licence of the code. It does not settle the terms of data the tool downloads (section 7).

Design rules for generators:

* Pin by version and by digest or checksum. The audited image installs scanners with unpinned `curl | sh`; that pattern is not carried into the new package (DECISIONS D4).
* An SBOM is stored with its SHA-256 and the generator name and version. A generator that exits 0 with zero components is an error (exit 3 "implausibly empty"), unless the operator declared an empty target.
* An SBOM states what the generator could see, not what is installed. The report carries the generator's catalogers or ecosystems as coverage, and any ecosystem for which no generator ran is reported as unassessed (section 8).

## 6. Scanners and their terms

| Scanner | Version checked | Licence (code) | Notes |
|---|---|---|---|
| Trivy | v0.76.1 | Apache-2.0 | Vulnerability database is an OCI artifact in public registries; databases can be self-hosted (https://trivy.dev/latest/docs/advanced/air-gap/, retrieved 2026-11-01). Flags: `--skip-db-update`, `--download-db-only`, `--db-repository` with ordered fallbacks (https://trivy.dev/latest/docs/configuration/db/). Misconfiguration checks are compiled into the binary. Already used by the existing integration |
| Grype | v0.121.0 (2026-10-19) | Apache-2.0: https://github.com/anchore/grype | DB commands in the source tree: `db update`, `db status`, `db check`, `db import`; configuration keys `auto-update`, `validate-age`, `max-allowed-built-age`, `validate-by-hash-on-start` (`cmd/grype/cli/options/database.go`, read 2026-11-01). Supports OpenVEX filtering (README). Database builder `grype-db` and `vunnel`: Apache-2.0 |
| OSV-Scanner | v2.7.0 (2026-10-12) | Apache-2.0: https://github.com/google/osv-scanner | `--offline`, `--offline-vulnerabilities`, `--download-offline-databases`; local database at `{dir}/osv-scanner/{ecosystem}/all.zip`, directory from the variable `OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY` (https://google.github.io/osv-scanner/usage/offline-mode/, retrieved 2026-11-01). Commit-level scanning is not supported |

Terms of the data each scanner downloads are separate from its code licence:

* Trivy and Grype databases are aggregations of vendor, distribution and ecosystem advisories. The licence of the aggregate is not stated by the code repositories. UNVERIFIED for redistribution. Design consequence: Edge Sentinel never redistributes a scanner database. The operator's machine downloads it, and the evidence bundle records its identity and digest, not its bytes.
* OSV data is licensed per source (section 7).

Scanner role and trust: a scanner's output is Edge Sentinel's native artifact. Edge Sentinel records it unchanged, never edits it, and never converts a scanner's "no vulnerabilities" into a pass. Two scanners disagreeing is a recorded caveat, not a vote.

Scanner supply chain is itself a risk: binaries are fetched by version and checked against a checksum list that ships with the release; unsigned or unchecked downloads are refused. Which signing artefacts each project publishes is UNVERIFIED here and is a precondition for ES-01c.

## 7. Vulnerability feeds

All terms retrieved 2026-11-01. "Primary" means read from the cited page or repository; "secondary" means read from an extract or a search result for that page.

| Feed | Access | Rate limit and terms | Licence of the data | Freshness fields | Reading |
|---|---|---|---|---|---|
| NVD CVE API 2.0 | `https://services.nvd.nist.gov/rest/json/cves/2.0` (as used in the ingest script) | 5 requests per rolling 30 s without an API key, 50 with one. Terms ask services to display "This product uses the NVD API but is not endorsed or certified by the NVD", forbid implying NVD endorsement, and forbid attributing modified content to the NVD; keys must not be shared (https://nvd.nist.gov/developers/start-here and https://nvd.nist.gov/developers/terms-of-use; the terms text read at https://open.windriver.com/info/uni-license-list/licenses/nist-nvd-api-tou.html). Secondary: the NVD pages did not render through the page reader | Data licence not stated on any page I could read: UNVERIFIED. Do not describe NVD data as public domain or freely redistributable in product copy until counsel confirms | per-CVE `lastModified`; per-CVE `vulnStatus` (store verbatim); response `timestamp`. The `lastModStartDate`/`lastModEndDate` window is capped at 120 days in the existing ingest code; the NVD documentation of that cap was not read: UNVERIFIED | Secondary source for enrichment and for CPE-based hints. NIST has changed which records it enriches (a "Deferred" status for pre-2018 records in 2025, replaced in 2026 by "Not scheduled" according to secondary reports: UNVERIFIED against NIST): a record without a CPE configuration is not evidence of "not vulnerable" |
| OSV.dev | API `/v1/query`, `/v1/querybatch`, `/v1/vulns/{id}`; bulk dumps `gs://osv-vulnerabilities/<ECOSYSTEM>/all.zip` and `modified_id.csv` | "Currently there are no limits on the API"; 32 MiB response cap over HTTP/1.1 (https://google.github.io/osv.dev/api/) | Per source: CC-BY 4.0 for most (including GitHub Advisory Database, PyPI, Go); CC0 for some (Rust, GSD, Haskell, opam); MIT, BSD, Apache-2.0 for others; CC-BY-SA 4.0 for Ubuntu and Alpine converted data; CC-BY-NC-ND 4.0 for Chainguard/Wolfi; none stated for Debian and NVD-converted data (https://google.github.io/osv.dev/data/) | per-record `modified`, `published`, `withdrawn`; `modified_id.csv` is newest first so a client can stop at the last timestamp it already holds; withdrawn records are in `all.zip` | Primary source for ecosystem advisories (purl and version ranges). Bulk dump, not per-package queries, is the snapshot path |
| CISA KEV | `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` (and CSV) | none stated on the catalog page | CC0 1.0 (https://github.com/cisagov/kev-data, README and licence file); the licence text states that third-party links in the data keep their own terms and that use does not imply CISA or DHS endorsement | read directly from the live feed: `catalogVersion` (2026.10.30), `dateReleased`, `count`; per entry `cveID`, `dateAdded`, `dueDate`, `knownRansomwareCampaignUse`, `forensicTriage`, `cwes`, `notes` | Context only: "listed in KEV" raises priority. It is not detection and not proof that a given installation is exploited |
| FIRST EPSS | daily CSV `https://epss.empiricalsecurity.com/epss_scores-current.csv.gz` (redirects to a dated file) and API `https://api.first.org/data/v1/epss?cve=...` | the FIRST page says the data is "freely and openly accessible" and published every day (https://www.first.org/epss/); no rate limit stated | Reuse, attribution and redistribution terms not stated on the pages I could read; the site footer reads "All Rights Reserved" and links a copyright page I could not retrieve: UNVERIFIED. Treat as: fetch on the operator's machine, never bundle, cite FIRST as the source | CSV first line `#model_version:vYYYY.MM.DD,score_date:<ISO time>`; columns `cve,epss,percentile`; API fields `cve`, `epss`, `percentile`, `date` (both read live) | Context only: a probability estimate with a model version, not a measurement for this host. FIRST warns that a percentile recalculated over a subset must be labelled as such (https://www.first.org/epss/articles/prob_percentile_bins), so the product shows the published percentile only |
| Scanner databases (Trivy, Grype) | see section 6 | per the tool | UNVERIFIED for the aggregate | Trivy DB `metadata.json` (field names not read: UNVERIFIED); `grype db status` reports build time | Native artifacts of the scan step; their identity (version, build time, digest) is recorded |
| Distribution security trackers (Debian, Ubuntu, Red Hat and similar) | not selected for R0 | not read | not read | not read | Planned. For OS packages the distribution's own advisory data is the right authority (section 8.3); R0 relies on the scanner databases for it and states that as a caveat |

Snyk is not a feed: the existing fetcher is never enabled and its API data terms prohibit redistribution (ES-00; not re-read here). It is dropped from the design.

### 7.1 Freezing a feed snapshot for offline evaluation

A snapshot is a directory, written once, never edited, and addressed by the digest of its manifest.

```text
feeds/<snapshot-id>/
  snapshot.json            id, created_at (producer-supplied), tool version, per-feed entries, manifest digest rules
  osv/<ECOSYSTEM>.zip      the dump file as downloaded (selected ecosystems only)
  kev/known_exploited_vulnerabilities.json
  epss/epss_scores-<date>.csv.gz
  nvd/<window>.json        only if NVD enrichment was requested; raw pages concatenated by request order
  scanner-db/<scanner>/    version file and digest only; the bytes stay in the scanner's own cache
  sources.txt              URL, HTTP status, retrieved_at, response digest per request
```

`snapshot.json` per feed records: the source URL; `retrieved_at`; the source's own freshness field (KEV `catalogVersion` and `dateReleased`; EPSS `model_version` and `score_date`; OSV newest `modified`; NVD newest `lastModified` and the request window); record count; SHA-256 of each stored file; the licence label as Edge Sentinel understood it at retrieval; and the attribution text the terms require. Retrieval details for a feed that was requested and failed are stored too, with the failure, so the snapshot says which feeds it does not contain.

Reproducibility contract: given the same SBOM digest, the same snapshot digest, the same VEX and suppression files and the same Edge Sentinel version, `assess` produces byte-identical `findings.json` (sorted keys, no clock, no host data). Online freshness is a separate record: `freshness.json` holds, per feed, the snapshot age at evaluation time measured against a clock the operator can see. A stale snapshot never changes the findings; it adds a caveat and, past the configured limit, makes `assess` and `report` exit 3 unless the operator passes an explicit "evaluate this frozen snapshot" flag that is itself recorded in `run.json`.

Public fixtures use synthetic advisories authored for the tests (section 10). A snapshot of real feed data is never committed to the repository or published, because of the per-source terms above.

## 8. Evaluation model

### 8.1 States

| State | Meaning | Requirement |
|---|---|---|
| detected | A version-evaluated match: the installed version of a component identified by purl lies in an affected range of an advisory from a source with ecosystem-native version semantics | component identity from the SBOM; advisory with a range or an explicit affected version; comparison with the ecosystem's own version ordering (semver, PEP 440, Debian, RPM, apk, Maven and so on); no VEX statement overriding it |
| potentially affected | A weaker or unresolved match that a person must review | CPE-only match; version missing, unparsable or from a different scheme; advisory lists the package as affected with no fixed version; distribution backport possible and no distribution data; scanners disagree; the advisory is modified after the snapshot's matching rule was last validated |
| not affected | The operator or a supplier states the product is not affected, supported by a VEX statement | a CycloneDX `not_affected` (with a justification) or an OpenVEX `not_affected` (with a justification or an impact statement), identifying the author, scoped to the product and component version, unexpired, and not contradicted by a newer statement. A suppression or a version outside the range does not produce this state |
| unassessed | No judgement was possible | no matcher or no data for the component's ecosystem; component without a purl or version; feed missing from the snapshot; VEX `in_triage` or OpenVEX `under_investigation` |

"Installed version outside the affected range" (a patched or never-affected version) is a recorded evaluation trace entry (`evaluated: outside_range`, with the advisory id, range and installed version), not a finding and not "not affected". It lets a test prove the patched fixture was evaluated and not skipped. A component with no advisory in the snapshot is reported as covered with no match for that snapshot id, which is a statement about the snapshot, not "clean".

Each finding carries `state`, `advisory_id`, `aliases`, `component_purl`, `installed_version`, `affected_range`, `fixed_versions`, `source_feed` and its freshness, `matched_by` (`purl-range`, `scanner-only`, `cpe-hint`), `scanners_reporting`, `context` (KEV listing, EPSS value and model version, both optional and labelled as context), `caveats[]`, and evidence digests.

### 8.2 Matching rules

* Match on package URL and the ecosystem's own version comparison. A matcher that cannot parse a version returns potentially affected with the reason, never detected and never "no match".
* Components from the operating system package manager are matched against distribution data when it is in the snapshot; otherwise the matcher reports the scanner's verdict with the caveat "no distribution advisory data in this snapshot" because a distribution fix can carry the same upstream version string.
* The affected range is the whole `affected[]` array of an OSV record (all packages, all ranges and events). The existing ingest keeps only the first entry and its first range (`vuln_feeds._osv_to_record`); that behaviour is replaced.
* Aliases (CVE, GHSA, distribution ids) are merged into one advisory identity with a stable preference order so a finding is counted once.

### 8.3 CPE pitfalls

CPE strings from NVD are a secondary hint only, because: vendor and product names are not unique or stable and do not map to package names; `*` and `-` mean different things in version fields and ranges are expressed in separate configuration nodes; many records have no configuration (the enrichment status issue in section 7); a configuration saying "running on" an operating system is not a statement about a library; and a CPE match says nothing about distribution patches. Therefore a CPE-only match yields potentially affected at most, with the `cpe-hint` marker, and the report prints it under a separate heading.

### 8.4 Withdrawn and modified advisories

The snapshot keeps withdrawn records. Evaluation retires a finding whose advisory is withdrawn, keeps it in the history with the withdrawal time and reason, and never counts it. A record modified between two snapshots is re-evaluated; `report` can show "changed since snapshot X" by comparing two `findings.json` files. Rejected NVD records are treated like withdrawn ones. The ingest today never parses withdrawn data and never deletes a row; that gap is closed by the new store, not by the old table.

### 8.5 VEX, suppression and false positives

Three different things, kept apart:

* VEX statement: a supplier's or operator's statement about exploitability in a product. It is the only input that yields `not affected`. Stored as the file the author gave (its digest recorded), with author, timestamp, scope and the validity window; statements with no author or no scope are rejected. A CycloneDX `false_positive` is imported as `not_affected` only when it carries a justification; otherwise it is `unassessed` with a caveat.
* Suppression: an operator decision that a finding is accepted or deferred. Record: finding key (advisory, purl, version, asset), reason, owner (a named identity), created, expires (required; an absent expiry is refused; a maximum validity is configurable), approver. An expired suppression stops applying at the next `assess` and the finding returns with the caveat "suppression expired". A suppressed finding stays in every report with its suppression visible; it never becomes `not affected`.
* False positive: not a state. It is handled by a VEX statement with a justification (for example `code_not_present`), authored by a named person, or by a correction to the matcher with a regression fixture. Marking something a false positive without a justification is not accepted.

### 8.6 Priority context

KEV and EPSS inform ordering only. The report shows them in a separate column labelled "context", with the feed identity and date, and never in the state. A KEV listing does not turn potentially affected into detected, and an EPSS value is not shown as the chance of attack on this host.

## 9. Data model and storage

### 9.1 What is stored

Design sketch of the R0 store (SQLite, one file per scope, standard library driver; DECISIONS D1 explains why not the existing Doris table). Names are targets.

| Table | Key fields |
|---|---|
| `assets` | `asset_id` (stable, derived from the declared target identity and a fingerprint such as the image digest or host id supplied by the operator), kind, declared scope id, first and last inventoried |
| `sbom_documents` | `sbom_digest`, format and spec version, generator name and version, asset id, component count, coverage (ecosystems and catalogers seen) |
| `components` | `sbom_digest`, `purl`, name, version, ecosystem, supplier hint, hashes, layer or path |
| `scanner_runs` | `run_id`, scanner name and version, scanner database version and build time and digest, command line (redacted), exit code, native artifact path and digest, started and finished (producer-supplied) |
| `feed_snapshots` | `snapshot_id` (manifest digest), per-feed source freshness fields, counts, licence labels |
| `advisories` | normalised: id, aliases, source, ecosystem, package, `affected` (full structure), fixed versions, published, modified, withdrawn, source snapshot, record digest |
| `findings` | state, advisory id, purl, installed version, affected range, matched_by, scanners_reporting, context, caveats, `assessment_run`, evidence digests (SBOM, scanner artifact, snapshot) |
| `vex_statements` | statement digest, format, author, issued, scope, status, justification, impact statement, valid until |
| `suppressions` | finding key, reason, owner, approver, created, expires, status |
| `receipts` | run record, exit code, tool versions, digests of every input and output |

Every finding row can be traced to: the asset, the SBOM digest, the scanner and database versions, the feed snapshot and its source timestamps, the evidence file digests, and the caveats that applied. A finding with a missing link in that chain is not stored as detected.

### 9.2 Where it lives

R0 keeps findings and receipts in the store above and in the output directory (files under `records/`, `native/`, `sbom/`, `feeds/`). The qa-evidence bundle (section 13) is built from the output directory, not from the database. An optional adapter can write the same rows to Apache Doris for a multi-asset deployment (Planned, after R0), because Doris is a heavy dependency for a one-host package and the audited table cannot hold the model above.

### 9.3 Existing Doris table: reusable and wrong

The existing table `vulns(source, cve_id, package, ecosystem, severity, cvss, fixed_version, vulnerable_range, published, modified, summary, refs, aliases, owner)`, unique key `(source, cve_id, package)`, replication 1, loaded by an ingest script in a private deployment copy.

| Aspect | Verdict |
|---|---|
| Columns `source`, `cve_id`, `ecosystem`, `published`, `modified`, `summary`, `aliases` | Reusable as the shape of the advisory view |
| `package=''` for every NVD row (`_nvd_record`) | Wrong for matching: the rows cannot be joined to a component. The unique key then collapses all NVD data for a CVE to one row. Fix: NVD enters only as an enrichment source keyed by CVE, never as a package advisory |
| OSV rows keep only the first `affected[]` entry and its first range | Wrong: advisories that list several packages, ranges or ecosystems lose data. Store the full structure |
| `vulnerable_range` as a free `VARCHAR(255)` string | Wrong for evaluation: needs structured events (introduced, fixed, last affected) and the ecosystem's version scheme |
| `cvss DOUBLE` with `0.0` when absent, `severity` `UNKNOWN` | Wrong: an unscored record and a score of zero are different; store null |
| no `ingested_at`, no feed snapshot hash, no source freshness | Wrong for reproducibility and for stale-feed detection |
| no deletion and no withdrawn handling | Wrong: a withdrawn advisory keeps producing results |
| `owner` column set to the source name | Mislabelled; the model needs an owner only on suppressions |
| `refs` and `aliases` joined with `;` and clipped to 2000 and 500 bytes | Lossy; store arrays |
| row scope and column mask in the reader | Reusable as a permissions pattern for the enterprise multi-user case; out of R0 |
| Keyword search over the NVD with a last-modified window | Wrong for inventory-driven evaluation (a fixed keyword list is the owner's stack, not the operator's components); an inventory-driven query is the model |
| Ingest exit codes (non-zero on a failed NVD request, an unresolved OSV id, or zero rows) and the 36 hour stamp check | Reusable principles, extended in section 11 |
| `fetch_osv` swallows exceptions and returns `[]` | Wrong: a dead feed must fail loudly |

## 10. Fixtures plan

All fixtures are synthetic: invented package names, invented advisory ids in a reserved prefix, advisory text authored for the test. No real CVE record or real feed bytes are committed. Each fixture is a directory with an SBOM, a feed snapshot, optional VEX and suppression files, and an `expected.json` that is the frozen expectation set the check program compares against.

| Fixture | Contents | Expected result |
|---|---|---|
| vulnerable | one component at a version inside a synthetic affected range, in two ecosystems | `detected` with the full evidence chain; exit 1 at the default gate |
| patched | the same component at the fixed version, and at a version past the fix | no finding; trace entries `outside_range`; never reported as clean without those entries |
| not-applicable | a component the advisory names under a different ecosystem; a CPE-only look-alike with a different vendor | no `detected`; the look-alike is at most `potentially affected` with `cpe-hint`; the other-ecosystem component produces no match |
| not-affected-vex | the vulnerable component plus a valid VEX statement; the same with an expired statement; with a statement for another version | `not affected` only for the valid one; the expired and out-of-scope statements leave `detected` with a caveat |
| unassessed | a component with no purl, a component in an ecosystem with no matcher, a component with an unparsable version | `unassessed` or `potentially affected` with the reason; never dropped |
| withdrawn | an advisory later withdrawn between two snapshots | finding present under snapshot A, retired with history under snapshot B |
| suppression | a suppression that expires between two evaluation dates | applied before expiry, finding returns after, caveat present |
| stale-feed | a snapshot older than the configured limit; a snapshot missing the KEV feed; a snapshot whose OSV file is empty | `assess` exits 3 with the named reason in each case; none produces an empty "clean" report |
| wrong-asset | evidence (a scanner artifact or findings file) produced for asset A offered for asset B; an SBOM digest that does not match the recorded one | `verify` rejects it; an evidence bundle built from it is flagged by the check program, and altered artifacts are caught by the qa-evidence digest checks |
| scanner-disagreement | two recorded scanner outputs, one reporting a finding and one not | the finding is kept, `scanners_reporting` lists both, caveat added |

Reproducibility test: `assess` run twice over each fixture produces byte-identical output, on macOS and on the Linux CI runner, with the network disabled. The R0 bar's "patched and not-applicable fixtures never yield a false PASS" is the patched, not-applicable and stale-feed rows.

## 11. Failure modes

Rule: a failure to read data is a failure of the run, not an empty result. The only way to get an empty findings list with exit 0 is a snapshot that was read completely and a covered inventory with no match, and the report then says so in those words.

| Failure | Behaviour |
|---|---|
| a feed request fails, times out, returns a non-success status or an error body | the refresh fails (exit 3); the partial snapshot directory is marked incomplete and refused by `assess` |
| a feed returns success with an implausible body: zero records, or a record count more than a configured fraction below the previous snapshot, or a schema change | refused as "implausibly empty" or "schema changed" (exit 3) |
| the snapshot lacks a feed the policy requires | `assess` exits 3 naming the feed |
| a feed in the snapshot is older than its limit | caveat on every finding and exit 3, unless the recorded "evaluate this frozen snapshot" flag is passed |
| a scanner's database is missing, older than its limit, or failed to update | scanner step exits 3; the scanner is recorded as not run, never as "no findings" |
| a scanner exits non-zero or produces unparsable output | native status error; findings from that scanner are absent and the report says so |
| a scanner reports zero components or zero packages scanned | treated as implausibly empty, not as clean |
| SBOM generator sees no components | exit 3 unless the target was declared empty |
| a VEX or suppression file is malformed, unsigned where signing was required, or its author or scope is missing | rejected with a reason; the finding keeps its prior state |
| the store or output directory is not writable, or a digest does not match on re-read | exit 4; no partial report |
| rate limit response (HTTP 429 or a timeout cluster) | back off per the feed's limit (section 7); after the retry budget, fail the refresh. Never skip a page and continue |

Every failure is written to `run.json` and to the evidence bundle as a record, so a missing result is explainable from the evidence alone.

## 12. Security model

* Declared scope only. A scope file lists targets (paths, image references already local, SBOM files) and, for any later network-facing mode, host allow-lists. Anything not in it is refused. Third-party systems are never targets.
* Read-only. Scanners get the target mounted read-only, no network when run against a local snapshot, a non-root user, a dropped capability set, no new privileges, a CPU, memory, time and output-size cap, and a scratch directory that is discarded. Linux first; other platforms are marked unsupported until the same isolation is demonstrated.
* No Docker socket. Image scanning reads image archives or a registry-less directory the operator exports (`docker save` run by the operator), or an OCI layout. The audited tree already ships a default deployment with no socket and an opt-in overlay behind a path-filtering proxy; its own documentation states that a path allow-list cannot inspect request bodies and that the read-only mount does not make the Docker API read-only (ES-00 section 2.6). R0 does not carry the socket proxy and does not need it.
* Network. Only `refresh-feeds` opens connections, to the feed hosts in section 7, with a fixed allow-list, TLS verification on, and the NVD key read from a file or an environment variable and never written to output. Scanners and the matcher run with the network disabled.
* Rate limits. Feed clients enforce the published limits with margin (NVD: at most one request every 6.5 seconds without a key, as the existing ingest does, and 0.7 seconds with one) and a maximum retry count.
* Secret redaction. Command lines, environment and output pass a redaction step before they enter records: known token patterns, the configured key names, and any value from the secret files declared in the scope. SBOMs of a filesystem can contain file paths with sensitive names; paths are recorded as given by the SBOM and the operator is told so in the `inventory` output.
* Untrusted input. SBOMs, VEX files, scanner output and feed data are parsed with size caps and without executing anything from them; no archive is extracted outside a scratch directory; every string that enters a report is escaped for the output format.
* Supply chain. Pinned scanner versions with checksums, a recorded SBOM of Edge Sentinel itself, and a refusal to run an unpinned scanner.
* Prompts. No LLM is in the R0 path. If the chat console later presents findings, it receives records, not raw scanner output, and its tools cannot start a scan or a remediation (the existing console already marks such tools side-effecting with an empty allow-list: CI-tested in the private deployment copy).

## 13. Evidence export to qa-evidence

The merged format is `packages/qa-evidence` in the redevops-qa repository (read at commit 2ad97f3, 2026-11-01; its README and schemas are the contract). Edge Sentinel is an additive consumer. It writes bundles that the unmodified verifier accepts; it adds no field, role, schema or vocabulary to that format.

Mapping:

| Edge Sentinel item | qa-evidence element |
|---|---|
| one evaluation run | the run: `run_id`, `plan_digest` (the digest of the frozen expectation set), `environment` |
| a scanner | a lane: `lane_id` = scanner name, `engine {name, version}`, `native {status, raw_status, exit_code, artifacts[]}` with status `NATIVE_PASS` or `NATIVE_FAIL` only if the scanner ran to completion (its exit code under its own rules), `NATIVE_ERROR`, `NATIVE_TIMEOUT` or `NATIVE_UNAVAILABLE` otherwise |
| raw scanner output, the scanner's database metadata as emitted | files under `lanes/<scanner>/native/`, role `native-artifact` |
| the SBOM, the normalised findings, the evaluation trace, the feed snapshot manifest, `freshness.json`, VEX and suppression files, `run.json` | files elsewhere in the bundle, roles `record` or `observation` |
| the expectation set | `assertion-lock.json`, produced by `build_bundle` from the frozen assertions; each assertion is `{assertion_id, lane_id, digest}`, for example "finding set for fixture asset A equals the expected set" or "no finding of state detected exists for component C at version V" |
| the check program's decision | a verdict per assertion with a state and a reason code from the closed list, provenance naming the verifier and the evidence file |

Rules that follow from the format:

* Scanner output is a native artifact. A verdict's evidence may not be a native artifact, may not live under a native directory, and may not equal native bytes by digest. A scanner result therefore can never by itself be `VERIFIED`; the format enforces it, and the design relies on that.
* The independent check program reads the recorded `findings.json` (an observation or record produced by `assess`, not scanner bytes) and compares it with the frozen expectation set, writing `VERIFIED` (`EQUALS_EXPECTED` or `PREDICATE_HOLDS`), `FAILED` (`EXPECTED_VALUE_DIFFERS` or `PREDICATE_VIOLATED`) or `INCONCLUSIVE` (`EVIDENCE_MISSING`, `EVIDENCE_UNUSABLE`). `VERIFIED` here says the recorded findings equal the frozen expectation for a synthetic fixture. For a real target there is no frozen expectation of the true vulnerability set, so a real-target bundle states findings and caveats, and its assertions are limited to properties that can be frozen (the SBOM digest equals the declared one, every finding has its evidence chain, no finding rests on a feed older than the limit, no `detected` state without a version-evaluated match). It does not say the host has no other vulnerabilities.
* Run outcome follows the format: any lane that did not run to completion caps the run at `INCONCLUSIVE`, every lane needs at least one frozen assertion, and a `NATIVE_PASS` lane may not report failed counts. A dead feed therefore cannot produce a `VERIFIED` run.
* Roles are labels written by the producer. The format says so and so does this design: Edge Sentinel does not claim that an observation file is what an independent party saw. The independence of the check program is a separate program, separate parser and separate code path under the same producer; it is not a separate trust domain. Integrity against a deliberate re-forging exists only when the manifest digest is compared with a value published outside the bundle (the anchor), and the plan digest must be supplied from outside to show the expectation set was not trimmed. The report prints the format's own "not established" notices when no anchor or plan digest was supplied.
* Path rules: ASCII, at most 100 bytes, no spaces; asset and advisory identifiers are mapped to safe path components (a hash prefix with a readable suffix where it fits), with the real identifier inside the file.
* Stable size caps (4096 files, 64 MiB a file, 256 MiB a bundle) bound what a bundle may contain; raw scanner databases are never included, only their identity and digest.

Interface points, so each side ships independently:

1. The bundle layout and report schema of qa-evidence 1.0, pinned by commit or tag. Edge Sentinel treats `python -m qa_evidence verify BUNDLE` as the acceptance test and runs it offline in its own CI, with a golden bundle.
2. The library API `assemble_report` and `build_bundle`, used through an optional extra pinned to a commit. If the package is absent, `report` still writes the output directory and says no bundle was built; no other feature depends on it.
3. The `qa_evidence digest` value, handed to the operator's CI attestation step. Edge Sentinel does not implement attestation.
4. The licence relationship between the packages (DECISIONS Q2).

Edge Sentinel does not depend on the QA runner, the QA verifier's engines, runtime-contracts, agent-harness or any identity-workstream feature. The QA side does not depend on Edge Sentinel. If the pinned qa-evidence changes its major version, Edge Sentinel's export is disabled until the adapter is updated, and nothing else changes.

## 14. Remediation coupling for ES-01

R0 has none. ES-01 adds the response workflow: finding, risk and exposure assessment, proposed remediation, explicit approval, constrained action, QA verification, audit receipt. It starts with seeded findings and dry-run execution only.

Coupling rules:

* A finding is an input. A finding of any state never causes a write. There is no code path from `assess` to an executor.
* A proposal is a data object: action type from a closed list (for the first slice, "upgrade package P on asset A to version V" and "disable service S"), the finding keys it addresses, the exact operation (command and arguments, or a package plan) rendered to a canonical form, and the operation digest. Risk and exposure inputs (KEV, EPSS, asset exposure supplied by the operator, fix availability) are context, shown to the approver, not an automatic decision.
* Approval binds to the operation digest. Any change to the operation changes the digest and voids the approval. The existing chain for the ban action (staged request with a content-addressed id, authenticated operator decision with the recorded actor, a second enable switch, a recorded dry-run receipt, then a separate read-only verification step; CI-tested for bans in the private deployment copy and, for the library layer, in the public tree) is the pattern to reuse. Reuse means the same state machine, not importing ban code into vulnerability handling.
* Dry-run first: the executor renders what it would do, checks preconditions read-only (package exists, version available from a configured source, the asset matches the proposal), and records a dry-run receipt. A dry-run receipt cannot be promoted: real execution needs a new, separately authorised request that references it.
* Real execution needs the identity workstream's authorised-execution primitives (agent identity, delegated scope bound to tool, resource and action, the operation digest, a short-lived credential from a broker). Until those exist, the executor interface is present and refuses with "no execution authority configured". This design does not define those primitives; it defines the point where they plug in (section 15).
* QA verification: after any real execution (not in the first slice) a read-only re-inventory and re-assessment runs, and a check program compares the new findings with the expectation "the addressed findings are absent and no new detected finding appeared in the touched components". The result is a verdict in the same bundle format.
* Receipt: decision, attempt, outcome, partial effects, evidence references. A receipt of a dry run says dry run in its first field.

Acceptance (also in DECISIONS): seeded findings in dry-run mode cannot trigger a real write with every authorisation switch off, with the enable switch on but no execution authority, and with a forged approval; this is the owner's demonstration 7.

## 15. Interface points with other workstreams

| With | Interface | Dependency direction |
|---|---|---|
| QA (qa-evidence) | bundle layout, `verify` CLI, optional build API (section 13) | Edge Sentinel consumes the pinned format; QA does not depend on Edge Sentinel |
| Identity workstream | the executor's authority check (ES-01): a function that takes an operation digest and returns an authorisation or a refusal. Until the identity work ships, the answer is always refusal | Edge Sentinel consumes; absence means no execution, not a degraded mode |
| Identity workstream | agent identity strings in receipts (an optional field in Edge Sentinel's own records) | optional; no behaviour depends on it |
| Governance and approvals (AGPL-side) | the approval state machine used by the existing response layer | existing |
| Enterprise | per-tenant authorisation of findings and multi-user approvals | out of scope; the single operator credential is the first-package assumption (DECISIONS Q8) |

## 16. Wording rules for this capability

May say, once the capability exists and its tests pass: "lists installed components from an SBOM", "reports advisories that list an installed version as affected, from a named feed snapshot", "shows the age of the data behind each result", "keeps scanner output unchanged and records how it was produced", "evaluation is reproducible offline against a frozen snapshot", "an independent check compares recorded findings with a frozen expectation".

May not say: "no vulnerabilities", "clean", "secure", "compliant", "confirmed vulnerable" or "exploitable" for a scanner or matcher result; "real-time" or "continuous" for a periodic evaluation; "SBOM" as proof of what is running; any claim about CVE coverage ("covers all CVEs"); "audit" for a technical test.

Before the capability is demonstrated, none of these appear in public copy (the current demo offers none of them).

## 17. External facts register

All retrieved 2026-11-01.

| Fact | Source | Read how |
|---|---|---|
| Syft, Grype, Trivy, trivy-db, OSV-Scanner, osv.dev, cdxgen, cyclonedx-python, grype-db, vunnel licence Apache-2.0; CycloneDX specification Apache-2.0; OpenVEX spec CC0-1.0; OpenSCAP LGPL-2.1; ComplianceAsCode content BSD-3-Clause | GitHub repository licence metadata and the licence files in `anchore/*`, `aquasecurity/*`, `google/*`, `CycloneDX/*`, `openvex/spec`, `OpenSCAP/openscap`, `ComplianceAsCode/content` | primary (API) |
| Release versions and dates in sections 5 and 6 | GitHub latest-release API for the same repositories | primary |
| SPDX specification licensing | https://github.com/spdx/spdx-spec/blob/main/LICENSE | primary |
| NVD API: 5 and 50 requests per rolling 30 s; notice wording; modification and name-use clauses | https://nvd.nist.gov/developers/start-here, https://nvd.nist.gov/developers/terms-of-use; text read at https://open.windriver.com/info/uni-license-list/licenses/nist-nvd-api-tou.html | secondary (page reader returned an empty shell; the limits came from a search extract of the NVD page, the terms from the mirror) |
| NVD data licence; 120-day window documentation; enrichment-status changes | not read | UNVERIFIED (enrichment: secondary press reports only) |
| OSV API has no stated limits; per-source data licences; dump locations; withdrawn handling | https://google.github.io/osv.dev/api/, https://google.github.io/osv.dev/data/ | primary |
| KEV CC0 1.0; feed URL and fields | https://github.com/cisagov/kev-data; the live JSON feed | primary |
| EPSS free and daily; file and API formats | https://www.first.org/epss/; the live CSV and API responses | primary for access and format; reuse terms UNVERIFIED |
| Trivy database flags; air-gap behaviour | https://trivy.dev/latest/docs/configuration/db/, https://trivy.dev/latest/docs/advanced/air-gap/ | primary |
| Grype database commands and configuration keys | `anchore/grype` source tree, `cmd/grype/cli/` | primary (source) |
| OSV-Scanner offline flags | https://google.github.io/osv-scanner/usage/offline-mode/ | primary |
| CycloneDX VEX states and justifications | `CycloneDX/specification` `schema/bom-1.6.schema.json` | primary |
| OpenVEX statuses and justifications | https://github.com/openvex/spec/blob/main/OPENVEX-SPEC.md | primary |
| Trivy and Grype aggregate database redistribution terms; scanner signing artefacts; Trivy DB metadata field names | not read | UNVERIFIED |
