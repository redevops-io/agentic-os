# Microsoft Partner Center: submission checklist (preparation only)

Status: 2026-11-19. No Partner Center account exists yet (D-MS-1, `docs/release/MICROSOFT_STORE.md`). This document turns that
document's research and the `MICROSOFT_STORE.md`/`STORES_MATRIX.md` owner decisions into an action list: what to gather before
opening the account, what the account-creation and submission steps are, and what engineering must still be true (verified,
not assumed) before a real submission happens. It does not open an account or submit anything; account creation and
submission are the owner's own action (D-MS-1).

Route: **S-A, "EXE or MSI app" listing** (`docs/release/MICROSOFT_STORE.md` section 1) is the recommended first route — Partner
Center lists a versioned HTTPS URL to the existing signed `.msi`/NSIS installer, with no new package format. S-B (MSIX) is
deferred; nothing below assumes it.

## 1. What is actually true right now (verified 2026-11-19, not recalled from the documents above)

| Item | State | Evidence |
|---|---|---|
| Windows installer signing | A **real** Authenticode signature (Azure Trusted Signing, chained to the Microsoft root) has been produced and `signify`-verified — but only once, for `BUILD_COMMIT 36bbab35d600d21562d7923a562f85944af8f472` (2026-11-18 run 44391022557). `release/0.6.0`'s tip is now 9 commits ahead of that commit (including the Projects UI re-pin, PR #698), and every `windows-signing-r0prime.yml` dispatch since the successful one has failed. | `gh run list --workflow windows-signing-r0prime.yml`; `git log 36bbab35..origin/release/0.6.0` |
| Offline WebView2 / silent install / publisher metadata | Done and verified on real Windows CI (commit `fc254a34`, PR #664) | `docs/release/MICROSOFT_STORE.md` section 7 |
| Distribution legal gate | All 21 decisions in `packaging/distribution-gate.json` are `DECIDED` with valid signatures | `packaging/check-distribution-gate.py --distribution --about-slots` |
| Projects UI source publication (a separate, hard block in `release-publish.yml`) | Was `false` (unpublished local-only source commit, unrecoverable after a 2026-11-07 data loss); re-pinned to a real published public commit in PR #698 (`ci/publish-projects-ui-source-m10` → `release/0.6.0`, not yet merged as of this writing) | `packaging/projects_ui.lock.json` |
| A versioned, public HTTPS URL for the installer (what the S-A listing actually points at) | Does not exist. `release-publish.yml` (the one workflow authorized to cut a GitHub Release, plan M3) has never been dispatched. | `gh api .../actions/workflows` — no run history for `release-publish.yml` |
| Partner Center account | Does not exist | D-MS-1, below |

**Reading this table correctly:** engineering readiness for S-A is close but not current — the one real signed build is now
stale relative to `release/0.6.0`'s tip, and there is nowhere public yet to point the Store listing's URL at. Neither blocks
*preparing* the account or its submission materials (sections 2–4 below); both block the actual submission (section 5).

## 2. Before creating the account — decide and gather these

These map to `docs/release/MICROSOFT_STORE.md` section 8's open owner decisions. Nothing here needs engineering work; all of it
is the owner's own call or the owner's own paperwork.

- [ ] **D-MS-1 — Account type and publisher.** Individual or Company account in Partner Center. A company account needs a
      D-U-N-S number or equivalent business documents plus a work email on the company's own domain (not a free email
      provider); an individual account needs government ID verification. Decide which legal person/entity publishes
      ("RedevOps" is used as the working name elsewhere in these documents — confirm this is the actual registered name to
      submit, or decide an individual account instead).
- [ ] **Publisher display name.** This is what users see in the Store; it does not have to match the legal entity name, but
      must be decided before the listing is drafted (it appears in `Identity/@Publisher`, which must match the signing
      certificate's subject — confirm the existing Azure Trusted Signing certificate's subject name against whatever
      publisher name is chosen, D-MS-4).
- [ ] **D-MS-3 — Supported configuration.** x64 only for the first listing (recommended in `MICROSOFT_STORE.md`); minimum
      Windows version (10 1809 vs. 11 — only 11 is covered by the acceptance VM tests today).
