# Releasing agentic-os

This is the checklist for cutting a release. It exists because three things drifted apart:

| Where | State when this was written |
|---|---|
| `pyproject.toml` `version` | `0.2.4` |
| Latest git tag | `v0.3.27` |
| GitHub "Latest release" object | `v0.3.7` (stale) |

Git tags are pushed more often than GitHub Release objects are created, and `pyproject.toml` was not bumped
alongside them. Downstream repos pin `agentic-os` by git tag (`@v0.3.x`), so the tag is the source of truth;
the package metadata and the GitHub Release should follow it at release time.

Re-verify the drift before you start:

```bash
grep -m1 '^version' pyproject.toml
git fetch --tags && git tag --sort=-v:refname | head -3
gh release list --repo redevops-io/agentic-os --limit 5
```

## The release script

**Cut and verify kernel releases with `scripts/cut_release.py`**, run with the kernel's Python environment (it also needs
`git`, `gh`, `node` and `npm`). Do not run the publishing commands by hand: v0.4.24 was published with a browser archive
that did not load, because a failing check was piped into `tail`.

```bash
python3 scripts/cut_release.py check   vX.Y.Z                    # origin/main is this version and every run on it passed
python3 scripts/cut_release.py publish vX.Y.Z --notes NOTES.md   # build, probe, tag, DRAFT, re-download, check, publish, images
python3 scripts/cut_release.py verify  vX.Y.Z                    # the published assets: run before deploying from a release
```

`publish` makes the release public only after the assets downloaded back from the draft match their checksum, hold what
the tagged source builds and run in WebAssembly Python. It then builds and signs the images and waits for success. If the
image run fails after publication, fix the cause and run `images vX.Y.Z` while `main` is still the tagged commit.
The checklist below covers preparing the version (steps 1 to 3); the script replaces steps 4 to 6.

## Checklist

1. Start from an up-to-date `main` with a green `tests` workflow.
   Run the `integration-checks` workflow on `main` as well (`gh workflow run integration-checks.yml --ref main`) and wait for it:
   it runs the Support, Billing and Outreach apps against real, throwaway Chatwoot, Lago and Twenty instances. A failure there is a
   release blocker for the app it names; the unit tests use stand-ins and cannot show it.
2. Pick the next version `X.Y.Z` (the tag will be `vX.Y.Z`; it must be greater than the latest existing tag).
3. In a release PR, set `version = "X.Y.Z"` in `pyproject.toml` so it equals the tag you are about to create.
   In the same PR set `ARG KERNEL_REF=vX.Y.Z` in every `apps/*/Dockerfile` and the pin in
   `apps/infra/requirements.txt` (`tests/test_app_dockerfiles.py` enforces they equal the pyproject version).
   Merge it.
4. Tag the merge commit and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
   Do not use an `apps-v*` tag for this: that prefix triggers the desktop-launcher build
   (`.github/workflows/native-packaging.yml`).
5. Create the GitHub Release for the tag so "Latest release" is not stale:
   ```bash
   gh release create vX.Y.Z --repo redevops-io/agentic-os \
     --title "vX.Y.Z" --generate-notes --latest --verify-tag
   ```
   Use `--notes-file <file>` instead of `--generate-notes` for hand-written notes.
6. Verify all three agree:
   ```bash
   gh release list --repo redevops-io/agentic-os --limit 1   # shows vX.Y.Z, Latest
   git describe --tags --abbrev=0                            # vX.Y.Z
   grep -m1 '^version' pyproject.toml                        # version = "X.Y.Z"
   ```
7. Bump the `agentic-os` tag pins in downstream repos separately, via their own PRs.

## Desktop releases (branch `release/0.6.0`, tags `r0prime-vX.Y.Z`)

A desktop release page must carry, besides the installers:

- `release-manifest.json`, `release-manifest.json.sig` and `release-signing-key.pub` (the owner signs the manifest);
- one CycloneDX SBOM per platform and architecture shipped: `sbom-macos-arm64.cdx.json`, `sbom-windows-x64.cdx.json`,
  `sbom-linux-amd64.cdx.json`, `sbom-linux-arm64.cdx.json`. Each `native-packaging` leg uploads its SBOM as an artifact of
  that name. Put them under `<evidence>/sbom/` before building the manifest: `scripts/release/verify_signed.py
  release-manifest` (on the release branch) refuses a platform without its SBOM and lists each SBOM's sha256 in the
  manifest, so the one signature covers them.

After publishing, run the check of the published release and wait for it:

```bash
gh workflow run published-artifacts-acceptance.yml --ref main -f release_tag=r0prime-vX.Y.Z
```

**Version identities.** The desktop app's version and its release tag are different numbers: app 0.1.4 is published under
the tag `r0prime-v0.1.5` (app 0.1.1 under `v0.6.1`). Neither can be derived from the other. `deploy/compat/releases.json` is
the machine-readable pairing: for each desktop release its app version, release tag, build commit, signed-manifest hash,
kernel line and Projects UI; for each bundle its kernel, Projects image, UI, redevops-qa and Edge Sentinel versions. Add the
new entry (and mark the previous one `superseded`) in the same PR that bumps a pin: `tests/test_release_compat.py` fails
when the file and `deploy/combined/.env.bundle.example` disagree, and the published-release check fails a tag that has no
entry or whose manifest differs.

Its `supply chain` job (`packaging/acceptance/release_assets_check.py`) fails when the signed manifest, a listed installer
or an SBOM is missing or differs. Do not point the download page at a release until this run is green.

## Known stale state (to be fixed by the next release)

- The GitHub "Latest release" is `v0.3.7`; tags `v0.3.8` through `v0.3.27` have no Release object.
  Creating the next release with `--latest` fixes the badge. Backfilling the intermediate tags is optional
  (`gh release create vA.B.C --verify-tag --generate-notes --latest=false`).
- `pyproject.toml` is at `0.2.4`. Releasing the next version per the checklist above brings it in line.
