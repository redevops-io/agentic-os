# R0-prime macOS acceptance kit (dmg verification, headless sidecar smoke test, manual GUI checklist)

Purpose: run the Agentic Apps macOS arm64 candidate through a repeatable acceptance test on a **fresh local macOS account**, and
record what happened. It is the macOS counterpart of `../windows-r0prime/` and `../../ACCEPTANCE-ubuntu.md`.

The kit **prepares and verifies**. It never starts the launcher, opens a window or a browser, signs anything, touches a keychain,
uses `sudo`, or changes a system setting. The GUI steps are done by a person following `MANUAL-CHECKLIST.md`.

Evidence status of the kit (levels as in the R0-prime brief):

| Part | Status |
|---|---|
| `verify_dmg.sh` | OBSERVED on three inputs: the unsigned local development dmg and an unsigned fixture dmg (`--expect unsigned`), and the signed + notarized + stapled v0.3.2 dmg (`--expect signed`, read-only; its Licenses/ rows fail as they should, v0.3.2 has none). Negative runs (wrong mode, wrong hash) fail closed. |
| `headless_smoke.sh`, `post_run_checks.sh`, `collect_evidence.sh`, `fallback_throwaway_home.sh` | OBSERVED on the unsigned local development build, in the owner's session (this is NOT the fresh-account run). `headless_smoke.sh` also fails closed against the v0.3.2 sidecar (no one-time URL). |
| `MANUAL-CHECKLIST.md`, expected outcomes of the GUI steps | STATIC / INFERRED. Nobody has launched the Tauri window of this candidate. Every "expected" cell is a prediction from the source (`deploy/launcher/ui/index.html`, `deploy/launcher/src-tauri/src/*.rs`, `agentic_os/local_capability_shim.js`), not an observation. |
| Gatekeeper first launch, offline first launch, Intel | UNVERIFIED (see the end of this file). |

## Files

| File | Runs on | Purpose |
|---|---|---|
| `verify_dmg.sh <dmg> <expected-sha256-file>` | the owner's build/verification account (needs the Xcode Command Line Tools for `vtool` and `stapler`; without them those rows are SKIPPED) | hash, read-only attach, contents, Licenses/, Info.plist vs Mach-O `minos`, signatures, entitlements, Gatekeeper and stapler assessments, detach. `--expect unsigned` (default) or `--expect signed`. |
| `headless_smoke.sh <app>` | any account, no developer tools needed | runs the embedded sidecar (no GUI) under `env -i` with throwaway HOME/TMPDIR/port and checks the local API contract; stops it by exact PID; proves nothing is left. |
| `post_run_checks.sh` | the test account | processes, listeners, `_MEI*` directories, leftovers in `~/Library`; read-only; never touches the keychain. |
| `collect_evidence.sh` | the test account | one evidence directory: host facts, copies of transcripts and screenshots, SHA-256 of every file. |
| `fallback_throwaway_home.sh` | the owner's session, only with the owner's explicit approval | prepare/verify a throwaway-HOME run; never runs the app. |
| `MANUAL-CHECKLIST.md` | the test account, by a person | named screenshots, GUI steps, expected outcomes. |
| `SIGNING-READINESS.md` | reading | what must be signed, in which order, what differs from the v0.3.2 procedure. Not executed. |
| `INTEL-ROSETTA-TEST-PLAN.md` | reading | the Intel / Rosetta question. The candidate is arm64 only. |
| `_lib.sh` | (sourced) | shared helpers. |

All scripts are bash 3.2 compatible (the macOS `/bin/bash`; the login shell may be zsh, the shebang handles it), ASCII only, no
`sudo`, no `security`, no `open`, no `rm -rf`. They are idempotent: each writes to a new `--out` directory (default
`./r0p-out/<name>-<UTC stamp>`; an existing non-empty `--out` is refused) and never modifies the inputs. `--help` prints each script's header.

## Why a fresh account

A clean account removes every source of false results: no previous sidecar listening on 8787 (v0.3.2 answers unauthenticated),
no `~/.agentic-os` or `~/.redevops`, no old WebKit data for the bundle id, no developer tools that hide missing system
dependencies, no trusted-developer history, an empty login keychain, and no stale Gatekeeper approvals.

