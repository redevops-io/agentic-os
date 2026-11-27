redevops-legal-decision: 1
decision: D-LIC-11
option: b

# OWNER DECISION: LAUNCHER ICONS AND THE rAIls MARK (D-LIC-11), SUPERSEDING THE 2026-11-12 ENTRY

> **DRAFT - NOT AUTHORIZED.** This document is a draft prepared by an agent at the owner's instruction of 2026-11-27. It authorizes nothing by itself,
> including after it has been read or copied. It becomes the owner's decision only if the owner signs these exact bytes with the legal-decision key and
> the release gate verifies that signature (section 5). **No counsel was consulted and none approved anything in it.** It is not a legal opinion and
> contains no counsel reference. The banner stays in the signed bytes on purpose: the signature, not the banner, is the decision.

## 1. What is decided, and what it supersedes

* **Decision:** D-LIC-11, "Launcher icons and the 'Re' mark", option **b** ("Exclude the icons from the licence statement and record their actual terms").
* **Superseded:** the D-LIC-11 line of `docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md` (sha256 `fa8662f3bcd02238adb514e17cb72cb87eb2f08613fb30cc3f111ad47e0f64a2`,
  decided 2026-11-12, option b), which prepared option b *for generated neutral launcher icons* (`scripts/make_neutral_icons.py`). **Only that line is superseded.**
  Every other decision in that approval stands, and that signed document is not edited.
* **What changes:** the artwork the option applies to. The six launcher icon files, the same six in `deploy/desktop-shell-main/`, and the snap icon are now the
  rAIls prime-spiral mark, which the owner approved on 2026-11-27, instead of the neutral artwork. The chosen option stays **b**: the icons are not listed as
  covered by the licence statement, and their actual terms are recorded as provenance, with no statement about authorship or rights.

## 2. The artwork that ships under this decision

Generator: `brand/rails_mark.py`, SHA-256 `342c0d47bb1b0f74cdf12625d8c231fc4bf1dd93ae326745572999f60e8970c8`, Python standard library only; a square spiral of the integers with 1 at the centre and a marker on
each prime; no letters in the application icons, no fonts, no third-party content, no model output in the files; nothing is traced from another image. Definition,
variants and colours: `brand/README.md`. Provenance record (facts only): `docs/legal/ARTWORK_PROVENANCE.md`. Launcher files (`deploy/launcher/src-tauri/icons/`):

| File | SHA-256 |
|---|---|
| `32x32.png` | `1c4d5fcf74a9c05dd1e770fac573c775b59809272e4dcbc2aaf3a0f4175bf0be` |
| `128x128.png` | `4bb529e59018d23aba7b2c932d7b1bbd231d2f68ed902910d8079a5b12cbf398` |
| `128x128@2x.png` | `00f871b066fc94466716ddc561e0abcaa9d220b8208538d3900bc51f318a39cd` |
| `icon.png` | `3369df2ff1515a7cae6d3102fec7ed7561136cbd89165b0992f504de3b6e3dbf` |
| `icon.icns` | `019432dadd4b22ba2dd38e15492967838d99f1fe7bc280cd7a0b580ac8693fb8` |
| `icon.ico` | `ad7e56e6bb5b6430e9dfe43ef09961754c3efe047c6b70dd6ae4053b6c6b9fcc` |

The copies in `deploy/desktop-shell-main/src-tauri/icons/` are byte-identical to these; the snap icon `snap/gui/icon.png` is the 256 px rendering of the same mark.

## 3. What the owner states, and what is not stated

The owner states that:

1. the rAIls mark was approved by the owner on 2026-11-27 as generated (primary 11 x 11, compact 7 x 7, tiny 5 x 5), and the owner wants it used as the application icon;
2. the files above are the output of the generator named above.

This document does **not** state that the owner or anyone else holds copyright in the files, that the files are first-party artwork under the family licence
(option a), that they are free of third-party rights, or that any trademark right exists or has been cleared. Mathematical originality is not trademark
clearance. **rAIls** and **rAIlsApps** are names the publisher uses; no registration or application is claimed.

## 4. Risks the owner accepts by signing

* Whether anyone holds rights in the output of a script written by an AI coding agent stays an open legal question (counsel questions Q28 and Q29); this record does not answer it.
* No trademark search or clearance has been done for the mark or the names. A third party could object to their use.
* The mark is distributed in the signed desktop installers from the next functional release on. Installers published before it keep their previous icon.

## 5. How this becomes a decision, and what changes in the gate afterwards

Sign these exact bytes (an OpenSSH SSHSIG in the namespace `redevops-legal-decision@v1`, made with the key listed in `packaging/legal-allowed-signers`):

    ssh-keygen -Y sign -f <your legal-decision key> -n redevops-legal-decision@v1 docs/legal/approved/D-LIC-11_RAILS_MARK_0.6.md

which writes `docs/legal/approved/D-LIC-11_RAILS_MARK_0.6.md.sig`. The agent never creates, chooses or handles that key. After the signature exists, the entry
`D-LIC-11` in `packaging/distribution-gate.json` changes in exactly these fields (nothing else, `chosen_option` stays `b`):

* `decision_reference`: `docs/legal/approved/D-LIC-11_RAILS_MARK_0.6.md`
* `decision_sha256`: the sha256 of the signed file (printed by `shasum -a 256`; this draft's bytes are the signed bytes, so it is the value in the pull request that carries this file)
* `decided_by`: `Alex Rybachek (owner), record <that sha256>`; `decided_on`: the date of signing
* `ships_meanwhile`: `the rAIls prime-spiral mark generated from code (brand/rails_mark.py), provenance and hashes in docs/legal/ARTWORK_PROVENANCE.md; LICENSING.md section 4 does not list the icons as covered`

Until then the gate keeps the 2026-11-12 entry, which still verifies; a distribution build made before the owner signs ships the new icons under the old entry, so
**sign before the release build that includes these icons is qualified**. Nothing in this file changes a signing key, a package identifier or a published installer.
