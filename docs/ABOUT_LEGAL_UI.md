# About and licences page (RC3 private development)

Status: private candidate development. Every legal sentence on the page is a slot that stays a neutral placeholder until the
decision it depends on is recorded. Nothing here is a legal statement or a clearance, and the page does not say that anything is.
Design record: `ABOUT_LEGAL_UI_DESIGN.md` and `DECISION-DEPENDENCIES.md` (RC3 planning).

## What ships

| Piece | File |
|---|---|
| Build record, schema `redevops.about/1` | `Licenses/about.json` (staged by `packaging/notices/stage_licenses.py`, listed in `LICENSES-MANIFEST.json`, embedded in the sidecar and bundled by the launcher with the rest of the Licenses folder) |
| Generator | `packaging/notices/generate_about.py` (called by `packaging/notices/build-licenses.sh` after `generate_notices.py`) |
| Closing check | `packaging/notices/check_about.py` (called by `packaging/build-all.sh`) |
| Validator (shared, standard library only) | `agentic_os/about_record.py` |
| Public routes | `agentic_os/about_ui.py`, registered through `local_capability_ui.install_ui_bootstrap` |
| Page | `agentic_os/about_assets/about.html`, `app.js`, `app.css` |
| Entry link target | `./about/` (relative; the shell and the launcher's browser page link to it) |

## The record

`about.json` is ASCII, at most 1 MiB (the sidecar refuses more than 2 MiB), canonical (sorted keys, one-space indent). Every field is
named in the generator (allowlist): product identity from `packaging/identity.json`; commit, tree, dirty count, commit time and
describe from git; the Projects UI bundle file hashes; the sha256 of the SBOM the notices came from and of the gate file; the
licence files (name, size, sha256); per third-party component the name, version, declared licence, status, the files found and the
byte offset and length of its section in `THIRD_PARTY_NOTICES.txt`; the five legal slots; `source_access.state`. It contains no
path, host or user name, no wall-clock time (the commit time is used) and no hash of the artifact that carries it. Declared licences
are shown "as declared in each package; not verified".

## Legal slots

`LEGAL.COPYRIGHT_LINE` (D-LIC-1), `LEGAL.LICENCE_STATEMENT` (D-LIC-1, D-LIC-15), `LEGAL.SOURCE_OFFER` (D-LIC-12, pins-03, pins-04),
`LEGAL.NO_WARRANTY` (D-LIC-10), `LEGAL.SECTION13_NOTICE` (D-LIC-5; may be recorded as not applicable). A slot is filled only when
every decision it lists is DECIDED in `packaging/distribution-gate.json` (each with a written record whose sha256 matches the entry) and
the first decision listed carries a `slot_texts` entry (see the gate file's `slot_texts_schema`) naming an approved text file under
`docs/legal/approved/` and its sha256, and the file matches. The record and the text files must be regular files of the repository: a
symbolic link, or a symbolic link anywhere below the repository root on the way to the file, is refused. The decisions that feed a slot
must stay `blocks_distribution: true`.

### What the hash match proves, and what it does not

A DECIDED entry is plain repository data. The sha256 values in it (the record, the slot text) prove **consistency** between the entry
and the files it names: a tripwire against drift and mistakes (a text edited after it was recorded, a path that points nowhere). They
prove **no authenticity and no approval**: any editor of the repository can write a DECIDED entry together with invented files and
matching hashes (the RC3 security review marked all 21 decisions DECIDED with invented one-line files and the unsigned gate passed). The
build record's `gate_file_sha256` and a slot's `text_sha256` are the same kind of value: consistency, not a guarantee.

### Detached signatures, bound meaning and a pinned signer list

A DISTRIBUTION build additionally requires, for every DECIDED decision that blocks distribution on the platform and for every decision a
slot lists, a detached OpenSSH signature `<record>.sig` over the record named by `decision_reference`, and `<file>.sig` over every
approved slot text: an SSHSIG in the namespace `redevops-legal-decision@v1`, verified with `ssh-keygen -Y verify` against
`packaging/legal-allowed-signers` (public keys only). That file ships EMPTY, so until the owner adds a public key every DECIDED entry
fails a distribution build; this is intended. A signature proves which key signed the exact bytes, not that the decision is right.

**The signed record carries the meaning.** A decision record starts with a header, up to the first empty line:

```
redevops-legal-decision: 1
decision: D-LIC-10
option: a
slot: LEGAL.NO_WARRANTY sha256 <64 hex of the approved text>
```

(`slot: <id> not-applicable` for an omitted slot; one `slot:` line per slot the decision authorizes, on its primary decision). The gate
parses these fields from the verified bytes and compares them with the gate entry (id, `chosen_option`, the slot set and each text's hash).
One signed record or text therefore cannot be re-used for another decision, option or slot, and a text signed alone is not enough.

**The signer list is pinned, not authenticated.** `legal_signer_fingerprints` in `packaging/distribution-gate.json` (ships empty) must list
the SHA256 fingerprint of every key in `packaging/legal-allowed-signers` and nothing else, or no signature verifies. BOTH are repository
data: the pairing makes a one-sided change (a key added to one file) fail, but a repository writer can edit both. Before any distribution
build the owner confirms the fingerprints against an independent channel, for example their published key page: a procedure step, not a code
guarantee. No key is chosen or generated by these tools.

**An unverified text is never ordinary legal text.** The About record's slot states are `pending` (a listed decision is OPEN: the neutral
placeholder), `approved-verified` (everything above verifies), `unverified-draft` (DECIDED and the text matches its hash, but a signature,
the signer pin or the record header does not verify: the text carries the label "Unverified draft, not an approved text." inside it) and
`not-applicable` (an omitted slot, signature-verified; an unverified omission stays a visible placeholder). This holds in EVERY build mode,
the About page and the launcher view. Each slot also carries a fixed `tag` ("Pending a decision", "Approved text, signature verified",
"Unverified draft, not an approved text") that the page shows as a visible state tag next to the slot. `text` is always the FINAL display
text: a renderer that ignores `state` and `tag` and shows `text` verbatim still shows the label of a draft.

The checks run in `check-distribution-gate.py` (every distribution build, `--distribution` or `REDEVOPS_DISTRIBUTION_BUILD`),
`generate_about.py` (exit 3), `check_about.py --distribution` and the signing scripts (below). No private key is ever generated or handled by
these tools. A CODEOWNERS draft for `docs/legal/**`, the gate file and the allowed-signers file is `docs/legal/CODEOWNERS.proposed`: it is
inactive until the owner moves it to `.github/CODEOWNERS` and enables code-owner review.

| Decision state | Private build | Distribution build (`REDEVOPS_DISTRIBUTION_BUILD` anything but the empty string or exactly `0`, no trimming, or `--distribution`) |
|---|---|---|
| any listed decision OPEN | placeholder "Legal text pending decision <ids>. This is a private candidate; nothing here is a legal statement or clearance."; the text file is never opened | generation fails, exit 3, no `about.json` written (a stale one is removed); `check-distribution-gate.py --about-slots` exits 1 |
| all DECIDED, text file ok, signatures, record headers and signer pin valid | `approved-verified`: the file's text (rendered as plain text) | same |
| all DECIDED, text file ok, a signature, header or pin does not verify | `unverified-draft` (label inside the text) and a WARNING on stderr | refused: gate exit 1, generator exit 3 |
| all DECIDED, text missing, unreadable, a symlink, not matching its sha256, or no `slot_texts` entry | error, exit 2 | error, exit 2 |
| gate file malformed or unreadable | exit 2 | exit 2 |

No decision is DECIDED in this repository, no approved text file exists and no signer is listed.

### Signing scripts

`packaging/signing/signing-common.sh` `r0p_gate` now passes `--about-slots`; in `--real` mode the macOS script checks the
`Contents/Resources/Licenses/about.json` inside the dmg copy and the Windows container stage checks the `Licenses` folder of the installer
tree (`--licenses-dir`, whose `about.json` sha256 must appear in the `--container-listing`) with `check_about.py` (plus `--distribution`
when `--distribution` is given): the record must be consistent with the repository's gate, identity, versions and manifest, and for a
distribution run be a distribution record without a placeholder slot. These are changes to `packaging/signing/**` made on the RC3
branch only: the signing procedure needs a new independent review round before any RC3 signing.

## Routes and the capability gate

The gate is unchanged. The About paths are returned to it as extra exact public paths (`public_ui_paths(extra=)`): `/about/`,
`/about/app.js`, `/about/app.css`, `/about/about.json`, `/about/files/<slug>`. There is no directory mount and no path parameter; the
bodies are verified at start-up (record valid, listed with its sha256 in `LICENSES-MANIFEST.json`, every named file has the recorded
size and sha256) and held in memory, so traversal is impossible by construction. A record that fails verification gives a fixed error
document (`ABOUT_RECORD_INVALID`, `ABOUT_HASH_MISMATCH`, `ABOUT_MANIFEST_MISSING`) and no file is served; no record at all gives 404
(`ABOUT_NO_RECORD`). The page still answers in both cases. A missing About asset (for example a packaging mistake) does not stop the
service: one ASCII log line is written, the About routes are not registered and `/about/` answers a short plain-text 404 note (the
shell's About link then shows that note instead of a 401); the control plane is unaffected. With `AGENTIC_OS_LOCAL_CAPABILITY=off` (a
hosted deployment without the gate) the same static public routes are registered so the link works. The page loads nothing from outside its origin, makes no `/api/` request,
sets no cookie, never reads `sessionStorage`, and inserts every value and file text as text. The CSP is the gate's, unchanged.

## Contract for the launcher (Tauri commands)

The launcher shows the same record without the sidecar: `about_info()` returns the contents of `<resource_dir>/Licenses/about.json`
as a string (size-capped at 2 MiB, error code strings as above if it is absent or too large), and `about_text(slug)` returns the
bytes of the file for `slug` (a `licence_files[].id` or `notices`) decoded as UTF-8 text, looking the slug up in the record
(the page never passes a path); sizes and sha256 are those of the record.