**The account has to be created by the owner.** This session cannot create it (`sudo` needs a password and `dscl` shows only the owner's account).

## Account prerequisites

1. A **standard** user (not an administrator). The kit records the account type (`collect_evidence.sh` prints it) and every checklist
   step that needs an administrator password is written down as a finding, not worked around.
2. Created with **System Settings > Users & Groups > Add User...** (the owner authenticates once, as administrator): Account Type
   **Standard**, a full name such as `R0 Prime Test`, account name `r0ptest`, a throw-away password. Do **not** tick "Allow user to administer this computer".
3. At the first login, in the setup assistant: **skip** Apple ID / iCloud sign-in (no iCloud Keychain, so the login keychain starts empty),
   skip Siri/analytics offers, do not enable anything optional. Do not sign in to the App Store.
4. **No Xcode, and no developer tools used in this account.** On a Mac without any developer tools, `xcode-select -p` prints an error, and typing `git`,
   `python3`, `make`, `clang` or `xcode-select --install` in Terminal triggers the install dialog: never do that there. The scripts of this kit
   use only tools that ship with macOS (`bash curl lsof ps pgrep file shasum hdiutil codesign spctl xattr plutil PlistBuddy`; `vtool` and `stapler` are optional and
   only used when present). **This Mac has the Command Line Tools installed system-wide** (`/Library/Developer/CommandLineTools`, no `Xcode.app`), so every
   account here sees them and `xcode-select -p` succeeds in the test account too. That is a property of the machine: `collect_evidence.sh` records it, and the
   test must not use those tools. Only a Mac that never had them (or a VM) can prove "works without developer tools".
5. The login keychain is not inspected by the kit (it would need the `security` tool; the kit never uses it). A newly created account has an
   empty login keychain; record that the account was new and that no password was saved into it during the test.
6. Optional but useful: System Settings > Control Center > **Fast User Switching** > "Show in Menu Bar" (set from the owner's account).

## Getting the dmg into the account without changing it

The dmg must reach the test account with the **same bytes** and a **known quarantine state**. Which state is a test parameter:

| Variant | How | What it exercises |
|---|---|---|
| **Q** (quarantined, the realistic one) | download it in the test account's own browser from wherever the candidate is published for the test, **or** copy it with `cp`, `cp -p`, `cp -pX` or `ditto` from a location where it already carries `com.apple.quarantine` (OBSERVED on macOS 27: all four keep the attribute; `rsync -a` / `openrsync` do **not**, so never use rsync) | the Gatekeeper first-launch path |
| **Q-set** (quarantine set deliberately) | on the copy only: `xattr -w com.apple.quarantine "0081;$(printf '%x' $(date +%s));Safari;" <file>`. Flag value `0081` is what a browser writes on a fresh download; the meaning of other values (for example `0083` after an approved first open) is INFERRED. Record the exact value used. | same path as Q, when the file came from a channel that strips the attribute (AirDrop, a shared folder, a USB stick) |
| **N** (no quarantine) | the file as it is (no attribute) | what a developer sees: Gatekeeper is **not** involved. Not an acceptance of the first-launch path. |

Copy through a world-readable drop folder that the owner creates, for example `/Users/Shared/r0p-handoff/` (contents: the dmg, the
expected-sha256 file produced on the build machine, this kit folder). Then in the test account:

```bash
xattr -l "/Users/Shared/r0p-handoff/<name>.dmg"       # note the quarantine value (or its absence) BEFORE anything else
shasum -a 256 -c /Users/Shared/r0p-handoff/SHA256SUMS # must say OK for the dmg
```

`verify_dmg.sh` records the extended attributes before and after; it attaches with `-readonly -nobrowse -noverify` and checks that the dmg's
bytes, size, mtime and attributes did not change. (`hdiutil attach` WITH verification writes a `com.apple.diskimages.recentcksum`
attribute onto the file, OBSERVED; the script avoids it and reports it as `D-04` if it ever appears.) `hdiutil attach` prints a deprecation notice on
macOS 27 recommending `diskutil image attach`; the kit keeps `hdiutil` because that is the documented procedure.

## Logging in, switching, screenshots

1. Owner's account: menu bar > Fast User Switching icon > `r0ptest` (or Apple menu > Lock Screen, then choose the user). Log in; the owner's
   session stays alive in the background (this matters: a Docker/dev process of the owner keeps listening, and `post_run_checks.sh` only inspects the **current** user).
2. Screenshots: **Shift-Command-3** (whole screen), **Shift-Command-4** (selection), **Shift-Command-5** (toolbar; Options > Save to: choose
   `~/r0p-evidence/screenshots`). Files get time-stamped default names; rename them in Finder (select, Return) to the **exact names in
   `MANUAL-CHECKLIST.md`** (`C-01-gatekeeper-first-launch.png` ...). No editing, no cropping of dialogs.
3. Terminal output: copy the script transcripts (they are plain text); for ad-hoc commands, `script`-style capture is not needed, paste the output into `~/r0p-evidence/notes.txt`.
4. When finished, collect everything with `collect_evidence.sh` and move the evidence directory to `/Users/Shared/r0p-handoff/out/`.
5. Switch back to the owner's account, copy the evidence, then delete the test account: System Settings > Users & Groups > (i) next to the user >
   Delete User... > "Delete the home folder" (owner authenticates).

## Order of work

| # | Where | What |
|---|---|---|
| 0 | owner's account | `./verify_dmg.sh --expect unsigned\|signed --out ... <dmg> <sha file>` on the candidate; fix anything FAIL before involving a person. For the signed candidate also `--team-id <Team ID>`. |
| 1 | test account | `./post_run_checks.sh --out ~/r0p-evidence/post-00-baseline`: expect all PASS (the baseline snapshot is used later with `--compare`). |
| 2 | test account | `MANUAL-CHECKLIST.md` parts B to J (this is where the app is launched, by a person). After each of the stop/quit/kill steps run `./post_run_checks.sh --out ~/r0p-evidence/post-<step>`. |
| 3 | test account | `./headless_smoke.sh "/Applications/Agentic Apps.app" --out ~/r0p-evidence/smoke` (or the `~/Applications` copy): the API contract of the **installed** sidecar, no GUI. |
| 4 | test account | `./collect_evidence.sh --out ~/r0p-evidence/bundle --tester <name> --dmg <dmg> --path ~/r0p-evidence/screenshots --path ~/r0p-evidence/notes.txt ...` and record the printed SHA-256 of `SHA256SUMS.txt` out of band. |
| 5 | owner's account | collect, delete the account. |

Expected pattern for the **unsigned local development build**: `verify_dmg.sh --expect unsigned` PASS (signature rows record "not signed"), `headless_smoke.sh` PASS,
Gatekeeper first launch of a quarantined copy is **blocked** (the standard user cannot approve it without an administrator password; record
the dialog text and do not approve unless the owner is at the keyboard and wants to), launching a copy without quarantine runs but proves nothing about Gatekeeper.
For a signed + notarized candidate the same script with `--expect signed` must be all PASS apart from the documented WARN/INFO rows.

## Fallback: throwaway HOME inside the owner's session

Only if the owner **explicitly approves it later**. It needs no new account but is a weaker test:

* `fallback_throwaway_home.sh prepare` creates `HOME`/`TMPDIR`/cwd directories and a baseline of the real home; `command --i-have-the-owners-explicit-approval ...`
  prints the one Terminal command the owner may run (`cd <cwd> && env HOME=<dir>/home TMPDIR=<dir>/tmp "<app>/Contents/MacOS/agentic-apps"`); `verify` checks what was
  left in the throwaway HOME and what **leaked into the real `~/Library`** (compared with the baseline). The kit does not run the command.
* What it does **not** give: a clean keychain (shared), the Gatekeeper first-launch path (starting the binary directly bypasses LaunchServices and
  the quarantine check), an isolated WebKit data directory (Cocoa resolves the home folder from the account database, not from `$HOME`, so WebKit/Caches
  entries may still land in the real `~/Library`; that is exactly what `verify` reports), a clean Dock/Launch Services registration, or a standard-user (non-admin) experience.
* A GUI run in this mode may be used for items D to G of the checklist (window, buttons, browser hand-off, stop/quit/kill). It must not be reported as a fresh-account acceptance.
* `headless_smoke.sh` does not need approval in this sense: it starts no GUI and uses its own throwaway HOME; it binds a loopback port for about 10 seconds.

## What the results mean

`PASS` / `FAIL`: a check the script could judge. `WARN`: a finding for a human (for example `LSMinimumSystemVersion` 10.13 below the binaries' `minos` 11.0).
`INFO`: a recorded fact. `SKIP`: could not be judged here (for example no Command Line Tools). A run is **not** a release verdict. "Signed" is not "works", and "works headless" is not "works in the window".

## Known limits and UNVERIFIED

* GUI behaviour of the Tauri launcher (all six buttons, the window, the browser hand-off): never observed. Everything in `MANUAL-CHECKLIST.md` is a prediction until a person runs it.
* Gatekeeper first launch of a downloaded dmg/app (unsigned: block dialog and `Open Anyway`; signed: "downloaded from the Internet" prompt) and **offline first launch** of an app copied out of the dmg
  (only the dmg is stapled; the app inside relies on Gatekeeper's online notarization lookup). `spctl -a` and `stapler validate` in `verify_dmg.sh` are assessments of files, not that path.
* Whether the owner's account being on the same machine changes any result (shared `/private/var/folders` is per user; shared `/tmp` is not).
* Intel Macs: not supported by this candidate, not tested (`INTEL-ROSETTA-TEST-PLAN.md`).
* `headless_smoke.sh` runs the sidecar in **standalone** `serve` mode (it prints the one-time URL on stderr). The launcher's hand-off mode (`AGENTIC_OS_HANDOFF=stdout`, the HMAC handshake) is covered by the launcher's own `cargo test -- --ignored` suite (reports/defects.md), not by this kit.
* The kit was written and run on macOS 27.0.1 (Apple Silicon). Other macOS versions are untested; the process, `lsof`, `ps` and `curl` output formats it parses are the stock ones.