- [ ] **D-MS-6 — Privacy policy.** A publicly hosted privacy-policy URL is **required** (policy 10.5.1 — the product reaches
      model providers/connectors with user-supplied keys). Counsel drafts the text; engineering's job is only to host it at a
      stable URL before submission. Not drafted yet.
- [ ] **Age rating answers.** The submission's content-rating questionnaire; answers must be truthful. Not yet drafted —
      low effort, do this alongside the listing draft (section 4).
- [ ] **D-MS-7 — Capability declarations.** For the S-A route specifically: **none apply.** `runFullTrust` and the capability
      review (section 5 of `MICROSOFT_STORE.md`) are an MSIX/S-B concern only; an EXE/MSI listing declares no Store
      capabilities. Confirm this stays true if S-B is ever pursued later.
- [ ] **D-MS-8 — Docker/WSL2 dependency disclosure.** If the Store flavour keeps the optional Docker-control feature, its
      description must disclose it as a non-integrated dependency (policy 10.2.4) — decide whether to keep or drop it for
      the Store listing text (`MICROSOFT_STORE.md` recommends dropping it for the Store flavour).

## 3. Opening the account (Microsoft's own process, for reference — not run)

1. Go to `https://partner.microsoft.com/dashboard` (or the developer registration entry point
   `https://storedeveloper.microsoft.com`) and choose Individual or Company per the D-MS-1 decision above.
2. Company accounts: supply the D-U-N-S number or equivalent registration documents, plus a work email on the company
   domain. Individual accounts: government ID verification.
3. Expect manual review: 2–5 business days (Microsoft's own published range, `docs/release/MICROSOFT_STORE.md` section 2 /
   section 9 sources).
4. Reserve the app name once the account is approved (this is a separate, quick step after account approval, done in Partner
   Center's "Create a new app" flow — not evaluated further here since it needs an approved account first).

## 4. Submission materials to have ready (prepare now, submit later)

None of this needs the account to exist yet — draft it in parallel with section 2/3 so submission isn't blocked waiting on text.

- [ ] **Store listing copy**: name, description, feature list. Must be accurate for reviewers (policy 10.1.1) and must
      **not** use "open source" wording (D-LIC-15 — the product is source-available under the family licence, AGPL-3.0-or-later
      with the Commons Clause) or claim certification/compliance it hasn't earned.
- [ ] **Screenshots** meeting Partner Center's current size/count requirements (not checked in this document — read the
      current submission-options page when drafting the listing, requirements have changed over time).
- [ ] **Privacy policy URL** (see D-MS-6 above — counsel text, engineering hosts it).
- [ ] **Age rating questionnaire answers.**
- [ ] **The installer URL itself** — a versioned HTTPS URL to the signed `.msi`/NSIS `.exe` that "never changes" per policy
      10.2.9 (a new version needs a new URL, not an overwrite). This is what section 1's "no public URL yet" gap must close
      before submission (see section 5).

## 5. What must happen in engineering before this can actually be submitted

In dependency order:

1. **Merge PR #698** (Projects UI re-pin) so `packaging/projects_ui.lock.json`'s `source_commit_published` is `true` on
   `release/0.6.0` — `release-publish.yml` refuses to publish a distribution release while it's `false`.
2. **Re-run `windows-signing-r0prime.yml`** for real against `release/0.6.0`'s then-current tip (not the now-stale
   `36bbab35` build) once #698 and any other pending release-line work has landed, so the signed artifact actually matches
   what will be listed.
3. **Dispatch `release-publish.yml`** to cut the GitHub Release and get a real, versioned, public HTTPS URL for the signed
   installer — this is the URL the S-A listing points at. It has never been run; confirm it behaves as designed (the
   distribution-gate precondition check, attaching the signed artifacts) before relying on it for a real submission.
4. **Open the Partner Center account** (section 3) — can run in parallel with steps 1–3 since account review (2–5 business
   days) and app certification (up to 3 business days, per `MICROSOFT_STORE.md` section 2) take their own time regardless of
   engineering state.
5. **Submit** once all of the above are true and the section 2 decisions are made.

Nothing in this document authorizes skipping any of D-MS-1 through D-MS-8, counsel's review of the privacy policy, or the
distribution-gate check that `release-publish.yml` already enforces — it is a sequencing checklist, not a new decision
record.
