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

## Checklist

1. Start from an up-to-date `main` with a green `tests` workflow.
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

## Known stale state (to be fixed by the next release)

- The GitHub "Latest release" is `v0.3.7`; tags `v0.3.8` through `v0.3.27` have no Release object.
  Creating the next release with `--latest` fixes the badge. Backfilling the intermediate tags is optional
  (`gh release create vA.B.C --verify-tag --generate-notes --latest=false`).
- `pyproject.toml` is at `0.2.4`. Releasing the next version per the checklist above brings it in line.
