# ReDevOps Product Distribution — delivery matrix (2026-11-19)

Generated from real, verified state after plan milestones M1–M8 (see `.claude/plans/elegant-yawning-eclipse.md` for the
full plan). Every row cites the evidence for its status; nothing here is aspirational. "Blocked on" names the next
concrete action and who owns it.

## Product A — rAIls Runtime + Apps Suite (Agentic Apps R0-prime)

| Artifact | Build | Sign | Test | Publish | Store | Blocked on |
|---|---|---|---|---|---|---|
| Windows MSI/EXE | DONE — `windows-signing-r0prime.yml`'s `collect`/`apply-and-rebuild` | **DONE, real** — Azure Trusted Signing, chain to Microsoft's root, `signify` verification PASS (hash, signature, chain, EKU, exact CN, RFC 3161 timestamp). Repeated, successful real runs. | Install/uninstall/regression: DONE (`packaging/acceptance/windows-r0prime/`). Upgrade: `native-upgrade-test.yml` exists (M8) but **not yet dispatched** — unverified. | `release-publish.yml` exists (M3), gated on the distribution gate + UI source publication, **not yet dispatched**. The repo has other GitHub Releases (`v0.6.1`, `v0.4.x`, ...), but none for the R0-prime line — this would be the first. | S-A engineering-ready (offline WebView2, silent install, publisher metadata, real signing — all verified for real). No Partner Center account. | Owner: dispatch `release-publish.yml` when ready; open a Partner Center account (D-MS-1) for S-A submission. |
| macOS DMG | Not built for R0-prime. Script exists (`sign_and_notarize_r0prime_macos_v3.sh`), never run with a real identity. | **Not done** — no real Apple Developer ID credentials exist in this repo (checked: only one GitHub Environment, `windows-signing`; no macOS-signing environment at all). | None. | None. | Mac App Store: `docs/release/MAC_APP_STORE.md` states the product **cannot be submitted as-is** (sandboxing/entitlement rework, 4–8 weeks) — explicitly deferred, not pursued this release (plan M7). | Owner: provision Apple Developer ID signing credentials (certificate + notarization API key) before M5 can proceed at all. |
| Linux .deb / AppImage | DONE — `native-packaging.yml` | Not signed (no Linux signing requirement identified). | Install: DONE (`packaging/acceptance-ubuntu.sh`). Upgrade: `native-upgrade-test.yml` (M8), **not yet dispatched** — unverified, likely needs iteration (two full native builds, new workflow). | Same as Windows (`release-publish.yml` could attach the .deb too; not yet done). | N/A | Dispatch and iterate on `native-upgrade-test.yml`. |

## Product B — Edge Sentinel / redevops-qa / Projects

