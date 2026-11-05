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
every decision it lists is DECIDED in `packaging/distribution-gate.json` with its hash-matched written record, and the first
decision listed carries a `slot_texts` entry (see the gate file's `slot_texts_schema`) naming an approved text file under
`docs/legal/approved/` and its sha256, and the file matches.

| Decision state | Private build | Distribution build (`REDEVOPS_DISTRIBUTION_BUILD` anything but empty or `0`, or `--distribution`) |
|---|---|---|
| any listed decision OPEN | placeholder "Legal text pending decision <ids>. This is a private candidate; nothing here is a legal statement or clearance."; the text file is never opened | generation fails, exit 3, no `about.json` written (a stale one is removed); `check-distribution-gate.py --about-slots` exits 1 |
| all DECIDED, text file ok | slot filled with the file's text (rendered as plain text) | same |
| all DECIDED, text missing, unreadable, not matching its sha256, or no `slot_texts` entry | error, exit 2 | error, exit 2 |

No decision is DECIDED in this repository and no approved text file exists.

## Routes and the capability gate

The gate is unchanged. The About paths are returned to it as extra exact public paths (`public_ui_paths(extra=)`): `/about/`,
`/about/app.js`, `/about/app.css`, `/about/about.json`, `/about/files/<slug>`. There is no directory mount and no path parameter; the
bodies are verified at start-up (record valid, listed with its sha256 in `LICENSES-MANIFEST.json`, every named file has the recorded
size and sha256) and held in memory, so traversal is impossible by construction. A record that fails verification gives a fixed error
document (`ABOUT_RECORD_INVALID`, `ABOUT_HASH_MISMATCH`, `ABOUT_MANIFEST_MISSING`) and no file is served; no record at all gives 404
(`ABOUT_NO_RECORD`). The page still answers in both cases. The page loads nothing from outside its origin, makes no `/api/` request,
sets no cookie, never reads `sessionStorage`, and inserts every value and file text as text. The CSP is the gate's, unchanged.

## Contract for the launcher (Tauri commands)

The launcher shows the same record without the sidecar: `about_info()` returns the contents of `<resource_dir>/Licenses/about.json`
as a string (size-capped at 2 MiB, error code strings as above if it is absent or too large), and `about_text(slug)` returns the
bytes of the file for `slug` (a `licence_files[].id` or `notices`) decoded as UTF-8 text, looking the slug up in the record
(the page never passes a path); sizes and sha256 are those of the record.
