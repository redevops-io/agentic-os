# packaging/notices: licence and notice files for the shipped artifacts

What this folder does, and how its output reaches each artifact. It collects texts and reports what could
not be collected. It does not say that anything is cleared; open items are in
[../DECISIONS-REQUIRED.md](../DECISIONS-REQUIRED.md). The product licence statement is
[../../LICENSING.md](../../LICENSING.md).

| File | Role |
|---|---|
| `generate_notices.py` | CycloneDX SBOM in, `THIRD_PARTY_NOTICES.txt` + `NOTICES_MISSING.{md,json}` + `notices-report.json` out |
| `interim_sbom.py` | stand-in SBOM generator (lock + `cargo metadata` + frozen-archive module list + `components.json`); replace by the real SBOM when the SBOM generator under `packaging/` lands |
| `components.json` | knowledge no package metadata carries: texts for CPython, OpenSSL, PyInstaller, React, certifi (MPL-2.0), plus the items with no text (each with a reason and a `D-LIC-n` id) |
| `texts/` | reference licence texts fetched from upstream, with `MANIFEST.json` (source URL, sha256) |
| `generate_about.py`, `check_about.py` | the About record `about.json` (schema `redevops.about/1`) with the legal-slot machinery, and its closing check; see [../../docs/ABOUT_LEGAL_UI.md](../../docs/ABOUT_LEGAL_UI.md) |
| `stage_licenses.py` | copies `LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`, `LICENSING.md` and the generated files into one `Licenses` folder with `LICENSES-MANIFEST.json` |
| `build-licenses.sh` | the three steps above in one command |
| `check_artifact_manifest.py` | fails if an unpacked artifact (or a path listing) lacks `Licenses/<file>`; verifies hashes against the manifest |
| `check_licence_statements.py` | fails if a product-licence field disagrees with `LICENSING.md` |

## Interface with the SBOM generator

`generate_notices.py --sbom FILE` reads a CycloneDX JSON (1.4 to 1.6), only `components[]` (nested too):
`name`, `version`, `purl`, `licenses[]`, `properties[]`. The purl type picks the lookup: `pkg:pypi` reads the
`*.dist-info` licence files of the build venv (`--site-packages`), `pkg:cargo` reads the crate's source
directory (`--crates-dir`: `$CARGO_HOME/registry/src/<index>` after `cargo fetch --locked`, or `cargo vendor`
output), anything else is looked up in `components.json`. A component with no text is never dropped: it is
reported with a reason and a decision id, and `--fail-on-missing` turns that into a non-zero exit (the release
gate; not enabled for the private candidate because D-LIC-2, -6, -13 are open).

## Delivery per artifact

All artifacts carry the same `Licenses` folder (`LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`,
`LICENSING.md`, `THIRD_PARTY_NOTICES.txt`, `NOTICES_MISSING.md`, `NOTICES_MISSING.json`, `LICENSES-MANIFEST.json`).

| Artifact | How | Evidence |
|---|---|---|
| macOS `.app` (inside the `.dmg`) | `tauri.conf.json` `bundle.resources`: `{"licenses/": "Licenses/"}` puts the folder in `Contents/Resources/Licenses`; `bundle.copyright` fills `NSHumanReadableCopyright` | OBSERVED: `tauri build --bundles app` of the launcher in a scratch copy (tauri-cli 2.11.4, rustc 1.98.1) produced the folder and the Info.plist key; `check_artifact_manifest.py --dir` passes on it |
| macOS `.dmg` root | not done. `bundle.licenseFile` would attach an acceptance EULA to the dmg (D-LIC-10); a copy at the dmg root needs a post-bundle step (before signing) | UNVERIFIED (the dmg step drives Finder via AppleScript and was not run) |
| Windows MSI and NSIS | the same `bundle.resources` entry installs `Licenses\` next to the executables. Tauri 2 has no per-installer licence key; `bundle.licenseFile` (acceptance page, D-LIC-10) is not set | STATIC: tauri-bundler 2.10.4 source; no Windows host here, nothing built |
| sidecar (PyInstaller) | `packaging/redevops-sidecar.spec` embeds the folder as `Licenses` when `REDEVOPS_LICENSES_DIR` is set (build fails if required files are missing) | STATIC |
| `agentic_os` wheel | `license-files` in `pyproject.toml` puts the four product files into `dist-info/licenses` | OBSERVED: wheel built, METADATA `License-Expression` and `License-File` checked |

`tauri-build` fails the launcher build when `deploy/launcher/src-tauri/licenses/` is missing ("resource path
`licenses` doesn't exist", OBSERVED), so an artifact cannot be built without the folder. Run
`packaging/notices/build-licenses.sh` first (it writes that folder; it is git-ignored).

## Wiring needed in CI (not edited here: `.github/workflows/native-packaging.yml` is another owner's file)

Before `cargo tauri build`, on each OS:

```
cargo fetch --locked --manifest-path deploy/launcher/src-tauri/Cargo.toml
cargo metadata --format-version 1 --locked --filter-platform <target-triple> \
    --manifest-path deploy/launcher/src-tauri/Cargo.toml > cargo-metadata.json
packaging/notices/build-licenses.sh --platform <macos|windows> --site-packages <freeze venv>/lib/python3.12/site-packages \
    --crates-dir "$CARGO_HOME"/registry/src/index.crates.io-* --cargo-metadata cargo-metadata.json \
    --exclude knowledge-frontier
```

After the bundle step: `python packaging/notices/check_artifact_manifest.py --dir "<Agentic Apps R0-prime.app>"` (macOS) or
`--listing <paths of the installed/extracted tree>` (Windows).