| Artifact | Build | Sign | Test | Publish | Blocked on |
|---|---|---|---|---|---|
| Edge Sentinel (`ghcr.io/redevops-io/edge-sentinel`) | DONE | **DONE, real** — `oci-edge-sentinel.yml`, keyless cosign + build provenance attestation | DONE, two independent paths: standalone-only (M8, new, passing — found and fixed a real secrets-permission bug on first real run) and inside the combined bundle (`combined-install-e2e.yml`, passing) | `v0.6.3` published | None known |
| redevops-qa (separate repo, `redevops-io/redevops-qa`) | DONE (that repo's own `oci-release.yml`) | **DONE, real** — same shape (cosign, SBOM, attestation) | That repo's own CI is green as of this check (`main` branch, all recent runs `success`) | `v0.6.2` published | Pre-release status (`LICENSE.md`: "the public licence of the finished product is not final"); narrow, gated-demo-only integration with Edge Sentinel by its own design, not a general engine |
| Projects UI (desktop app's bundled copy, `packaging/projects_ui.lock.json`) | DONE — locally built, richer than the public release (accessibility series + mission drill-down views) | N/A (not a signed artifact; part of the signed desktop installer above) | Covered by the desktop installer's own tests | **`source_commit_published: false`** — this build's source has never actually been published. `release-publish.yml` (M3) treats this as a hard block on any real distribution release regardless of the legal gate passing. | Owner: decide whether to publish the source commit (a `redevops-projects` repo action) before any real distribution release |
| Combined bundle (`ghcr.io/redevops-io/redevops-ts`) | `oci-redevops-ts-bundle.yml` exists (M4), **not yet dispatched** | Designed (cosign + attestation), **unverified** — no real publish has run | The file-secrets install path is tested and green (`combined-install-e2e.yml`'s `combined` job). The single-pull OCI path this workflow produces has its own real smoke test built in (runs `bundle-bootstrap.sh` against the freshly-published artifact) but has never actually executed. | One ad-hoc manual publish exists (`0.1.0`, 2026-11-15, per `bundle-bootstrap.sh`'s own comment) — not from this signed, automated pipeline. | Dispatch `oci-redevops-ts-bundle.yml` for the first real, signed, smoke-tested publish. |

## Shared release infrastructure

| Item | Status |
|---|---|
| `pins-05` ("no workflow may publish") | Narrowly reversed (M3), owner-approved: `tests/test_workflow_no_publish.py`'s `PUBLISH_ALLOWED_WORKFLOWS`/`OIDC_SIGNING_WORKFLOWS` name exactly `windows-signing-r0prime.yml` (signing only, never publishes), `oci-edge-sentinel.yml`, `release-publish.yml`, `oci-redevops-ts-bundle.yml`. Every other workflow is still fully refused. |
| Legal/distribution gate (`packaging/check-distribution-gate.py --distribution --about-slots`) | **Passes** — all 21 decisions DECIDED with valid signatures, verified directly (not just by a stale doc). `docs/release/RELEASE_INVENTORY.md` (a dated 2026-11-05 snapshot) and `docs/release/MICROSOFT_STORE.md` both corrected to match this (M6). |
| D-LIC-16 ("do not distribute the QA bundle") | Predates `main`'s later Edge Sentinel/redevops-qa integration work (M1's port). **Still needs owner re-confirmation** — not resolved by this matrix. |
| Independent installability ("neither product requires the other") | Product B → standalone: verified (M8, new CI job, found a real bug on its first run). Product A → without Product B: implicitly true (the desktop build never references `apps/edge-sentinel`/`agentic_os/products` at all; `packaging/acceptance/*` never touches them) — no dedicated negative-check added this round. |
| Acceptance testing gaps closed | Edge Sentinel standalone-only CI (M8). Native upgrade test exists for Product A but is unverified (M8). |
| Acceptance testing gaps still open | `native-upgrade-test.yml` has never been dispatched — budget ~90 min, likely needs iteration (same discipline the Windows signing pipeline needed across 8 PRs before it worked). |

## What's actually ready to ship vs. what needs a decision

**Ready, pending only a dispatch (your call, no further engineering needed):**
- `release-publish.yml` — would create a real GitHub Release of the signed Windows installers, *if* you also resolve the UI source-publication gate (gate 2) or decide to relax it.
- `oci-redevops-ts-bundle.yml` — would create the first real, signed, smoke-tested publish of the combined bundle.

**Needs your decision before it can ship:**
- Publish the Projects UI source commit (or decide the gate should not block on it) — **in flight**: PR #698 re-pins it to a
  published public commit (the original local commit was lost in the 2026-11-07 scratchpad wipe), not yet merged.
- Re-confirm or supersede D-LIC-16 for the QA bundle.
- Open a Partner Center account if you want to pursue the Microsoft Store (S-A) — see
  `docs/release/PARTNER_CENTER_SUBMISSION_CHECKLIST.md` for what to gather first and the engineering steps (a fresh signing
  run, dispatching `release-publish.yml`) that still have to happen before a real submission, not just the account.

**Needs your action before engineering can even start:**
- Provision real Apple Developer ID signing credentials (M5) — no macOS signing environment exists in this repo yet.

**Explicitly not pursued this release:**
- Mac App Store (M7) — the product cannot be submitted as-is; Developer ID + notarization is the realistic macOS channel once M5's credentials exist.
- MSIX / Microsoft Store S-B — no code exists beyond a draft manifest; S-A is the recommended path.
