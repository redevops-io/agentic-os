# Source references for Agentic Apps (template)

STATUS: DRAFT TEMPLATE. This file records WHERE the corresponding source of every shipped component
can be obtained, per build. It states no legal conclusion: what a source offer must contain, for how
long it must stay available, and what the Commons Clause means for it are open owner/counsel
questions (see `packaging/DECISIONS-REQUIRED.md`, section `## pins`).

How this file is produced: `python -I packaging/sbom/generate_source_offer.py --sbom packaging/dist/sbom/sbom.cdx.json
--out packaging/dist/SOURCE-OFFER.md` fills the markers below from the SBOM of the built artifacts, from
`packaging/git-pins.json` (first-party tags/commits) and from `packaging/source-offer/third-party-sources.json`
(upstream sdist / crate / tarball URLs and hashes; refresh with `packaging/tools/collect_source_refs.py`).
Nothing is added by hand to the generated tables.

## Product and licence wording

Agentic Apps is distributed under a source-available licence: the family licence, AGPL-3.0-or-later with
the Commons Clause (see the LICENSE files shipped with the product). Third-party components carry their
own DECLARED licences, listed below; the declarations come from package metadata and are not verified.

[OWNER/COUNSEL: the wording of the written offer itself (scope, duration, medium, contact) goes here.]

## Build identity

<!-- GENERATED:build-inputs -->

## 1. First-party code (corresponding source)

<!-- GENERATED:first-party -->

## 2. Third-party components shipped inside the sidecar and the launcher

<!-- GENERATED:third-party -->

## 3. Shipped components for which no corresponding source can be named

<!-- GENERATED:no-source -->

## 4. Rebuilding from the pinned inputs

1. `git clone <repository above> && git checkout <commit above>`
2. Toolchain: the versions in `packaging/toolchain.env` (CPython 3.12.10, PyInstaller 6.22.3, Rust 1.98.1, tauri-cli 2.11.4, uv).
3. `./packaging/build-wheelhouse.sh && ./packaging/build-sidecar.sh && ./packaging/stage-sidecar.sh && ./packaging/build-launcher.sh`
   (Python wheels are installed with `--require-hashes`, the launcher with `cargo --locked`).
4. `./packaging/build-sbom.sh` regenerates and verifies the component list; `./packaging/verify-reproducible.sh`
   reports which outputs are byte-identical across two clean builds.

## 5. What this file does not say

- It does not say the licences are cleared or compatible; they are declared values.
- It does not cover components that cannot be enumerated from the shipped binaries (see the SBOM `redevops:limitation:*` entries).
- It does not establish that the referenced repositories stay public or that a reference remains reachable.
