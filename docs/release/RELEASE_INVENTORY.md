# Release inventory and qualification feasibility (RC3 desktop freeze e2f2fa9), 2026-11-05

Status: PRIVATE working document on a local branch (`rel/inventory`, from e2f2fa9, unsigned commit, not pushed). It states facts observed in files and
read-only git commands on 2026-11-05; it makes no readiness, compliance or legal claim. Evidence levels: OBSERVED (command run or file read in this
inventory), RECORDED (stated in a named record, not re-run here), STATIC (read from source only), UNVERIFIED (not tested anywhere).
S = `/private/tmp/claude-501/-Users-arybach-agentic-os/30e85532-95c8-480d-aadc-1f976c943ab3/scratchpad`.
Licence wording used throughout: source-available (family licence: AGPL-3.0-or-later with the Commons Clause).

Two provenance lines exist and are kept in two separate sections. Section 1A (desktop freeze) and section 1B (agentic-os-stack integration) never share SHAs.

## 1A. Provenance line A: the RC3 desktop freeze (repo `/Users/arybach/agentic-os`)

### 1A.1 The frozen tip and its lineage

| Item | Value |
|---|---|
| Frozen tip | `e2f2fa9ef59dfc474eb561a87282019a06469b3b`, tree `e088c35786a6bfd975084ee7f5c8c75dd48eb3fa`; local branch `rc3/finalfix` (a worktree in S) (OBSERVED `git rev-parse`) |
| Version string `git describe` | `v0.3.2-176-ge2f2fa9` (OBSERVED). The tag `v0.3.2` is an annotated tag object `aa51321dfdfe1f4d289bfa12a792819282bd83c0` peeling to `feeca065cbd8923267c9d4f28bdfd4f8da905ee4` |
| Merge base with `main` | `feeca065cbd8923267c9d4f28bdfd4f8da905ee4` (= v0.3.2). Since then: 176 commits only on the freeze, 529 only on this clone's `main` (a560a6ee1a8081012540877146bacd9399743df6) (OBSERVED `rev-list --count`). The freeze is NOT contained in `main`; `main` has moved on without it. `origin/main` in this clone is `7b4b11c13c90de905c43637de286df6ac65c6fcd` |
| Last four commits (unsigned, F-1/F-2) | `e14098d74021d74d190a98824aa5880544e36b4f` tests F-1; `8b443a4f5b39a4d947411b44bc55e1cf87d5a722` launcher F-1; `0ae5b0100e4b9b3c3eb0242f79a4677fe4ad835f` tests F-2; `e2f2fa9ef59dfc474eb561a87282019a06469b3b` gate F-2 |
| Parent (rc3/dev, independently reviewed) | `6daf9086712e36e87746b165d914279eb0137384` (merge of `3fa40cfb4697c041f42296a7d829c00767eaf65e` rc3/launcherfix) |
| rc3/dev history | `22a906a4b4f73714ff5215b2ccb92a66b5a2d4c4` (scopefix `450350d5155e386b945595b5414b88c161930bfa`) <- `523ca534f079afcabb2588a39bb003a50b77477b` (devsigner `7eaed29afbf2fc481f7f2f2db52db3196c7b37c6`) <- `5b87c0e08ad48afff5424665283a7d7fd874751f` (rlimit `3726729dd1a5a01a22746e02481eed464d9bff27`) <- `0f5c03df556ffed740b5fba6f2bc3b6a45ab9821` (icons `ed9371fba3becb1c6aa8c8a36aec8662170534fc`) <- `fed7677e1c653f72f2fc476cfa500b671e2cca7c` (ui `b10724cbb20c18eaf10129ebe261cd1f429943d0`, about `351122dbfb61b18e4b15dd3c13f9113de47de51f`, launcher `52b4b57e3bb22450193e01dbd6d39c49d1ba06f0`) <- RC2 |
| RC2 (preserved) | `release/r0-prime-rc2` = `3f2b9c909dde04a0edefdf661d32b51d5be5ab89`, an ancestor of the freeze (OBSERVED `merge-base --is-ancestor`); candidate build commit `da16e7c09b207bff1ec1aa90036ea045db184d38` (shipped sources identical to the tip except `packaging/signing/**`, RECORDED phase-2 report section 1) |
| Existing RC2 evidence | unsigned dmg `Agentic Apps R0-prime_0.1.1_aarch64.dmg` sha256 `6207b9e2eece9962bd97ae258279678e9b9e5ceab93d527a1b1eaa84ff4ced45` (38,809,698 bytes), in `S/r0prime/candidate/macos-arm64-unsigned-rc2-final/` (OBSERVED, hash recomputed); sole eligible signing manifest `S/r0prime/candidate/evidence-macos-arm64-unsigned-rc2-final2/release-manifest.json` sha256 `29151a0ae5574460200981c128ee24e5da68bfddc8206e4cda7e32c3216baddd` (OBSERVED, hash recomputed). RC3 would supersede that manifest (freeze record section 5) |
| Frozen R0 baseline | `cd $S && shasum -a 256 -c r0-audit/baseline/BASELINE_SHA256SUMS`: 144 files OK (OBSERVED, 144 lines "OK") |
| Not part of the freeze | `rc3/nativeval e770750`, `rc3/launcherfix-validation 9e2e6fe`, `rc3/finalfix-validation ee3d727` (throwaway validation branches); agentic-tests PR #44 head `14de186a8312ed8d30d93234b786b5fa2bbaecce` on `hardening/t01-t04-t06` (OBSERVED, matches the freeze record) |
| Commit signing disclosure | 48 of the 53 commits in `3f2b9c90..e2f2fa9` are signed by the owner's GPG key through gpg-agent, 5 unsigned (RECORDED, freeze record section 7); this inventory's own commit is unsigned |

The freeze record states "no release tag was created": no tag points at e2f2fa9 (OBSERVED: `git tag --points-at` is empty for the branch tips listed; the newest tags reachable are v0.3.0, v0.3.1, v0.3.2).

### 1A.2 Pinned inputs of the desktop build (sha256 recomputed at e2f2fa9, OBSERVED)

| Input | Pin / content | sha256 of the file |
|---|---|---|
| `packaging/toolchain.env` | CPython 3.12.10 (line 13), PyInstaller 6.22.3 (16), pyinstaller-hooks-contrib 2026.7 (17), setuptools 84.0.0 (18), pip 26.2.1 (19), uv 0.12.16 (20), `LOCK_EXCLUDE_NEWER` 2026-09-18T14:00:00Z (25), certifi cut-off 2026-10-21T00:00:00Z (31), Rust 1.98.1 (34), tauri-cli 2.11.4 (35), Node none (40) | `f0ce5b5002038c2b6928992dd43c9443e1ce764d6b06199b9331d515734d7700` |
| `packaging/requirements.lock` | universal hash lock of the 21 third-party wheels, uv 0.13.6 header; read by the notices tooling only, the build installs the per-OS locks | `55fce3f016c71fd2ae197aac27bf0618bd551d1a30e5f0dd83f5d448d0e3517a` |
| `packaging/locks/requirements-macos.lock` (aarch64-apple-darwin) | per-OS shipped wheels, `--require-hashes` | `f2e92f9653772afb7ef9edf13272b7be5ad4a962b48ef3d9f4550c739232db7f` |
| `packaging/locks/requirements-linux.lock` (x86_64-unknown-linux-gnu) | same | `8dfc1f2aa49c7c7cac21e6275e6abb8552608d318127a63b294b166f32b566db` |
| `packaging/locks/requirements-windows.lock` (x86_64-pc-windows-msvc) | same | `be12ee4b27f5e0835fa6d7b1dc37364f2ba7132cf0047f2f551b253d45edb943` |
| `packaging/locks/build-tools-{macos,linux,windows}.lock` | PyInstaller and dependencies | `b0457b56...ff167` / `001e51b7...e79d` / `642f8782...5452` (full values in `shasum -a 256 packaging/locks/*.lock`) |
| `packaging/locks/build-backends-{macos,linux,windows}.lock`, `pip.lock` | pip, setuptools, wheel, hatchling | `2b2e8e70...e0ec` / `ea1f0405...e8f1` / `2d67a78a...d92a`; pip.lock `336cfd6e...b579` |
| `packaging/git-pins.json` | runtime-contracts v0.3.4 (tag object `46f6283e6ed4fb289fd375a27bf9839d5514bf33`, commit `a643edc3ba5df9024532ca170afbb6e83c2256c1`); knowledge-frontier v0.1.0 (`5da341eb...`, `d6a78ca3d1474cd06fd84c5f4547fdf381231cfb`; built into the wheelhouse but excluded from the sidecar, D-LIC-14); redevops-connectors v0.1.0 (`f36bdf55...`, `7fbb9ec4d4faff1dea2e238a94495bbc1951b56bc`) | `6ae28228ea41081d756ccabe266baac42735e6f57deccf024da160281b6a4d0d` |
| `deploy/launcher/src-tauri/Cargo.lock` | 427 `[[package]]` rows; read with `cargo ... --locked` | `634f912924798487a199d14ea9f519ef9c0f907b99526687be102df4796b62ab` |
| `deploy/launcher/rust-toolchain.toml` | channel 1.98.1, profile minimal | (in tree) |
| `uv.lock` (repo root) | stale developer lock; no build reads it; still names certifi 2026.7.22 (PINS.md says so) | `ba7a01745637dd2049fcae99d4d7a1fa8eb2b53d3452f900b2180c2029af6352` |
| `packaging/identity.json` | R0-prime identity: `Agentic Apps R0-prime` 0.1.1, `io.redevops.agentic-apps.r0prime`, launcher `agentic-r0prime`, sidecar `redevops-sidecar`, WiX UpgradeCode `f13fc625-f77d-5c30-b308-a3c3ec1045fc`; frozen v0.3.2 identity recorded beside it | `79bc1f6d4782d713cd13ae2c4a20d6568b3f55b85b740a13e8be1c073d10950a` |
| `packaging/distribution-gate.json` | 21 decisions, all OPEN, 18 with `blocks_distribution: true` (OBSERVED by a script; ids match the freeze record) | `fabab79e96f66e380650618de7ef6d5d9b3093e08ee5aacc64c11dee0e1e3151` |
| `packaging/legal-allowed-signers` | no key listed (ships empty by design) | `0a43cf60ce5b8bc54f59b1f1f0e9de5825ade9850e4555203e8c2f97ab3d9036` |
| `.github/workflows/native-packaging.yml` | actions by commit SHA; runner images by name (macos-14, windows-2022, ubuntu-24.04); no release job (line 156 comment; enforced by `tests/test_workflow_no_publish.py`) | (in tree) |

