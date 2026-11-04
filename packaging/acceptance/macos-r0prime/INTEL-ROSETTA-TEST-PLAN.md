# Intel / Rosetta: statement and test plan (docs level; nothing here has been run on Intel hardware)

## Statement

The R0-prime macOS candidate is **Apple silicon (arm64) only**. Both Mach-O files in the bundle (`agentic-apps`, `redevops-sidecar`) are thin arm64 binaries (`file` and `verify_dmg.sh` row `A-01`,
OBSERVED on the local development build and on the v0.3.2 dmg). **No Intel claim is made**, and none may be made in any document, release note or web page until the plan below has been run.

Rosetta 2 is **not** a route for Intel Macs to run this candidate: Rosetta translates x86_64 code on Apple silicon; it does nothing for an arm64 binary on an Intel Mac. The Rosetta question that
can arise is the reverse one: "would an x86_64 build run on an Apple silicon Mac under Rosetta, and is that a way to test an Intel build without Intel hardware?" It is a partial way (see phase 3).

Expected behaviour of this candidate on an Intel Mac (INFERRED, untested): the system refuses to run the arm64 executable (a "not supported on this Mac" dialog in Finder, `Bad CPU type in executable`
from a shell). `Info.plist` does not restrict the architecture (`LSMinimumSystemVersion` 10.13 is also lower than the binaries' `minos` 11.0, see `README.md`), so the dmg itself will mount and the app icon will look normal; the failure
comes at launch. Anything user-facing (download page, README) should therefore say "Apple silicon (M1 or later)" explicitly.

## Questions this plan answers

1. Is an x86_64 (or universal) build technically feasible from the current sources?
2. Does it pass the same acceptance (`verify_dmg.sh`, `headless_smoke.sh`, `MANUAL-CHECKLIST.md`) on a real Intel Mac?
3. Is Intel worth supporting at all (an owner decision, below)?

## Phases (each ends with a go/no-go for the next)

**Phase 0: decision input (no work).** Intel Macs are a shrinking installed base, and Apple has said that macOS 26 is the last release that supports them (check Apple's current
documentation; not verified here). Owner decides whether any Intel support is wanted for R0/R1.

**Phase 1: static inventory (cheap, can be done on Apple silicon).**
* Every native component of the sidecar: Python wheels with compiled code (duckdb, cryptography, pydantic-core, ...) and whether an x86_64 or universal2 macOS wheel exists at the **pinned versions** (`packaging/locks/`, `packaging/PINS.md`);
* the Rust target for the launcher (`x86_64-apple-darwin`) and whether `tauri build --target x86_64-apple-darwin` is available in the isolated toolchain;
* PyInstaller 6.22.3 `--target-arch` options: a `universal2` sidecar needs **every** binary dependency to be universal2, which is often not true; separate per-architecture dmgs are the likelier outcome.
Deliverable: a table (component, pinned version, arm64 wheel, x86_64 wheel, universal2 wheel). Record UNVERIFIED for anything not checked against the index.

**Phase 2: build an x86_64 artifact (needs an environment decision).**
* Option A: a native Intel build host or CI runner (availability of Intel runners is UNVERIFIED).
* Option B: on Apple silicon under Rosetta, with an x86_64 CPython 3.12.10 and `arch -x86_64` for the build. **Installing Rosetta is a system change** (`softwareupdate --install-rosetta`): needs the owner's authorization and is not done by the kit. This Mac does not have Rosetta installed (`collect_evidence.sh` records it).
Deliverable: an unsigned x86_64 dmg with the same Licenses/ and layout; `verify_dmg.sh` would need an `--arch` option (today `A-01` fails any non-arm64 file on purpose).

**Phase 3: test.**
* Under Rosetta on Apple silicon (cheap): `headless_smoke.sh` equivalent for the x86_64 sidecar (`arch -x86_64 <sidecar> serve`, same API checks), and the lifecycle checks (`packaging/check-sidecar-lifecycle.py`). This shows the **code** works as x86_64; it does **not** prove behaviour on Intel hardware (no real Gatekeeper/WebKit/ABI differences, performance differs).
* On a real Intel Mac (the only valid evidence for an Intel claim): the whole `MANUAL-CHECKLIST.md` on a fresh standard account, on the oldest and the newest macOS version intended, signed and notarized build (separate signing run per architecture, or one universal dmg).
Deliverable: transcripts and screenshots as for arm64, with the Intel model identifier and macOS build in the header (`collect_evidence.sh` prints both).

**Phase 4: release wording.** Only after phase 3 on real hardware. Until then every page says "Apple silicon (arm64)".

## What can be checked today without Intel hardware (STATIC)

* `verify_dmg.sh` `A-01`: arm64 only, on every Mach-O of the candidate. A universal or x86_64 file is reported (`WARN`/`FAIL`), so an accidental change of architecture is caught.
* A grep of the docs, the website sources and the release notes for "Intel", "x86_64", "universal" and "Rosetta" to make sure nothing claims Intel support (not done by this kit; owner/web owner task).

## Owner decisions

1. Is Intel support wanted at all, and for which macOS versions?
2. If yes: authorize Rosetta installation on a test Mac, or provide an Intel Mac / runner.
3. Wording for the download page ("Apple silicon only") for the arm64 candidate.
