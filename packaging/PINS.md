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