The two files named in the request that do not exist at e2f2fa9: `packaging/projects_ui.lock.json` (a file of that name exists only on a different line, in `S/followup/*`, pinning the UI as a release asset `projects-ui-root.tgz` v0.2.0 sha256 `a84b7ce4...c9a5e6`; it is not part of this freeze) and `DESKTOP.md` (no such file; the launcher documentation is `deploy/launcher/README.md`, `docs/RUNNING_AGENTIC_APPS_LOCAL.md`, `docs/LOCAL_API_SECURITY.md`).

### 1A.3 Vendored UI bundle and its provenance

* Files (OBSERVED sha256): `agentic_os/projects_ui/assets/index-BAPoIP6S.js` `632cc95eb87626af68bea344d21bcdaa5253b74bd6008ebf136fbadb460146f6` (227,205 bytes); `assets/index-DhBHdPmO.css` `1f8b87172f9f194c162daae140770af0d3c6320134183602f9e24fceb8cf497a` (12,736 bytes); `index.html` `8706d49a0d64aaa55206248e08f2a4acf70130d05ef19204205c25636d1827f8`; `hero.jpg` (neutral generated placeholder, D-LIC-8) `8b1470ffde8d64990b7be1d9759c971fa0f3169f4cfb1884968645de6961a7fb`. These equal the values in the RC3 UI report (`S/rc3/dev-reports/rc3ui.md` lines 11-13).
* Source: public `redevops-io/redevops-projects` at `5114b56012511a292eff0e505fdbd0d8b00544f8` ("Merge pull request #9 ... feat/outreach-demo-recorder", 2026-09-09) plus a LOCAL, unpublished accessibility series. Two commits in a scratch clone (`S/rc3/ui-src/b-5114b56`, branch `rc3/ui-a11y`): `e3cd79e`, `7edf610`, tree `ee735ca691e28eb7ab659c93b73246b9f1f75ab7`. The reviewable form is nine patches `0001`-`0009` in `S/rc3/dev-reports/ui-series/patches/` (with `COVER-LETTER.md`, `CORRESPONDENCE.md`, `SHA256SUMS`), same final tree. Build: `npm ci` with the UI repo lock, `VITE_PROJECTS_API=/ npm run build -- --base=/`, node v24.22.2, vite 5.4.21 (RECORDED, cover letter); vendored by `scripts/build_projects_ui.sh <clone> /` (OBSERVED script, keeps `hero.jpg`).
* In the agentic-os history the bundle arrives in `7180b73` (first re-vendor, e3cd79e build) and `7ef5748` (final, 7edf610 build) (OBSERVED `git log -- agentic_os/projects_ui`).
* Gaps (RECORDED and OBSERVED): the freeze record calls the series "`rc3/ui-series`"; no branch of that name exists in the agentic-os `.git` (the series exists only as patch files in S plus the scratch UI clone). The commit and lock that produced the bundle are not recorded inside the repository: `packaging/sbom/generate_source_offer.py:96-97` prints "NOT RECORDED" for the UI, decisions pins-10 and D-LIC-12 stay OPEN. Upstream `redevops-projects` main is 20 commits ahead with 13-file conflicts unresolved (freeze record section 4).

### 1A.4 Version strings in the desktop line (see section 6 for the inconsistencies)

`pyproject.toml:3` and `agentic_os/__init__.py:16`: 0.2.4 (the Python wheel `agentic-os`); `tauri.conf.json:4`, `Cargo.toml` and `identity.json`: 0.1.1 (the product `Agentic Apps R0-prime`); frozen product `Agentic Apps` 0.1.0; nearest git tag v0.3.2; Cargo package name `redevops-launcher`, binary `agentic-r0prime`.

## 1B. Provenance line B: the agentic-os-stack integration (separate line; READ-ONLY, record only)

Method: `git -C <path> rev-parse/log/status/rev-list/merge-base --is-ancestor/for-each-ref` only; nothing checked out, built or run; no fetch (all "origin/main" values are the local remote-tracking refs as of 2026-11-05). Nothing here is part of the desktop freeze and no SHA below is a desktop input.

### 1B.1 The reference clone and the integration tip

