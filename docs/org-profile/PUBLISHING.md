# Publishing the organisation profile (owner steps)

Nothing in this folder is live until the steps below are done by an organisation owner. The PR that adds these files does not create the `.github` repository, does not change organisation settings, and does not change any repository description.

Caveat: `README.md` here contains `added by pending PR` markers and links to files that do not exist yet. Publishing before the sibling PRs merge shows links that return 404. Either wait for the sibling PRs, or publish and accept the broken links temporarily. Also confirm the `EVIDENCE_STATUS_SOURCE` line (it cites a private repository) and the maturity labels marked `(to be confirmed)` before publishing.

## 1. Create the repository

```bash
gh repo create redevops-io/.github --public \
  --description "Organisation profile for ReDevOps"
```

## 2. Add `profile/README.md`

```bash
git clone https://github.com/redevops-io/agentic-os.git   # or use your checkout; use the branch/commit with docs/org-profile merged
git clone https://github.com/redevops-io/.github.git org-dot-github
cd org-dot-github
mkdir -p profile
cp ../agentic-os/docs/org-profile/README.md profile/README.md
git add profile/README.md
git commit -m "Add organisation profile README"
git branch -M main && git push -u origin main
```

If the new repository is empty, `git clone` warns about an empty repository; that is expected. Mermaid renders on github.com; if you also keep an SVG, add `architecture.svg` to `profile/` and reference it relatively.

## 3. Verify rendering

Open `https://github.com/redevops-io` in a private window (logged out). Check: the one-sentence proposition is at the top, the Mermaid diagram renders, tables render, and every link opens. To list link targets quickly:

```bash
grep -o '(https://[^)]*)' profile/README.md | tr -d '()' | sort -u | while read u; do printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -L "$u")" "$u"; done
```

## 4. Replace the organisation description

Settings: github.com/organizations/redevops-io/settings/profile, field Description. Current text: `Redevops.io public images`. Proposed:

> Governed execution runtime for AI applications: context planning, discovery, approvals, verification and replay. Licences vary by repository.

(GitHub limits the field to 160 characters; the proposal is under that.) Or via API: `gh api -X PATCH orgs/redevops-io -f description='...'`.

## 5. Remove the pending markers

When each sibling PR merges (STATUS.md, CONTRIBUTING.md):

1. Check the target resolves: `curl -s -o /dev/null -w '%{http_code}\n' https://github.com/redevops-io/<repo>/blob/<branch>/STATUS.md` returns 200.
2. In `README.md`, delete the `added by pending PR` text in the State column (or drop the column) and the pending note in the contributing section.
3. Re-run step 6.

## 6. Updating later

Edit `profile/README.md` in `redevops-io/.github` and push to `main`; GitHub shows the change on the org page immediately. Keep `docs/org-profile/README.md` in agentic-os in sync, or declare the `.github` copy canonical and delete the agentic-os copy. Revisit the repository table whenever a repository changes maturity, licence label or visibility. Counsel decisions on licence labels should be applied to the Licences section first.
