# Pins of the Agentic Apps native build

What is pinned, where, why, and how to regenerate it. Purpose and implementation only; no claim beyond
what a check in this repository shows. Evidence levels: OBSERVED / STATIC / INFERRED / UNVERIFIED.

## What a rebuild is made of

| Input | Pin | Where | Enforced by |
|---|---|---|---|
| CPython | exactly 3.12.10 | `toolchain.env` `PYTHON_VERSION` | `common.sh assert_python` (fails on any other interpreter); CI `actions/setup-python` |
| Third-party Python wheels (21) | exact versions + sha256 of every published file, per OS | `locks/requirements-{macos,windows,linux}.lock` | `pip download/install --require-hashes` |
| First-party Python (4) | git tag object AND peeled commit | `git-pins.json`; `pyproject.toml` pins the tag (pip's VCS syntax takes one ref) | `tools/build_git_wheels.py` clones the tag and fails unless tag object and commit match, removes committed build dirs, builds with the hash-locked backend, compares every built `.py` with `git show <commit>:<path>` |
| Build backends | pip 26.2.1, setuptools 84.0.0, wheel 0.48.0, hatchling 1.32.3 (+ its deps) | `locks/build-backends-<os>.lock`, `locks/pip.lock` | `uv pip install --require-hashes`, then `--no-build-isolation` (no backend is fetched at build time) |
| PyInstaller | 6.22.3, pyinstaller-hooks-contrib 2026.7, altgraph 0.17.5, packaging 26.3, setuptools 84.0.0 (+ macholib 1.16.4 on macOS; pefile 2024.8.26 + pywin32-ctypes 0.2.3 on Windows) | `locks/build-tools-<os>.lock` | `--require-hashes`; `build-sidecar.sh` asserts `pyinstaller --version` |
| Rust | 1.98.1 | `toolchain.env`, `deploy/launcher/rust-toolchain.toml` | `build-launcher.sh` asserts `rustc`; CI installs exactly it |
| tauri-cli | 2.11.4 | `toolchain.env` | `build-launcher.sh` asserts the version; CI `cargo install tauri-cli --version 2.11.4 --locked` |
| Rust crates | 427 rows | `deploy/launcher/src-tauri/Cargo.lock` | `cargo tauri build -- --locked` |
| uv | 0.12.16 (the frozen run used 0.12.16) | `toolchain.env`, CI `setup-uv` | CI; recorded in the build manifest |
| Node | none: no Node step exists (UI is vendored, launcher page is static) | `toolchain.env` | - |
| GitHub Actions | commit SHAs | `.github/workflows/native-packaging.yml` | `tests/test_packaging_pins.py` |
| Runner images | `macos-14`, `windows-2022`, `ubuntu-24.04` (by NAME) | workflow | the image VERSION still floats: recorded in `build-manifest.json` (`runner.env.ImageVersion`) |

`verify-pins.py` checks pyproject <-> git-pins <-> requirements.in <-> locks <-> toolchain.env <-> the frozen CI wheel list <-> Cargo.lock.
`tests/test_packaging_pins.py` (offline) asserts the same and that the scripts and workflow use the pins.

## certifi: a DELIBERATE deviation from the frozen v0.3.2 wheel set (decision pins-16)

The 21 third-party wheels equal the frozen CI wheel set **except certifi**: frozen 2026.7.22, now **2026.10.19**.
Why: GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320 (2026-10-20): certifi before 2026.10.18 bundles the `emSign ECC Root CA - G3` root, and
eight eMudhra intermediate CAs chaining to it have compromised keys; the sidecar's outbound TLS (httpx and the connectors) trusts the certifi
bundle. The advisory says: upgrade to 2026.10.19 or later (2026.10.18 removed all emSign roots; 2026.10.19 restored the unaffected
ones; the ECC G3 root is gone in both). The locks of 2026-09-18 predate the advisory.
How: `toolchain.env` `LOCK_EXCLUDE_NEWER_CERTIFI=2026-10-21T00:00:00Z` (certifi only; every other package keeps `LOCK_EXCLUDE_NEWER`
2026-09-18T14:00Z) selects the newest certifi published at least about two weeks before 2026-11-04 (2026.10.19, uploaded 2026-10-20;
2026.10.23 was too new), and `lock-constraints.in` (`certifi>=2026.10.18`) makes the resolver fail instead of falling back below the fixing
release. `regen-python-locks.sh` passes both (`--exclude-newer-package`, `-c`). The regeneration changed exactly certifi (version, two
hashes, the `via -c` comment) in the three `requirements-<os>.lock` files and the universal `requirements.lock`; the build-tools and
build-backends locks changed in a header comment only.
Enforcement: `verify-pins.py` asserts "the 21 pins equal the frozen CI wheels EXCEPT the documented deviation set {certifi}" and pins each deviation to
its recorded version (>= its fixing release); a drift of any other package, or of certifi to another version, fails
(`tests/test_packaging_pins.py::test_the_deviation_set_is_exactly_certifi_and_drift_elsewhere_still_fails`). The frozen evidence list is unchanged.
Not changed: the first-party pins (runtime-contracts v0.3.4, redevops-connectors v0.1.0, knowledge-frontier v0.1.0); `uv.lock` at the repository
root is a stale developer lock that no build reads (regenerating it rewrites it wholesale) and still names certifi 2026.7.22.
OSV check (read-only API, 2026-11-04) of all 21 pinned macOS wheels with certifi 2026.10.19: no advisory for any of them.
Consequence for the reproducibility claim: this build is NOT a byte-for-byte rebuild of the frozen v0.3.2 wheel set; the shipped `cacert.pem`
differs (and the notices/SBOM name certifi 2026.10.19).
Compatibility check (owner decision 6, 2026-11-04): `packaging/tools/certifi_chain_compat.py` compared the frozen bundle (2026.7.22) with the shipped one
(2026.10.19, the bytes extracted from the candidate sidecar) over 3,665 hosts (every product and documented host, 1,994 top-ranked, 1,498 `.in`, emSign and
major-CA candidates): no host validates with the old bundle and fails with the new one; the only difference between the bundles is `emSign ECC Root CA - G3`, and no
tested chain ends in it. Result, host table and limits: `packaging/evidence/certifi-chain-compat-2026-11-04.md`. certifi 2026.10.19 is retained.

## idna 3.20 / uvicorn 0.53.0 versus the old lock (3.19 / 0.52.4)

The committed `packaging/requirements.lock` was the `pip freeze` OUTPUT of `build-wheelhouse.sh`, rewritten in the CI
workspace on every build and never the input. It lagged what was built: the frozen CI run (35353188692, 2026-09-18) installed
idna 3.20 and uvicorn 0.53.0 (CI logs; confirmed inside the shipped archive's PYZ by the audit). The new locks are inputs,
generated with `uv pip compile --generate-hashes --exclude-newer 2026-09-18T14:00:00Z`; that cut-off makes the resolver
reproduce the 21 third-party wheels of ALL THREE CI legs exactly (macOS, Windows and Linux lists in
`evidence/frozen-ci-wheels.json`; `verify-pins.py` compares them). **Chosen: idna 3.20, uvicorn 0.53.0** (what the frozen run built; no reason to move). knowledge-frontier 0.1.0 (absent from the old lock) is built from its git pin.
PyPI check of 2026-11-04: all 63 wheel files of the three legs still exist and none is yanked or removed.

## runtime-contracts v0.3.4 and the stale `build/lib`

The tag commits a `build/lib/` tree (15 files of older code). setuptools skips copying a source file when the build output is
not older, so a build from a tarball/zip (equal mtimes) packages the OLD files. Reproduced here (OBSERVED): a `git archive`
copy with equal mtimes gave a wheel with 5 of 47 `.py` files stale (`runtime_contracts/__init__.py`, `models/__init__.py`,
`capability.py`, `intent.py`, `investigation.py`); a fresh `git clone` happened to be correct (the frozen CI build was correct
by that luck). `build_git_wheels.py` does not depend on mtimes: it deletes `build/` and `*.egg-info` in the verified clone and
then checks the wheel against the commit. `tests/test_packaging_pins.py` covers the detector. The pin itself (v0.3.4) is unchanged.

## Interpreter identity (honest statement)

The frozen CI legs did not use `actions/setup-python` and not uv's python-build-standalone: macOS ran the runner image's
python.org framework (`/usr/local/bin/python3.12`, 3.12.10), Windows the image's `C:\hostedtoolcache\windows\Python\3.12.10`
(Linux: the system `/usr/bin/python3.12`, 3.12.3). The pinned workflow installs exactly 3.12.10 with `actions/setup-python` and
`UV_PYTHON_PREFERENCE=only-system`. Local check builds (this Mac) use uv's python-build-standalone 3.12.10. The manifest records
the interpreter path, build string and executable sha256 of whichever was used. Whether `actions/setup-python` on macos-14
installs the same binary as the frozen `/usr/local/bin/python3.12` is UNVERIFIED (decision pins-06).

## Build manifest

`packaging/dist/build-manifest.json` (written by `build-wheelhouse.sh`, `build-sidecar.sh`, `build-launcher.sh`): schema
`redevops.build-manifest/1`: git commit/tree/dirty-file count; sha256 of every pin, lock, script and tool that steers the
build; interpreter identity + build venv distribution versions; rustc/cargo/tauri-cli/uv versions; runner platform and the
whitelisted `ImageOS`/`ImageVersion`/`GITHUB_*` variables (never the environment); the 25 wheels with sha256; the
first-party clones (tag object, commit, SOURCE_DATE_EPOCH, removed build dirs); artifact hashes; stage durations.

## Regenerating

| Need | Command | Network |
|---|---|---|
| Python locks (per-OS + `packaging/requirements.lock`, the universal lock the notices tooling reads) | `packaging/regen-python-locks.sh` | PyPI |
| Cargo.lock (after any Cargo.toml change, e.g. a new Windows dependency) | `packaging/regen-cargo-lock.sh` | crates.io |
| Third-party source references | `python -I packaging/tools/collect_source_refs.py --sbom packaging/dist/sbom/sbom.cdx.json --out packaging/source-offer/third-party-sources.json --existing packaging/source-offer/third-party-sources.json` | PyPI, npm, python.org, GitHub |
| Check | `python -I packaging/verify-pins.py`; `python -m pytest tests/test_packaging_pins.py tests/test_packaging_sbom.py` | none |

`regen-cargo-lock.sh` keeps every already-pinned row and resolves only new crates to "latest compatible today"; it prints what
changed so a reviewer can see the delta. The 343 rows recovered from the CI logs are in `cargo-targets/ci-locked-rows-recovered.txt`.

## SBOM and source references

`build-sbom.sh` builds the CycloneDX 1.5 SBOM from the BUILT sidecar archive (PKG entries and PYZ modules attributed to
distributions via wheel metadata and the stdlib list), reads OpenSSL version banners from the shipped binaries (including the
second OpenSSL statically linked into `cryptography`), Microsoft DLL version resources, UI bundle banners (React 18.3.1), the certifi
bundle, the wheels' own embedded SBOMs (cryptography/pydantic_core Rust crates, upstream-declared) and the launcher crate graph
from Cargo.lock. Licences are DECLARED values; what cannot be enumerated (DuckDB, libyaml, libffi, CPython extension contents)
is an explicit `redevops:limitation:*` entry. `verify_sbom.py` re-derives the expected components from the archive and fails on
any unlisted or mismatched one; run against the frozen v0.3.2 listings it reproduces the audit's gaps (see the pins report).
`generate_source_offer.py` + `SOURCE-OFFER.md` record where the corresponding source of each component can be obtained.

## Reproducibility (what the check shows, nothing more)

`verify-reproducible.sh` builds the same committed tree twice in clean clones (fresh wheelhouse, fresh venvs, empty
`CARGO_TARGET_DIR`) and compares; `build-all.sh` is the sequence it builds. Results on one Mac (arm64, uv's python-build-standalone
3.12.10, Rust 1.98.1, tauri-cli 2.11.4), release build of the launcher `.app` only (no dmg):

| run (OBSERVED, 2026-11-04) | wheelhouse | sidecar | launcher | app tree | SBOM |
|---|---|---|---|---|---|
| same checkout path, same CARGO_HOME | 25/25 wheels identical | whole file identical (63 PKG entries, 1088 PYZ modules, all identical) | identical | 12/12 files identical | byte-identical |
| different checkout paths + different CARGO_HOME (equal-length), launcher WITHOUT `--remap-path-prefix` | identical | identical | DIFFERENT: 7064 bytes: the embedded CARGO_HOME path (324 strings) + LC_UUID (16) + ad-hoc code signature (892); after replacing the path the remaining differences are LC_UUID and the signature only | `agentic-apps` differs | differs (it records the launcher's SHA-256) |
| same, WITH `--remap-path-prefix` (the default in `build-launcher.sh`) | identical | identical | identical | 12/12 identical | byte-identical |

Difference classes found: (1) build paths embedded by rustc (CARGO_HOME registry paths, checkout dir) -> removed by remapping;
(2) Mach-O LC_UUID and the ad-hoc linker signature, which follow any content change. PyInstaller output was identical with
`SOURCE_DATE_EPOCH` (commit time) and `PYTHONHASHSEED=1` set; first-party wheels were identical with `SOURCE_DATE_EPOCH`.
Durations: wheelhouse 7-9 s, sidecar freeze 15-16 s (twice per build: notices pass), launcher 21-23 s (release, crates already
downloaded; first fetch adds the download), whole build 68-69 s.

Not shown, therefore not claimed: identity across hosts, runner image versions, interpreters (python.org framework / hostedtoolcache
vs standalone: the PKG layer differs, decision pins-06), Rust/Xcode SDK versions, the dmg (not run: it drives Finder), Windows (the path
remap is POSIX-only; UNVERIFIED), or code-signed output (signing changes bytes by design). The frozen v0.3.2 launcher embeds
`/Users/runner/.cargo/...`; a CI rebuild with the remap embeds `/cargo-home/...` instead. No claim of reproducible releases is made.

## First-party pins on the converged trunk (0.6.0-rc1 line, 2026-11-06)

The trunk is built from `origin/main`, so the first-party pins are main's, not the RC line's: runtime-contracts **v0.3.14** (tag object
`73ff21c2`, commit `80e727d8`; the first tag without the stale committed `build/lib`, so `committed_build_dirs` is empty), discovery-runtime
**v0.1.19** (new first-party wheel; tag object `5c25b79d`, commit `b49f66c6`; its only dependency is runtime-contracts) and knowledge-frontier
**v0.2.0** (tag object `189029f4`, commit `057258ae`). redevops-connectors stays v0.1.0. The tag objects and commits were read with
`git ls-remote` on 2026-11-06 and agree with the commits recorded by pip for the installed test environment. No new third-party wheel is
introduced (discovery-runtime, runtime-contracts (pyyaml) and knowledge-frontier add none beyond what the 21 locked wheels cover).
**Not done here:** the wheelhouse build (`build_git_wheels.py`) and the SBOM/notices regeneration for these three pins have not been
re-run on the trunk; the licence status of discovery-runtime and knowledge-frontier v0.2.0 is not decided (D-LIC items stay OPEN). The earlier
sections above describe the RC line's pins and are historical for the first-party rows.

## The Projects UI: a pinned artifact, built locally, unpublished (converged trunk, 2026-11-06)

`agentic_os/projects_ui/` is a build OUTPUT (git-ignored; only the neutral `hero.jpg` is tracked). It is produced from ONE pinned artifact:
`packaging/projects_ui.lock.json` = sha256 + source commit + where to read it. `scripts/fetch_projects_ui.py` verifies the sha256 BEFORE
anything is extracted (and rejects any member that would leave the destination); the wheel build hook (`hatch_build.py`) runs it, so
`pip install -e .`, `pip wheel .` (wheelhouse, sidecar) and `docker build -f Dockerfile.projects .` all get the same bytes, offline.

| Field | Value |
|---|---|
| artifact | `packaging/ui-artifacts/projects-ui-root-ea9e53e5.tgz`, 162,769 bytes, sha256 `b202abc5...7b73` (checked into the repository; `local_path`) |
| `url` | empty: no release of `redevops-projects` has been published for it. A later publication fills it in; nothing else changes |
| source commit | `ea9e53e56b4cbaa1afb22404a6468695e49c24fa` (tree `7eb444ff...`; branch `ui/source-errors` of a LOCAL clone, from `ui/focus-fix` `9a1788b1`, from `ui/event-summaries` `5751e9e5`, from `ui/partly-sample` `312a811a`, from `ui/runtime-mode` `2f24719e`, from `ui/evidence-tracing` `ee958a01`; `source_commit_published: false`), public base `5114b560...`, and it merges the public release `v0.2.0` (`84f17fce...`) |
| contents | `index.html`, `assets/index-76LJRJ18.js` (400,961 bytes), `assets/index-BzggwPVV.css` (build output) + the neutral `hero.jpg` of this repository |
| recipe | clean `git archive` of the source commit; `env -i`, node v24.22.2, npm 11.19.1, `npm ci`, `VITE_PROJECTS_API=/ npm run build -- --base=/`; pack with `scripts/make_projects_ui_artifact.py` (sorted members, fixed mtime, gzip mtime 0) |
| reproduced | three builds, each in its own throwaway HOME, TMPDIR and npm cache, gave identical `index.html`, JavaScript and CSS; three consecutive packs are byte-identical. The archive bytes depend on the zlib in use, so the portable invariant is the per-member hash list (`bundle_files`); the archive hash is what is pinned |
| `previous_pin` | chain: `local+9a1788b1` (`packaging/ui-artifacts/projects-ui-root-9a1788b1.tgz`, sha256 `a463cfec...37cd`: before connecting, proposing and removing a source said anything when refused), then `local+5751e9e5` (`packaging/ui-artifacts/projects-ui-root-5751e9e5.tgz`, sha256 `eccf1596...5a31`: before the focus fix), then `local+312a811a` (`packaging/ui-artifacts/projects-ui-root-312a811a.tgz`, sha256 `5639c828...1005`: the same views without the flags), then `local+2f24719e` (`packaging/ui-artifacts/projects-ui-root-2f24719e.tgz`, sha256 `ff5224fa...fd341`: the same views without the Partly sample chip), then `local+ee958a01` (`packaging/ui-artifacts/projects-ui-root-ee958a01.tgz`, sha256 `cc2abd7c...6769a`: the same views with the Sample chip taken from a build flag), then `local+2fdef7f5` (`packaging/ui-artifacts/projects-ui-root-2fdef7f5.tgz`, sha256 `a5b5ef13...3654`), then the public `v0.2.0` asset (sha256 `a84b7ce4...`). Every local artifact is kept in the repository. Going back is a lock edit |

This is NOT a source offer and not a publication: `packaging/ui-bundle-source.json` keeps the provenance (decisions pins-04, pins-10 and
D-LIC-12 stay OPEN, the distribution gate is unchanged and still refuses). **What the artifact is:** the Today-first views of `v0.2.0`
(Today, quick capture, batch triage, notifications, routing, deployment-inspection and social-intelligence cards) together with the accessibility
series (UIA-01..12), the application-directory tab, and the mission drill-down (whose data source, Sample chip included, is what the RUNNING SERVER reports at `GET /api/ui-config`: there is no build flag, one artifact serves a sample and a runtime-backed deployment; a runtime server whose provider-backed pages are still sample gets a "Partly sample" chip with a keyboard-reachable explanation) (Summary, Tasks, Evidence with Inspect, Timeline, Graph, World State,
EXPLAIN, Replay; read-only). **What it shows on the desktop:** the sidecar serves the sample provider, which has no drill-down routes; the
drill-down views therefore show the sample mission detail labelled as sample and say that no recorded history is available. Recorded data appears only
when a server that mounts the drill-down router (`agentic_os/drilldown`, not yet mounted by `projects_api.create_app`) answers. The npm lock file
differs from the base by one line (the package `license` field); no dependency was added.
Tests:
`tests/test_projects_ui_pin.py` (12), `tests/test_fetch_projects_ui.py`, `tests/test_ui_bundle_source_record.py`.

## Build fixes after the 0.6.0-rc1 native qualification

* **The first-party wheel list has one source.** `packaging/git-pins.json` (plus `agentic-os`, built from the checkout) defines it; `packaging/tools/first_party.py` derives it. `build-wheelhouse.sh`, `build-sidecar.sh` and `build-notices.sh` call `first_party_wheels` (`common.sh`) instead of a hand-written glob, and the SBOM, source-offer, source-refs and build-comparison tools read the same set. A pin without a wheel, a wheel without a pin or a missing locked wheel stops the build with the package named. (rc1 listed four names while the pins built five, so `pip check` failed in step 4.) Test: `tests/test_first_party_wheels.py`.
* **The About record and the build manifest record the pinned Projects UI.** `generate_about.py` reads the files from the `agentic_os` wheel in `packaging/wheelhouse` (the bytes that ship), compares each with `packaging/projects_ui.lock.json` and fails the build on any difference; `ui_bundle.pin` carries the version, artifact sha256, source commit and the published flag, and `build-manifest.json` has the same under `projects_ui`. Test: `tests/test_about_ui_pin.py`.
* **`site` stays out of the frozen sidecar.** The spec excludes it (`SITE_NOT_FROZEN`); `trusted_import` imports it under `try/except ImportError`. `packaging/probes/` freezes a probe with the sidecar's own spec to show `trusted_import` refusing planted packages inside the PyInstaller runtime. Tests: `tests/test_trusted_import_frozen_safe.py`, opt-in `tests/test_frozen_trusted_import.py`.
* **The sidecar does not depend on where the build interpreter is installed.** `packaging/tools/normalise_sysconfigdata.py` (called from the spec) replaces the interpreter prefix inside `_sysconfigdata__<platform>`. Test: `tests/test_sidecar_sysconfigdata_normalised.py`.
* **A bad `PROJECTS_DRILLDOWN_*` configuration prints one line.** `agentic_os/projects_entry.py` is the entry of `serve` and `agentic-os-projects`. Test: `tests/test_projects_entry_refusal.py`.

## runtime-contracts v0.3.17 and discovery-runtime v0.1.21 (branch `candidate/0.6.0-rc2-contracts-0.3.17`, 2026-11-10)

`pyproject.toml`, `uv.lock`, `git-pins.json` and `.github/workflows/native-core.yml` move runtime-contracts from **v0.3.14** to **v0.3.17**
(tag object `977f5532`, commit `f465e501`, tree `239dae0a`; no committed `build/` or `dist/`; `committed_build_dirs` stays empty) and discovery-runtime
from **v0.1.19** to **v0.1.21** (tag object `043117dc`, commit `3856bca7`; based on v0.1.19, so its licence files are byte-identical to v0.1.19's). discovery-runtime requires its runtime-contracts exactly: v0.1.19 and v0.1.20 require
v0.3.14, so v0.1.21 (which changes only that pin and the version strings of v0.1.19) is what resolves with v0.3.17. The tag
object above is the one published for discovery-runtime v0.1.21 (checked against the remote). knowledge-frontier
v0.2.0, redevops-connectors v0.1.0 and redevops-rag v0.4.0 do not depend on runtime-contracts and keep their pins.

The behaviour that matters at run time arrived in runtime-contracts v0.3.15: a credential request is denied (`TENANT_MISMATCH`, `AUTHORITY_MISMATCH`, `AUTHORITY_EXPIRED`)
unless its tenant equals the authority principal's tenant, its secret namespace equals that tenant, the authority id matches and the authority has not expired.
`agentic_os/secrets/resolve.py::read_secret` states where its tenant comes from (the `tenant` argument, else `REDEVOPS_LOCAL_TENANT`, else the namespace of the
reference for the single-user desktop). Tests: `tests/test_read_secret_tenant.py`, `tests/test_credential_gate_shipped_paths.py`.
The wheelhouse, SBOM and notices are not regenerated here and every native artifact must be rebuilt from this pin.

## 2026-11-11: sibling licence tags (T1 / D-LIC-2)
`redevops-connectors` v0.1.0 -> **v0.1.1** (tag object `04d398445d6169f8ebd9807ea68dfd439afb90cf`, commit `4358493c8c58c75bc7dd2c29bd12df7be5eccb0f`) and `knowledge-frontier` v0.2.0 -> **v0.2.1**
(tag object `d2b52a08f7917dd8d3f18ba9e332d78d7546a339`, commit `ed224d73d4ab65c14f1f205b1a957f6cc8eb0967`). Each is the previously pinned tree plus the licence files only (LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md, the
`license` metadata, a README line, the patch version): no code changed (`git diff v0.1.0 v0.1.1` and `git diff v0.2.0 v0.2.1` list those files). The tags were created in local clones and pushed ONLY to the workspace
mirrors (`xrepo/repos/*.git`); nothing was pushed to GitHub, no published tag was rewritten. The pinned URLs name GitHub, so a build from these pins needs the two tags pushed first (publication step, pins-04).
Licence note: the v0.1.0 and v0.2.0 pyprojects declared plain AGPL-3.0-or-later; the new tags apply the family licence (AGPL-3.0-or-later with the Commons Clause) that the owner stated for D-LIC-2 in the signed release approval.