* `/Users/arybach/projects/agentic-os` (a SECOND clone of `https://github.com/redevops-io/agentic-os`; the desktop session uses `/Users/arybach/agentic-os`): branch `main`, HEAD = `origin/main` = `7b4b11c13c90de905c43637de286df6ac65c6fcd` ("Append-only writers: one locked append so no record is lost on Windows (#608)"), clean, 41 registered worktrees (the 36 `aos-wt/*`, `agentic-os-agent-ui`, `agentic-os-artemis`, `rails-mem-gpu-01-wt/agentic-os`, two `/private/tmp/m8-wt-*`), 37 local branches. `git describe`: `v0.4.1-62-g7b4b11c`. (Note: the desktop clone's `origin/main` is the same 7b4b11c.)
* The `aos-wt/*` worktrees are worktrees of that clone (`git rev-parse --git-common-dir` = `.git`). Every `feat/*` worktree below has merge-base `7b4b11c1` with origin/main, so all of them are based on current main and NONE is merged into it (0 behind, N ahead).
* The integration tip is `feat/integration` = `bbf03cbd76d7ed9aa6cdc22408fcec1541434513` (worktree `aos-wt/integ`, clean, last commit 2026-11-05 "docs: PG-6 (rollback-guard race) and PG-7 ... G8 on PG review status"; 263 commits ahead of origin/main, 1151 files changed, +215,962/-115). OBSERVED: every `aos-wt/*` HEAD (including both detached `final-live*` HEADs) and `agentic-os-agent-ui` is an ancestor of it (column "contained in feat/integration"); `agentic-os-artemis` is not.
* g8pg: the request names `feat/g8-pg a47d11c base 89b166d`. Current state (OBSERVED): `feat/g8-pg` = `2546f17b81bd2bd2382c3af325e9214915aa563c` ("G8 on PostgreSQL: documentation, evidence, register rows"), whose parent is `8192b67` ("G8 data-subject export/erasure on PostgreSQL: one implementation behind a backend seam"); base `89b166d24798330e9e41022371fc414e188ca990` is an ancestor (confirmed). `a47d11c` still exists as an object (message identical to 8192b67, authored 2026-11-05 08:11 -0500) but is reachable only from the reflog (`refs/heads/feat/g8-pg@{3}`), i.e. the branch was rewritten after the request was written; `a47d11c` is NOT an ancestor of any branch. Treat `2546f17b` as the current g8pg HEAD. `feat/g8-pg-review` = `45801214cf21f5b275535b86627480e22334743e` merges it ("Merge feat/g8-pg-review: G8 data-subject export/erasure on PostgreSQL (implementation 8192b67, docs 2546f17, ...)") and is contained in `feat/integration`.
* Dockerfiles: `feat/integration` adds NO Dockerfile and no workflow relative to origin/main (OBSERVED `git diff --name-only --diff-filter=A origin/main feat/integration`); it adds compose files for live connector stacks (`deploy/listmonk/listmonk.compose.yml`, `deploy/pins/proposed/apps.compose.pinned.yml`, `tests/live/env/*`) and evidence text. So this line contributes no new image definition to section 3.

### 1B.2 The `aos-wt/*` worktrees (all under `/Users/arybach/projects/aos-wt/`)

| path | branch | HEAD (full SHA) | worktree | commits ahead of origin/main 7b4b11c | contained in feat/integration bbf03cbd | last commit author / date |
|---|---|---|---|---|---|---|
| aos-wt/c-intel | feat/connector-intel | `184409aadcb631b04759b1fad26beffd3c97e5a8` | clean | 58 | yes | Alex Mats, 2026-11-04 |
| aos-wt/c-lago | feat/connector-lago | `920760facbc059613a6e07c1e7cf91e2c06003b9` | clean | 52 | yes | Alex Mats, 2026-11-04 |
| aos-wt/c-listmonk | feat/connector-listmonk | `f1cf712f00eaac6775e07fa07137fc8d72bb4052` | clean | 47 | yes | Alex Mats, 2026-11-04 |
| aos-wt/c-metabase | feat/connector-metabase | `ec2550e64e151ce40fe33b7a2460070ca026827e` | clean | 49 | yes | Alex Mats, 2026-11-04 |
| aos-wt/c-observe | feat/connector-observe | `77f883a668026901847664fc86eb5f0445937427` | clean | 47 | yes | Alex Mats, 2026-11-04 |
| aos-wt/c-postiz | feat/connector-postiz | `65fcd678bfd832f536daa039550c161ff9898da3` | clean | 47 | yes | Claude, 2026-11-04 |
| aos-wt/c-security | feat/connector-security | `39ce12ce7c9b0e0970d31383858b49f966cbffdc` | clean | 51 | yes | Claude, 2026-11-04 |
| aos-wt/conn-review | feat/conn-review | `09471d6f4138c54a32029491eff94c9ba346c205` | clean | 121 | yes | Alex Mats, 2026-11-04 |
| aos-wt/custody | feat/custody | `e73c924fe0b36e2257712b81023de2a8afc2871f` | clean | 151 | yes | Alex Mats, 2026-11-04 |
| aos-wt/custody-r3 | feat/custody-r3 | `4e2bf83f5593c730395a0b97f3c0e609e52a595e` | clean | 168 | yes | Alex Mats, 2026-11-04 |
| aos-wt/custody-rereview | feat/custody-rereview | `2e46e0dce261b2aba8b205136f71f411fe838961` | clean | 163 | yes | Alex Mats, 2026-11-04 |
| aos-wt/custody-review | feat/custody-review | `914df14954aad8d6616d9932adfadb5e422439be` | clean | 160 | yes | Alex Mats, 2026-11-04 |
| aos-wt/egress | feat/cd-egress | `d197f1d4dfa3f96540839002fed4b7eb997707d2` | clean | 233 | yes | Alex Mats, 2026-11-04 |
| aos-wt/final-live | (detached) | `ed74044b4365ab0cbc7300029c5daa08768297c6` | dirty 5 | 230 | yes | Alex Mats, 2026-11-04 |
| aos-wt/final-live2 | (detached) | `cf67473ff10b52c709a570cff39b38861e7e1760` | dirty 1 | 232 | yes | Alex Mats, 2026-11-04 |
| aos-wt/g5 | feat/g5 | `e14f065edcf3500b7b3a0ef7f29e8f6b8a42ff18` | clean | 123 | yes | Alex Mats, 2026-11-04 |
| aos-wt/g5-review | feat/g5-review | `35421e2f145bc8e9e2259e89209d5ff9fa3550a6` | clean | 124 | yes | G5 reviewer, 2026-11-04 |
| aos-wt/g6g9 | feat/g6g9 | `6b6159f44864013c93ba1f4995cb098fe8c01f26` | clean | 48 | yes | Alex Mats, 2026-11-04 |
| aos-wt/g6verify | feat/g6verify | `102dad2cf303eefad227b6f883b933c9369fea6b` | clean | 130 | yes | Alex Mats, 2026-11-04 |
| aos-wt/g7g8 | feat/g7g8 | `bc65fc71fdd2041f2804138bbba17b88561cf73b` | clean | 51 | yes | Alex Mats, 2026-11-04 |
| aos-wt/g8pg | feat/g8-pg | `2546f17b81bd2bd2382c3af325e9214915aa563c` | clean | 242 | yes | Alex Mats, 2026-11-05 |
| aos-wt/g8pg-review | feat/g8-pg-review | `45801214cf21f5b275535b86627480e22334743e` | clean | 258 | yes | Alex Mats, 2026-11-05 |
| aos-wt/integ | feat/integration | `bbf03cbd76d7ed9aa6cdc22408fcec1541434513` | clean | 263 | yes | Alex Mats, 2026-11-05 |
| aos-wt/kit | feat/connector-kit | `e38b83bff66557a0e863cb91df29886c33fe0bc4` | clean | 46 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit2 | feat/kit2 | `a48436347ce0334c06d22c5085654b110f3fdd8c` | clean | 128 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit2-review | feat/kit2-review | `e9c98be4234f502b52637a61a22c78c40108f62c` | clean | 132 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit3 | feat/kit3 | `75d43c0e005aad7624c9eb4f49784b3ec7723d3b` | clean | 166 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit3-r3 | feat/kit3-r3 | `a2a69524500e5398cf581043154ca972ca46f759` | clean | 197 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit3-rereview | feat/kit3-rereview | `3804d29f05b9634b64dcbf069ac57543c112cfb6` | clean | 192 | yes | Alex Mats, 2026-11-04 |
| aos-wt/kit3-review | feat/kit3-review | `17f86038d495242c95c17d12f5edd2b0748f8b7b` | dirty 1 | 185 | yes | Alex Mats, 2026-11-04 |
| aos-wt/phase0b | feat/phase0b-provenance | `4287657596a94bd05731242b8520ded43fcc191c` | clean | 43 | yes | Alex Mats, 2026-11-04 |
| aos-wt/register | feat/register | `2230f35e5f5b539beb78ee8219d66227dafd7e26` | clean | 232 | yes | Alex Mats, 2026-11-04 |
| aos-wt/schemafix | feat/pg-schema-cleanup | `31755b5adbdbdc38b4ccf1b904c16a6d8287a560` | clean | 229 | yes | Alex Mats, 2026-11-04 |
| aos-wt/sdk | feat/agent-workspace-sdk | `ef1737e26507a5ea7e6c01e0478c12aefab63485` | clean | 43 | yes | Alex Mats, 2026-11-04 |
| aos-wt/sec-review | feat/sec-review | `4d0c86212e7f4b14cf6bca38c0594dab74ed0123` | clean | 51 | yes | Alex Mats, 2026-11-04 |

Owner session: not recorded in git (all commits are authored "Alex Mats" or "Claude" with the owner's identity). By convention the brief assigns this line to session `agentic-os-stack-6e`; supporting evidence: a scratchpad directory `/private/tmp/claude-501/-Users-arybach-projects-agentic-os-stack` exists, and `/Users/arybach/projects/agentic-os-stack` is NOT a git repository (it holds only audit documents: `audit/PHASE0_AUDIT_AND_REVISED_PLAN.md`, `PHASE2_DURABLE_GOVERNANCE.md`, ...); the stack's code lives in the branches above. Dirty worktrees: `final-live` (5 status entries, among them 2 modified evidence files and an untracked `tests/e2e/chatwoot/node_modules`), `final-live2` (1 modified evidence file), `kit3-review` (1 modified csv): other sessions' scratch state, left as is.

### 1B.3 Other repositories named in the request (path, branch, HEAD, state)

| repo | path | branch | HEAD (full SHA) | worktree | state vs its default branch | owner session (evidence) |
|---|---|---|---|---|---|---|
| agentic-os (reference clone) | `/Users/arybach/projects/agentic-os` | main | `7b4b11c13c90de905c43637de286df6ac65c6fcd` | clean | = origin/main | stack line (above) |
| agentic-os, a worktree of the reference clone | `/Users/arybach/projects/agentic-os-agent-ui` | feat/agent-native-ui | `1e6f99c014b73d3ce25bf315b04f98f6f40f837c` | clean | 42 ahead / 0 behind origin/main, contained in feat/integration | stack line |
| agentic-os, a worktree of the reference clone (Artemis) | `/Users/arybach/projects/agentic-os-artemis` | feat/artemis-integration-review | `2b2dd9daaca7cd25301a4f0d8ce96da0bdcbfc09` | clean | 7 ahead / 0 behind origin/main, NOT contained in feat/integration, tag `artemis-dev-milestone-354da5f` | not evident |
| agentic-os-stack | `/Users/arybach/projects/agentic-os-stack` | (not a git repo) | n/a | audit documents only | n/a | `agentic-os-stack-6e` (scratchpad dir name) |
| agentic-os-src | `/Users/arybach/projects/agentic-os-src` | (not a git repo) | n/a | audit documents (`REPO_CONSOLIDATION_AUDIT.md`, `GO_KERNEL_DIVERGENCE_REVIEW.md`, ...) | n/a | scratchpad dir `-Users-arybach-projects-agentic-os-src` exists |
| context-runtime | `/Users/arybach/projects/context-runtime` | feat/agent-harness-integration | `c2559459b636b204b0a213288b820b3d43c5c6a2` | dirty 4 (2 modified: `supply_chain.py`, `pyproject.toml`; 2 untracked: `known_malicious.py`, its test) | 7 ahead / 125 behind origin/main; not merged. origin/main = `47d2e3bcf6233c1d4fed11b16fce40139f8c2fda` (the staged snapshot commit); tags v0.4.1, w5, w7 | not evident |
| agent-harness | `/Users/arybach/projects/agent-harness` | feat/ours-h2 | `aac5a43c79095bcfc31d168a873e486f2488ae9d` | dirty 1 (untracked `experiments/`) | 42 ahead / 52 behind origin/main; not merged. origin/main = `876ae44a506c14b1c47fdfb09f03943081f58c58` (the staged snapshot commit); tags v0.1.0, v0.2.0 | not evident (memory: Harness-squared worktrees are not to be touched) |
| runtime-contracts | `/Users/arybach/projects/runtime-contracts` | main | `d739c1941fd0f03aad9b5c75c6f9b2a0df67cc26` | clean | = origin/main, tag v0.3.16 (the staged commit). NOTE: the desktop build pins v0.3.4, not v0.3.16 | not evident |
| redevops-rag | `/Users/arybach/projects/redevops-rag` | (detached) | `6e50d6db262135f9fa51f8c5cbc9be4b8952179b` | clean | an ancestor of origin/main (0 ahead / 73 behind); origin/main = `3a6c25379b93d81124ffc80ca459ff324685f18c`. `pyproject` extra `rag` pins `@main` (excluded from the desktop build) | not evident |
| agentic-tests | `/Users/arybach/projects/agentic-tests` | master | `249969ec2220ec0f24019e9073bdc8d4f5d3f15a` | clean | = origin/master; PR #44 head is `14de186a8312ed8d30d93234b786b5fa2bbaecce` on origin `hardening/t01-t04-t06` (separate, not in RC3) | the desktop session reviewed it (freeze record section 6) |
| knowledge-frontier | `/Users/arybach/projects/knowledge-frontier` | master | `976101014b1cf1c48218f732531ce2754aab758b` | clean | = origin/master | n/a (desktop pins v0.1.0 `d6a78ca3...`, a different commit) |
| ui-agent | `/Users/arybach/projects/ui-agent` | feat/ui-agent-p0-audit | `c5f0f170809d9e485b9ced638fef0df386d3e3cc` | clean | 1 ahead / 0 behind origin/main | not evident |
| decision-bench | `/Users/arybach/projects/decision-bench` | master | `2279e76013b88354d91f0ae5f8b46cdfe50f9dfc` (2026-09-18) | dirty 1 (untracked `fixtures/corpus/`) | no `origin` remote configured (OBSERVED: empty `remote get-url`), so no merged/unmerged state can be computed | `decision-bench-00` (scratchpad dir `-Users-arybach-projects-decision-bench`) |
| observability | `/Users/arybach/projects/observability` | (not a git repo) | n/a | documents only (`AUDIT_FINDINGS_2026-11-01.md`, remediation logs) | n/a | `observability-a5` (scratchpad dir `-Users-arybach-projects-observability`, 10 session dirs). The `obs-r0 7aa435e` commit named in the phase-2 report is not a valid object in either agentic-os clone: not located |
| redevops-qa | read-only mirror of `redevops-io/redevops-qa` (assessed 2026-11-10); the earlier scratch clone `S/id/src/redevops-qa` (2ad97f3e, 2026-11-01) is superseded | n/a | main `acfa0851` (ahead of the staged candidate-3 source `7456c16d162fe6a67e4c30d847629806619a305a` by its PRs #42 to #51; the staging record is RECORDED, not re-verified) | n/a | independent repository: not a runtime, build or qualification dependency of this repository | QA workstream |

Staged private OCI packages (RECORDED in `~/Documents/60_Security_Audits_Code_Review/QA_PRIVATE_STAGING_RECORD_2026-11-02.md`; not re-verified here, no registry contact): `ghcr.io/redevops-io/staged-runtime-contracts:0.3.16-d739c1941fd0` (digest `sha256:268dc75e...c0e` full value in the record); `staged-agent-harness:0.2.0-snapshot-876ae44a506c`; `staged-context-runtime:0.4.1-snapshot-47d2e3bcf623`; `staged-redevops-qa-bundle` candidates 1-3 (candidate 3: tag `0.1.0.dev0-cp312-linux-x86_64-7456c16d162f`, manifest digest `sha256:dba009f72d3e59413d74b8c5ee35517fac95466b6c5b535d4ed33ebff476997f`). These are OCI ARTIFACTS (media type `application/vnd.redevops.staged-source.v1+json`: source tarball, sdist, wheel, SHA256SUMS, PROVENANCE.json), not container images, private, anonymous fetch 401. The record: "agentic-os native packaging" and "redevops-rag" were NOT staged by owner direction.

## 2. Component ownership and licence class

Licence classes are the DECLARED statements in `packaging/distribution-gate.json`, `packaging/notices/components.json`, `docs/legal/COUNSEL_BRIEF.md` section 3 and `LICENSING.md`; no legal conclusion is drawn and nothing is cleared. "Owner session" is by the convention of the brief; git does not record sessions.

| Component | Built from (repo, pin) | Goes into | Owner session | Licence class as recorded |
|---|---|---|---|---|
| Launcher (Tauri shell, `agentic-r0prime`) | `deploy/launcher/src-tauri` of agentic-os e2f2fa9; 427 crates (`Cargo.lock`) | .app/.dmg, .msi/.nsis, .deb/.AppImage | this session (RC3 desktop) | family licence via `license-file = "../../../LICENSING.md"` (Cargo.toml:8; no SPDX id, deliberately); crates mostly permissive, 5 MPL-2.0 crates, objc2 family flagged D-LIC-18 |
| Sidecar `redevops-sidecar` (PyInstaller) | agentic-os wheel 0.2.4 from tracked files; 21 third-party wheels (locks); runtime-contracts v0.3.4 and redevops-connectors v0.1.0 from git pins; CPython 3.12.10 | inside every desktop bundle | this session | agentic-os and runtime-contracts: family licence; connectors: declared plain AGPL-3.0-or-later at the pin, tag has no licence file (D-LIC-2, OPEN, no text shipped); CPython PSF-2.0; PyInstaller bootloader GPL-2.0-or-later with Bootloader Exception; OpenSSL Apache-2.0; certifi MPL-2.0 (D-LIC-4); Windows only: Microsoft VC runtime DLLs (pins-08, counsel) |
| knowledge-frontier v0.1.0 | git pin | built into the wheelhouse, EXCLUDED from the sidecar (`redevops-sidecar.spec` UNEXERCISED; D-LIC-14, D-LIC-2 inferred licence) | this session | licence inferred (no licence file at the tag) |
| Projects UI bundle | `redevops-projects` 5114b560 + local a11y series (section 1A.3) | inside the sidecar (`agentic_os/projects_ui`) | this session (UI series), upstream repo is the owner's | first-party, family licence; React 18.3.1 MIT banners (D-LIC-7, non-blocking); source commit/lock not recorded (pins-10, D-LIC-12) |
| Licence/About record, notices, SBOM, source offer | `packaging/notices`, `packaging/sbom` | `Licenses/` folder in each bundle | this session | generated; 21 legal decisions all OPEN; production legal signer allow-list empty |
| Signing procedure (not executed) | `packaging/signing/**` | not a shipped artifact | this session | n/a |
| QA bundle (`redevops-qa`) | separate repo; main `acfa0851`, staged candidate-3 source `7456c16d` | NOT in the desktop build and not a dependency of this repository: it runs only against its own bundled fixture app (it makes no claim about this product's behaviour), on Linux x86_64 only. Released independently; Linux x86_64 tarball staged privately | QA workstream | D-LIC-16 hand-off decision OPEN; 29 pending licence decisions in its own matrix (RECORDED, readiness report section 6) |
| Edge Sentinel (`apps/edge-sentinel`) | this repository, reference application | NOT in the desktop build: absent from every sidecar; run as its own service (port 8203, its own `Dockerfile`), not started by the launcher | this session | independent component; the source here predates its later hardening, which is released separately; same family licence as the repository |
| agentic-os-stack integration (connectors, custody, G5-G8, PG ledger ...) | `feat/integration` bbf03cbd of the reference clone (section 1B) | NOT in the desktop build; no Dockerfile or image defined in this line | `agentic-os-stack-6e` | family licence (same repo); not assessed here |
| Observability stack | `/Users/arybach/projects/observability` (documents; no git repo located) | not in the desktop build | `observability-a5` | n/a here |
| decision-bench | `/Users/arybach/projects/decision-bench` master `2279e760` | not in the desktop build | `decision-bench-00` | not recorded in this tree |
| context-runtime, agent-harness | their own repos; staged as OCI artifacts (section 1B.3) | not in the desktop build (extras `rag`/`mcp`/`postgres` are excluded from the native suite: `build-wheelhouse.sh` `EXTRAS="projects,duckdb"`) | not evident | agent-harness described by D-LIC-16 as "plain-AGPL" (open item); context-runtime not stated in this tree |
| Demo/dev OCI images (section 3C) | agentic-os Dockerfiles at e2f2fa9 | none built or published from this repo | n/a | family licence; base `python:3.12-slim` (Debian + CPython, separate licences, not enumerated here) |

Wording check: `grep` of README, LICENSING, `pyproject`, the launcher UI and `tauri.conf.json` finds "source-available, not open source" statements only; D-LIC-15 (OPEN) records that `agentic_os/world/outreach.py` still contains the phrase "an open-source runtime stack". `agentic_os/stack_knowledge.py:160-162` is a question-matching keyword list, not a claim.

## 3. Release artifact inventory

Status words: EXISTS (file present and hash recomputed), RECORDED (record only), NONE (never produced for this identity), N/A (no producer in the repo). No RC3 package of any kind exists for e2f2fa9 (freeze record: "Nothing is built for distribution"); the only installable artifact built from this line is the RC2 macOS arm64 unsigned dmg.

### 3A. Desktop installers per platform (target list: `tauri.conf.json:29` `["deb","appimage","msi","nsis","dmg","app"]`)

Names are DERIVED by `python3 -I packaging/tools/identity.py get <key>` (OBSERVED) except Linux (the identity file has no Linux names; `docs/R0_PRIME_IDENTITY.md:71`: "no Linux R0-prime artifact is built or claimed").

| Platform | Artifact | Producer | Inputs | Output name | Status |
|---|---|---|---|---|---|
| macOS arm64 | `.app` | `packaging/build-all.sh` (default `LAUNCHER_BUNDLES=app`) or the candidate script below | pinned recipe, aarch64 locks | `deploy/launcher/src-tauri/target/release/bundle/macos/Agentic Apps R0-prime.app` | RC2: EXISTS (inside the candidate dir). RC3 e2f2fa9: NONE |
| macOS arm64 | `.dmg` | `packaging/candidate/build-local-macos-candidate.sh OUT_DIR` (runs build-all.sh with `CI=true LAUNCHER_BUNDLES=app,dmg`; `CI=true` skips the Finder/AppleScript styling) | same | `Agentic Apps R0-prime_0.1.1_aarch64.dmg`, volume `Agentic Apps R0-prime` | RC2: EXISTS (sha256 above). RC3: NONE |
| macOS x86_64 / universal2 | `.dmg` | NONE: no producer, no x86_64 lock, no CI leg (`native-packaging.yml:33-34` dropped macos-13; `build-sbom.sh:12` and `build-notices.sh:16` hard-code `aarch64-apple-darwin`; `INTEL-ROSETTA-TEST-PLAN.md` is a plan only) | - | - | NONE (UNVERIFIED whether feasible) |
| macOS | signed/notarized dmg | `packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --real` (owner terminal, owner identity) | signing manifest, scope file | - | NONE, not authorized. The frozen v0.3.2 dmg was signed and notarized in a separate exercise (RECORDED) |
| macOS | `.pkg` / Mac App Store build | NONE (no producer, no sandbox entitlement set, no `.pkg` step anywhere in the repo; OBSERVED by `git grep`) | - | - | NONE |
| Windows x64 | `.msi` (WiX 3.14.1 downloaded by tauri-bundler) | `packaging\build-windows.ps1` -> `build-all.sh` in git-bash, or CI leg `windows-2022` | `requirements-windows.lock`, MSVC | `Agentic Apps R0-prime_0.1.1_x64_en-US.msi` | NONE built; the script and CI leg for the R0-prime recipe were never executed on Windows (build-windows.ps1 header: "NOT executed on Windows ... UNVERIFIED"). The frozen v0.3.2 msi existed (CI run 35353188692) and was signed separately (RECORDED) |
| Windows x64 | NSIS `.exe` (NSIS 3.11 downloaded by tauri-bundler) | same | same | `Agentic Apps R0-prime_0.1.1_x64-setup.exe` | NONE (same) |
| Windows | MSIX (Microsoft Store) | NONE (no producer; `git grep -i msix` finds nothing) | - | - | NONE |
| Windows ARM64 | any | NONE (`build-launcher.sh` refuses a non `x86_64-pc-windows-msvc` host) | - | - | NONE |
| Linux x86_64 | `.deb` | CI leg `ubuntu-24.04` (`native-packaging.yml`) or `build-all.sh` on Ubuntu | `requirements-linux.lock`, WebKitGTK 4.1 dev libs | derived by tauri-bundler from productName (expected `Agentic Apps R0-prime_0.1.1_amd64.deb`; UNVERIFIED, `acceptance-ubuntu.sh` finds it with `find`, and the doc names `ReDevOps_*.deb`) | NONE for R0-prime. The older scaffold (0.1.0) was built on evo-x2 in September (RECORDED, `BUILD-hosts.md`, `README.md`) |
| Linux x86_64 | `.AppImage` | same | same | expected `Agentic Apps R0-prime_0.1.1_amd64.AppImage` (UNVERIFIED) | NONE for R0-prime |
| Linux | `.rpm`, snap, flatpak | NONE (not in `tauri.conf.json` targets; `git grep` finds no snapcraft.yaml, rpm or flatpak manifest) | - | - | NONE |
| Linux aarch64 | any | NONE (no aarch64 lock; SBOM triple x86_64) | - | - | NONE |

### 3B. Supporting artifacts of a desktop build (per platform, same producers)

| Artifact | Producer | Output | Status |
|---|---|---|---|
| Wheelhouse (25 wheels = 21 third-party + 4 first-party), `WHEELS.sha256`, `wheelhouse-manifest.json`, `first-party-wheels.json` | `packaging/build-wheelhouse.sh` | `packaging/wheelhouse/` (git-ignored) | regenerated per build; RC2 copy inside the candidate evidence |
| Sidecar `redevops-sidecar[.exe]` | `packaging/build-sidecar.sh` (twice: pass 1 without, pass 2 with `REDEVOPS_LICENSES_DIR`) then `stage-sidecar.sh` | `packaging/dist/redevops-sidecar`, staged as `src-tauri/binaries/redevops-sidecar-<triple>` (git-ignored) | per build |
| Build manifest `redevops.build-manifest/1` | `packaging/tools/build_manifest.py` (called by the three build scripts) | `packaging/dist/build-manifest.json` | per build |
| SBOM CycloneDX 1.5 + verify report | `packaging/build-sbom.sh` (`sbom/generate_sbom.py`, `verify_sbom.py`) | `packaging/dist/sbom/{sbom.cdx.json,archive-attribution.json,sbom-inputs.json,sbom-verify.txt}` | RC2: 425 components EXISTS in the candidate; RC3: NONE |
| Notices and About record | `packaging/build-notices.sh` -> `notices/build-licenses.sh` | `deploy/launcher/src-tauri/licenses/{THIRD_PARTY_NOTICES.txt,NOTICES_MISSING.md/.json,about.json,LICENSES-MANIFEST.json,LICENSE*,LICENSING.md}` -> `Licenses/` in every bundle (`tauri.conf.json:42` `resources`) | per build; NOTE the rc3/dev staged `licenses/` had no `about.json` (freeze record section 3) |
| Source offer | `packaging/sbom/generate_source_offer.py` with `packaging/SOURCE-OFFER.md` template | `SOURCE-OFFER.md` beside the artifacts (NOT inside the .app/.dmg, D-LIC-12) | RC2 EXISTS (75,496 bytes) |
| Provenance statement | `packaging/candidate/` assembler (unsigned in-toto/SLSA-style statement, local-assembler builder) | `provenance.statement.json` in the evidence set | RC2 EXISTS (`evidence-macos-arm64-unsigned-rc2-final2/`); unsigned |
| Evidence release manifest + signature | candidate evidence assembler; `packaging/signing/sign_manifest_sshsig.sh` (SSHSIG, owner's agent only) | `release-manifest.json` (+ `.sig` when signed) | manifest EXISTS for RC2 (hash above); NO signature; not authorized |
| Application signatures | `packaging/signing/sign_and_notarize_r0prime_macos_v3.sh`, `sign_windows_r0prime_v3.sh` (modes `--dry-run`, `--adhoc-test`/`--throwaway-test`, `--real`) | signed dmg/msi/exe | rehearsal modes only; `--real` not authorized |
| Entitlements input | `packaging/macos/sidecar.entitlements.plist` | single key `com.apple.security.cs.disable-library-validation` on the sidecar only (provisional, `docs/MACOS_ENTITLEMENTS.md`) | in tree |
| CI workflow artifacts | `native-packaging.yml` upload step (`redevops-<os>`: manifest, SBOM, WHEELS.sha256, installers) | GitHub workflow artifacts | NONE for this tree (never pushed) |
| Reproducibility report | `packaging/verify-reproducible.sh` | `reproducibility-report.{md,json}` | RECORDED 2026-11-04 (PINS.md "Reproducibility"), macOS app only |

### 3C. OCI images: every Dockerfile in the repo at e2f2fa9 (OBSERVED by `git ls-files` and grep)

Result in one line: none of the 26 Dockerfiles is a release image. No workflow builds or pushes an image (`.github/workflows` holds only `native-packaging.yml` and `secret-scan.yml`; `git grep` finds no `docker/build-push`, buildx, cosign or attestation step); no Dockerfile pins its base by digest; none declares a `HEALTHCHECK`; only `deploy/sidekick-devops/Dockerfile` sets a non-root `USER` (10001). All use `FROM python:3.12-slim` (floating tag, multi-arch upstream; no digest). The images are demo/dev/module images used by compose files or by sibling module repos.

| Image (compose name where one exists) | Dockerfile | Port | User | Arch notes | Class | Used by |
|---|---|---|---|---|---|---|
| control plane (`control-plane`) | `Dockerfile` (apt installs git, `pip install .` from the git pins, purges git) | 8080 | root | arch-neutral | dev/demo (control-plane runtime) | `docker-compose.yml`, `deploy/docker-compose.yml` |
| Projects UI + API (`redevops-projects`, doc comment only) | `Dockerfile.projects` (git stays in the image; `pip install '.[projects]'`) | 8787 | root | arch-neutral | dev/self-host example | none (documented `docker build -f Dockerfile.projects`) |
| module stand-in (`${PORT}`, default 8000) | `deploy/Dockerfile.module` (`pip install "fastapi>=0.110" "uvicorn>=0.29"` unpinned) | $PORT | root | arch-neutral | demo | `deploy/docker-compose.yml` (9 stand-ins) |
| agentic-crm / privacy / billing / books / compliance / control-tower / edge-sentinel / growth-assistant / growth-engine / guide / lifecycle / market-radar / outreach-engine / social-autopilot / support | `apps/<app>/Dockerfile` (15 files) | 8210 / 8212 / 8201 / 8209 / 8208 / 8202 / 8203 / 8213 / 8205 / 8215 / 8211 / 8204 / 8214 / 8206 / 8207 | root | `edge-sentinel` downloads x86_64 docker CLI 27.3.1 and `cosign-linux-amd64` from `releases/latest`, and pipes `trivy` and `syft` install scripts from each project's `main` branch to `sh` (lines 12-21): amd64-only and unpinned. Others arch-neutral; `compliance` bakes `results/` and `content/` into the image | dev/demo; no compose in this repo references them (module repos do) | sibling module repos |
| admin-console / attention-console / evidence-explorer-demo / refcore-billing / security-showcase-demo / unsubscribe-service / world-canvas-demo | `deploy/<name>/Dockerfile` (7 files; each has a `*.compose.yml` using `image: ${..._IMAGE:-<name>:latest}`) | 8105 / 8104 / 8101 / 8106 / 8099 / 8103 / 8102 | root | arch-neutral; uvicorn started with `--loop asyncio --http h11` | demo | their own compose files |
| sidekick-devops (`sidekick-devops:latest`) | `deploy/sidekick-devops/Dockerfile` (kubectl v1.31.0 `linux/amd64` downloaded without a checksum; `fastapi==0.115.6 uvicorn==0.32.1` pinned, rest floating; optional SkyPilot build-arg) | 8000 | 10001 (non-root) | amd64-only because of kubectl | dev/demo bundle | `deploy/sidekick-devops/docker-compose.yml` (also pulls `prom/prometheus:v2.54.1`, `grafana/grafana:11.2.2`, `grafana/loki:3.2.0`, `grafana/promtail:3.2.0` by tag, not digest) |

Images referenced but not built from this repo: `ghcr.io/redevops-io/redevops-runtime:0.3.1` and `ghcr.io/redevops-io/cockpit:v6` (Helm values `deploy/reference-system/redevops-runtime/values.yaml:16,124`; the comment says the tag must be published before a one-click deploy), `rocm/vllm:latest`, `pgvector/pgvector:pg16`, `curlimages/curl:8.10.1`, `traefik:v3.0` (`deploy/docker-compose.yml:196`), `vllm/vllm-openai:latest` (`deploy/nemo-voice-agent/nemo-llm.k8s.yaml`); profiles carry `REPLACE_WITH_PINNED_DIGEST` placeholders. `deploy/k8s/` holds only an example selector-less Service for an external LLM host. Existing private GHCR `staged-*` packages are OCI source artifacts, not images (section 1B.3). No SBOM, provenance or signature exists for any image of this repo; the cosign/oras binaries are on this Mac but no image has been signed.

## 4. Reproducible build instructions per platform

Common facts (OBSERVED unless marked). One recipe: `packaging/build-all.sh` owns the order: distribution gate (`check-distribution-gate.py --platform <os> --about-slots`; a private build only prints the open decisions, a distribution build `REDEVOPS_DISTRIBUTION_BUILD` other than empty/0 exits 3 while any blocking decision is OPEN) -> `build-wheelhouse.sh` -> `build-sidecar.sh` (pass 1) -> `build-notices.sh` -> `build-sidecar.sh` (pass 2 with `REDEVOPS_LICENSES_DIR=deploy/launcher/src-tauri/licenses`) -> `stage-sidecar.sh` -> `build-launcher.sh` -> `build-sbom.sh` -> `check_notices_vs_sbom.py` -> `check_about.py`. `build-launcher.sh` refuses an unpinned rustc/tauri-cli, a missing `licenses/`, or a staged sidecar that is not the pass-2 binary. Network needed: PyPI (hash-checked wheels), GitHub (clones of three public tags of runtime-contracts, knowledge-frontier, redevops-connectors), crates.io (`cargo fetch --locked` and licence texts), python.org/others for notice texts; Windows also downloads WiX 3.14.1 and NSIS 3.11 into `%LOCALAPPDATA%\tauri`. Pinned toolchain: CPython 3.12.10 exactly (`common.sh assert_python` fails otherwise), PyInstaller 6.22.3, pip 26.2.1, setuptools 84.0.0, Rust 1.98.1, tauri-cli 2.11.4, uv 0.12.16 (recorded only, never asserted; see section 6). Python locks exist for aarch64-apple-darwin, x86_64-pc-windows-msvc and x86_64-unknown-linux-gnu ONLY (`regen-python-locks.sh:17`).

### 4.1 macOS arm64 (host: Apple-silicon macOS with Xcode Command Line Tools; this Mac qualifies)

Status: VERIFIED for RC2 (RECORDED: phase-2 report section 3-4: two clean builds byte-identical on the host except the dmg; candidate smoke 63/63, `verify_dmg.sh` 33, `headless_smoke.sh` 38, 0 FAIL). NOT built at e2f2fa9: F-1 changed the launcher `app.js`, so a launcher rebuild is required (freeze record section 4); the e2f2fa9 native smoke used a validation build.

```
S=/private/tmp/claude-501/-Users-arybach-agentic-os/30e85532-95c8-480d-aadc-1f976c943ab3/scratchpad
git -C /Users/arybach/agentic-os worktree add -b rel/<name> $S/rel/wt-<name> e2f2fa9ef59dfc474eb561a87282019a06469b3b
export PATH="$S/toolchain/rust/bin:$S/toolchain/npm/node_modules/.bin:$PATH"   # rustc/cargo 1.98.1 (aarch64-apple-darwin tarball), tauri 2.11.4 (npm @tauri-apps/cli)
export CARGO_HOME="$S/toolchain/cargo-home"
cd $S/rel/wt-<name> && packaging/candidate/build-local-macos-candidate.sh "$S/rel/out/<candidate-dir>"   # OUT_DIR must be new or empty
```

The script re-executes itself under `env -i` with an allowlist and a throwaway HOME/TMPDIR inside OUT_DIR, refuses a dirty tree and any `APPLE_*`/`CODESIGN*`/`TAURI_SIGNING*`/`KEYCHAIN*` variable, builds `app,dmg` with `CI=true`, and assembles `sbom/`, `notices/`, `SOURCE-OFFER.md`, `listings/`, `smoke-test/`, `README-LOCAL-BUILD.md`, `SHA256SUMS`. Toolchain on this Mac (OBSERVED): `uv 0.13.6`, uv-managed `cpython-3.12.10-macos-aarch64-none` present in `~/.local/share/uv/python`, Rust 1.98.1 and node-based tauri 2.11.4 only under S/toolchain (not on the default PATH; `rustc`, `cargo`, `rustup`, `tauri` are MISSING from PATH), macOS 27.0.1 arm64, Command Line Tools only (no full Xcode: `xcodebuild` fails).
Non-determinism: PyInstaller PKG timestamps (mitigated by `SOURCE_DATE_EPOCH`=commit time and `PYTHONHASHSEED=1`, `build-sidecar.sh:50-53`; sidecar was byte-identical across two local builds with the SAME interpreter; a python.org framework interpreter (frozen CI) vs uv python-build-standalone (local) differs in the PKG layer, decision pins-06 UNVERIFIED); rustc embedded paths (removed by `--remap-path-prefix`, POSIX only); Mach-O `LC_UUID` and ad-hoc signature follow any content change; the dmg (hdiutil timestamps, Finder styling) is NOT reproducible: compare the `.app` file hashes, not the dmg; local uv 0.13.6 vs pinned 0.12.16 and tauri-cli from npm vs `cargo install tauri-cli` (same version number, different binary) are recorded not asserted; certifi differs from the frozen v0.3.2 set on purpose (pins-16).
Intel: UNVERIFIED and not a pinned configuration.

### 4.2 Linux x86_64 (host: Ubuntu 24.04 x86_64; NOT this Mac natively)

Status: UNVERIFIED for R0-prime. Last real run was the older scaffold on evo-x2 (RECORDED). The R0-prime launcher (two-step purge, signal handling, per-OS navigation guard) has never been built on Linux; `docs/RUNNING_AGENTIC_APPS_LOCAL.md:58` says the Linux containment path is "UNVERIFIED on this build host".

```
sudo apt-get update && sudo apt-get install -y libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libayatana-appindicator3-dev patchelf rsync build-essential git curl
# exact: CPython 3.12.10 on PATH (setup-python or pyenv), uv 0.12.16, rustup toolchain 1.98.1, then:
cargo install tauri-cli --version 2.11.4 --locked
export UV_PYTHON_PREFERENCE=only-system UV_PYTHON_DOWNLOADS=never
cd <checkout at e2f2fa9> && ./packaging/build-all.sh          # bundles: deb + appimage from tauri.conf.json targets
# output: deploy/launcher/src-tauri/target/release/bundle/{deb,appimage}/
./packaging/acceptance-ubuntu.sh <path-to.deb>                # installs with sudo apt/dpkg, serves 127.0.0.1:8787, expects 401 without a session
```

Note `acceptance-ubuntu.sh` expects the sidecar at `/usr/bin/redevops-sidecar` and runs `sudo`. Non-determinism (all UNVERIFIED): PyInstaller as above with the distro CPython 3.12.3 vs 3.12.10 (frozen CI used `/usr/bin/python3.12` 3.12.3, PINS.md), `.deb` member timestamps, AppImage squashfs mtimes and the linuxdeploy/appimagetool helpers the bundler downloads (not pinned by this repo).

### 4.3 Windows x64 (host: Windows 11 or Server 2022 x64; NOT available here)

Status: UNVERIFIED. `build-windows.ps1` is static-lint only, never executed (header lines 12-13); 8 defects of the Windows path were fixed by static review in RC2 (phase-2 report item 9). Prereqs (header lines 7-10): Git for Windows (bash.exe), uv, CPython exactly 3.12.10, Rust 1.98.1 `x86_64-pc-windows-msvc` + VS Build Tools C++ workload, WebView2 runtime, `cargo install tauri-cli --version 2.11.4 --locked`; build from a short path (MAX_PATH).

```
powershell -File packaging\build-windows.ps1     # runs packaging/build-all.sh in git-bash; output deploy\launcher\src-tauri\target\release\bundle\{msi,nsis}\ + packaging\dist\
packaging\acceptance\windows-r0prime\Run-All.ps1 # kit scripts 01-11 (STATIC, never run)
```

Known non-determinism: WiX/NSIS (GUIDs and timestamps; the frozen run's MSI and NSIS launchers differed at equal size, not investigated, RECORDED), the rustc path remap is POSIX-only so the launcher embeds Windows paths, the tool cache `%LOCALAPPDATA%\tauri` is reused without re-hashing (build on a clean profile), the frozen run used windows-2025 (`windows-latest`) while the workflow pins windows-2022. Signing is a separate stage (Authenticode via Azure Artifact Signing from the macOS host with Jsign; not authorized for RC3).

### 4.4 OCI images (any Docker host; Docker Desktop on this Mac qualifies)

No release recipe exists. A rehearsal build is `docker build -f <Dockerfile> -t <name> .` from the repo root (build contexts: repo root for `Dockerfile`, `deploy/Dockerfile.module`, `deploy/sidekick-devops/Dockerfile`; the app directory for `apps/<app>/Dockerfile` and `deploy/<name>/Dockerfile`). Reproducibility is not defined: floating base tag, unpinned `pip install`, `apt-get`, `releases/latest` downloads and `curl | sh` installs (section 3C).

## 5. Which release-qualification asks are doable on THIS Mac

This Mac (OBSERVED): arm64, macOS 27.0.1 (26A434), 36 logical CPUs, 256 GiB RAM; Docker Desktop 29.9.2 (daemon running, server linux/aarch64, 24 CPUs in its VM, VM memory 8.3 GB; buildx `desktop-linux` lists linux/amd64 and others via emulation); Lima 2.2.1 with one existing VM `aos-ubuntu` (vz, aarch64, RUNNING, belongs to another session: do not use or stop it); colima present; `cosign` and `oras` present; `trivy`, `grype`, `syft`, `skopeo`, `qemu-system-*`, `snapcraft`, `pwsh`, `wine`, `makensis`, `osslsigncode`, `jsign` MISSING; Rosetta not installed (`/Library/Apple/usr/share/rosetta` absent); Command Line Tools only. No Windows machine or VM. Installing build tools by brew/pip/npm/cargo is authorized (note each install); pushing, store submission, production signing credentials, paid infrastructure, owner hosts, Accessibility/Screen Recording are not.
The request lists six asks in prose (the "seven" count presumably splits the three build platforms differently); the table follows the request order.

| # | Ask | macOS arm64 | Linux | Windows | Doable on this Mac? | What it needs otherwise |
|---|---|---|---|---|---|---|
| 1a | Build unsigned packages | `.app` + `.dmg`: YES (done for RC2, section 4.1; a fresh e2f2fa9-based build is required) | `.deb`/`.AppImage` x86_64: NOT natively. Possible only (a) in an emulated linux/amd64 container or a QEMU x86_64 Lima VM (needs `brew install qemu`; Rust + PyInstaller + WebKit libs under emulation, 8.3 GB Docker VM memory; UNVERIFIED, slow) or (b) an aarch64 Lima VM, which is NOT a pinned configuration (no aarch64 lock, SBOM triple and acceptance assume x86_64; new locks would be a recipe change) | NO: the sidecar must be frozen by PyInstaller on Windows, WiX/NSIS/WebView2 and MSVC are Windows-only, no Windows host | macOS arm64 yes; Linux emulated only; Windows no | Ubuntu 24.04 x86_64 host (owner's evo-x2 is off limits; a GitHub-hosted runner needs a push and CI, not authorized); a Windows 11 x64 host or authorized CI (phase-2 decision 3 options) |
| 1b | macOS Intel / universal2 | NO as pinned (no x86_64 lock or SBOM triple; PyInstaller cannot cross-freeze) | - | - | no | Intel Mac or Rosetta-based x86_64 toolchain plus new locks; owner decision |
| 2 | Install tests | YES for what an unsigned local build permits: `verify_dmg.sh` (read-only hdiutil attach), `headless_smoke.sh`, `post_run_checks.sh` (observed on RC2). A real drag-install into `/Applications`, Gatekeeper first-launch and quarantine behaviour need admin authorization or a fresh macOS user account (phase-2 open item 4) and cannot be fully done here | `.deb` install test needs a real Ubuntu environment; an amd64 container without systemd can run `apt install ./x.deb` and `acceptance-ubuntu.sh` partly (UNVERIFIED); aarch64 VM cannot install an amd64 deb | NO | macOS partially; Linux emulated only; Windows no | Windows 11 (kit `packaging/acceptance/windows-r0prime`), a clean Ubuntu x86_64 machine, a fresh macOS account |
| 3 | UI-agent installed-app tests | PARTIAL: the installed app's sidecar serves the Projects UI on 127.0.0.1, so browser-driven tests of that UI (Chromium/axe, the repo's `RDO_BROWSER_TESTS=1` suite, the `ui-agent` repo) are possible. Driving the native Tauri window (the launcher) by clicking needs Accessibility/Screen Recording, which is NOT authorized; the `rc3/finalfix-validation` build already covered the window through in-app probes (CSP 41/41, ACL 77/77, nav guard 22/22, About 9/9, buttons 17/17; RECORDED) | WebKitGTK window automation needs a Linux desktop session (xvfb in a VM) | NO | macOS partially (UI over HTTP only); no native-window clicking | A Linux desktop session; a Windows 11 host with WebView2; a person for the manual checklists (D-01, D-02, D-05, D-06, D-09, VoiceOver, real Tab order, Gatekeeper) |
| 4 | OCI images with digests, SBOM, provenance, scans, signature verification | YES as a local rehearsal: `docker buildx build` native linux/arm64 and emulated linux/amd64; local image digests; a loopback `registry:2` container for registry digests; SBOM and provenance via buildx attestations or `brew install syft`; scans via `brew install trivy`/`grype` (each install to be noted; vulnerability DBs download from public hosts); sign and verify with `cosign` using a throwaway key (production signing keys must not be used; a throwaway-key signature proves the mechanism, not a release). Blockers that are decisions, not tooling: which of the 26 Dockerfiles are release images (none today); base images are tag-pinned only (digest pinning is a change on the new branch); `edge-sentinel` fetches unpinned tools from `main`/`latest` and is amd64-only; sidekick-devops downloads kubectl without a checksum; no push to ghcr.io | - | - | YES (rehearsal, local, nothing pushed) | An owner decision on the release-image list and a registry/signing identity for anything beyond a rehearsal |
| 5 | Apple / Microsoft Store / Snap packaging | Apple: NONE exists. Only structural work is possible here (unsigned `.pkg` skeleton with `pkgbuild`/`productbuild`, an entitlement analysis). A Mac App Store build needs App Sandbox entitlements, Apple Distribution/Installer certificates (only a Developer ID Application identity exists; no Installer certificate; production credentials are not authorized), and Transporter/App Store Connect validation (not authorized). Sandbox rules interact with the existing sidecar entitlement `disable-library-validation`, the loopback listener and the Docker buttons: any permission a store forces is an owner decision and has not been applied | Snap: NONE exists (no snapcraft.yaml; tauri has no snap target). `snapcraft` needs Linux (an aarch64 Lima VM could build an arm64 snap in destructive mode; amd64 needs an amd64 host); confinement choice (strict blocks the sidecar spawn and Docker; classic needs store review) is an owner decision | MSIX: NONE exists. Needs `MakeAppx`/signing on Windows, a Store publisher identity and the `runFullTrust` capability (owner decision) | Structural prep on macOS only; nothing submittable | Windows host + Store account for MSIX; Linux host for snap; owner decisions on permissions; submission of any kind is not authorized |
| 6 | Readiness report | YES (a document). It can only state what was tested: macOS arm64 unsigned build and local tests; Linux x86_64 and Windows not tested; no signing, notarization, Gatekeeper first launch, or store packaging; all 18 blocking distribution decisions OPEN | - | - | YES | - |

Existing blocked-by-design items that no host choice removes: the 21 legal decisions (18 blocking) and the empty legal-signer allow-list make a distribution build exit 3 (`check-distribution-gate.py`); RC3 supersedes signing manifest 29151a0a only after a new candidate and an independent review of the signing procedure on the exact candidate tree (freeze record section 5).

## 6. Inconsistencies and defects found while inventorying

1. Version streams disagree and none is cross-referenced: wheel `agentic-os` 0.2.4 (`pyproject.toml:3`, `agentic_os/__init__.py:16`) vs product 0.1.1 (`tauri.conf.json:4`, Cargo.toml, `identity.json`) vs frozen product 0.1.0 vs nearest tag `v0.3.2` (`git describe` `v0.3.2-176-ge2f2fa9`) vs the reference clone's `main` at `v0.4.1-62` (a different, later line). The desktop freeze is based on the v0.3.2 commit and does not contain the 529 later commits of this clone's `main`.
2. Product naming: product `Agentic Apps R0-prime`, Cargo package `redevops-launcher`, binary `agentic-r0prime`, sidecar `redevops-sidecar`, installer text `ReDevOps` in `packaging/README.md` and `ACCEPTANCE-ubuntu.md` (`ReDevOps_*.deb`), `agentic-apps_0.1.0_amd64.deb` in `BUILD-hosts.md`. `acceptance-ubuntu.sh` comments say the Debian package name slugs to `re-dev-ops`.
3. `packaging/BUILD-hosts.md` is stale against the pinned recipe: it names an AWS EC2 Mac host and a Proxmox Windows image as the build hosts (both off limits or not in use), shows `cargo tauri build --bundles dmg` directly instead of `build-all.sh`, says "Build both arches ... or a universal binary" although only aarch64 macOS and x86_64 Linux/Windows are locked, and marks Ubuntu as built for the old `agentic-apps` 0.1.0 deb.
4. `deploy/launcher/src-tauri/Cargo.toml:11` still says "SCAFFOLD: not built in CI" while `native-packaging.yml` builds it on three OS. The top of `deploy/launcher/README.md` describes a Docker-compose launcher ("Install / Start -> docker compose up"), which predates the native-first sidecar design described further down the same file.
5. `packaging/README.md` says the old `requirements.lock` "is gone"; a `packaging/requirements.lock` exists (regenerated as the universal lock, commit bab6c55). The sentence refers to the previous pip-freeze file; the name is reused.
6. uv: `toolchain.env:20` pins 0.12.16 and the workflow installs it; the locks are generated by uv 0.13.6 (their headers) and this Mac has 0.13.6; no script asserts the uv version (only recorded in the build manifest). `uv.lock` at the repo root is stale (certifi 2026.7.22) and unused.
7. The request's `packaging/projects_ui.lock.json` and `DESKTOP.md` do not exist at e2f2fa9 (section 1A.2). The UI provenance exists only in scratch reports; the freeze record's `rc3/ui-series` is not a branch in the repo.
8. The request's g8pg SHA `a47d11c` is stale (section 1B.1): the branch was rewritten; current HEAD `2546f17b`.
9. The desktop pins runtime-contracts v0.3.4 (`a643edc3...`), while the staged private package and the reference clone carry v0.3.16 (`d739c194...`). They are different components of different lines; the desktop does not consume the staged package.
10. Licence wording: `Cargo.toml` now uses `license-file` to `LICENSING.md` (the earlier readiness report's complaint about a plain AGPL string is fixed at the freeze); the copyright line is "Copyright (c) 2025-2026 RedevOps." (`tauri.conf.json:41`, also shown in the macOS About panel unconditionally, freeze record F5) while the signing identity names ReDevOps.io LLC: D-LIC-1 OPEN. D-LIC-15 OPEN (outreach text "open-source runtime stack").
11. Dockerfiles: all use a floating `python:3.12-slim`; `edge-sentinel` pipes installers from `main` to `sh` and fetches `releases/latest`; `sidekick-devops` fetches kubectl without a checksum; `Dockerfile.projects` leaves git in the image; seven demo images have an `image: <name>:latest` default; amd64-only binaries break arm64 builds of `edge-sentinel` and `sidekick-devops`. None is a release image and none has SBOM/provenance/signature.
12. The Tauri `targets` list contains no rpm, no MSIX and no snap; the CI workflow publishes nothing by design (enforced by `tests/test_workflow_no_publish.py`).
13. The freeze record states the repository commit-signing disclosure (48 signed / 5 unsigned); this document's own commit is unsigned (`-c commit.gpgsign=false`).

## 7. Limitations of this inventory

Read-only analysis. No package was built, no container started, no registry contacted, nothing fetched. The 144-file baseline was re-verified (144 OK). Hashes in this document were recomputed from files at e2f2fa9 or from the named scratch files; hashes quoted from records are marked RECORDED. The Linux and Windows build commands are derived from the scripts and the workflow and have not been run for the R0-prime line. Dirty worktrees of other sessions were not touched. Items that need owner input: which OCI images (if any) are release images; whether macOS Intel, Linux aarch64, rpm, snap, MSIX or Mac App Store are in scope (each is new work, none exists); the Windows build host choice (phase-2 decision 3); a fresh macOS account for final acceptance (phase-2 decision 4); every permission or entitlement a store would force.
