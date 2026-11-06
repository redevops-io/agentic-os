# Licensing of the Agentic Apps artifacts

This file is the single place that states which licence statement the shipped
artifacts carry, which files hold the texts, and what the statement does and
does not cover. It restates what the existing licence files already say. It
does not choose legal terms, it is not legal advice, and nothing in it says
that any component or distribution is cleared. Points that need the owner or
counsel are marked `[OWNER DECISION D-LIC-n]` and are listed, with what ships
meanwhile, in [packaging/DECISIONS-REQUIRED.md](packaging/DECISIONS-REQUIRED.md).

## 1. The statement

<!-- licence-statement:begin -->
Agentic Apps and the agentic-os control plane are source-available (family licence: AGPL-3.0-or-later with the Commons Clause).

SPDX-License-Identifier: LicenseRef-AGPL-3.0-or-later-with-Commons-Clause
<!-- licence-statement:end -->

No registered SPDX identifier exists for this combination: the Commons Clause
is a licence condition, not an SPDX exception, so a `LicenseRef-` name is used
(see [LICENSE.md](LICENSE.md)). Tooling that expects a registered identifier
reports the project as unrecognised.

Copyright line, as written in [LICENSE.md](LICENSE.md): "Copyright (c) 2025-2026 RedevOps."

`[OWNER DECISION D-LIC-1]` The wording of section 1 is the wording already used
by the repository (`LICENSE.md`, `README.md`, `modules.yaml`) and by the
evidence of the earlier native release. Whether it is the final public
statement for the packages, and whether the short form above is acceptable in
place of a longer one, is for the owner.

## 2. The texts

| File | What it is |
|---|---|
| [LICENSE](LICENSE) | the GNU Affero General Public License, version 3, 19 November 2007 (verbatim text) |
| [LICENSE-COMMONS-CLAUSE](LICENSE-COMMONS-CLAUSE) | the "Commons Clause" License Condition v1.0, naming `agentic-os` as the Software and RedevOps as the Licensor |
| [LICENSE.md](LICENSE.md) | the owner's statement of the combination, the effective date (2026-08-08) and the reasoning |
| this file | which artifact carries which statement (section 3) and the open points (section 5) |

The build configuration places the three licence files and this file in a
`Licenses` folder of each artifact, together with `THIRD_PARTY_NOTICES.txt`
(how, and what has been observed per artifact:
[packaging/notices/README.md](packaging/notices/README.md)).

## 3. What carries the statement

| Artifact or field | Value |
|---|---|
| `pyproject.toml`, `[project] license` | `LicenseRef-AGPL-3.0-or-later-with-Commons-Clause` (PEP 639 licence expression), with `license-files` naming the four files above |
| `deploy/launcher/src-tauri/Cargo.toml` | `license-file = "../../../LICENSING.md"`; no `license` SPDX field (a plain `AGPL-3.0-or-later` there would omit the Commons Clause) |
| `deploy/launcher/src-tauri/tauri.conf.json`, `bundle.resources` | delivers `Licenses/` into the macOS app bundle (`Contents/Resources/Licenses`) and into the Windows install directory |
| `deploy/launcher/src-tauri/tauri.conf.json`, `bundle.copyright` | the copyright line of section 1 |
| `README.md`, licence badge and "License" section | AGPL-3.0-or-later with the Commons Clause, source-available |
| `modules.yaml` header | AGPL-3.0-or-later with the Commons Clause, source-available |
| the sidecar's licensing answer (`agentic_os/stack_knowledge.py`, entry `licensing`) | the section 1 statement |

`packaging/notices/check_licence_statements.py` fails if any of these fields
stops agreeing with section 1.

## 4. What it covers, and what it does not

Covered by the statement in section 1 (first-party code in this repository, as
built into the artifacts):

* the `agentic_os` Python package (the control plane and Projects API), the
  sidecar `redevops-sidecar` that freezes it, and the Projects UI bundle
  served by the sidecar (vendored from the separate `redevops-projects`
  repository: `[OWNER DECISION D-LIC-2]` records that its licence statement was
  not re-read here);
* the `redevops-launcher` crate (the Tauri shell) and the installers that
  contain it: the macOS `.dmg`, the Windows `.msi` and the Windows NSIS `.exe`.

Not covered by it:

* third-party components (Python wheels, Rust crates, CPython, OpenSSL,
  Microsoft runtime files, JavaScript in the UI bundle). They remain under
  their own licences, whose texts are collected in `THIRD_PARTY_NOTICES.txt`;
  items for which no text could be found are listed in `NOTICES_MISSING.md`
  (same folder) and in the decision register;
* the sibling first-party packages that are imported (`runtime-contracts`,
  `redevops-connectors`, `knowledge-frontier`), each of which declares its own
  licence in its own repository; two of those declarations are marked there as
  inferred (`[OWNER DECISION D-LIC-2]`);
* `apps/*/LICENSE`: per-app licence files in this repository (plain
  AGPL-3.0). They are not part of the artifacts;
* releases tagged at or before `v0.1.0`, which `LICENSE.md` says were
  distributed under AGPL-3.0-or-later alone.

## 5. Obligations as the licence texts state them

These are quotations and section references, so that the open points below are
tied to the texts. They are not a reading of the texts.

AGPL-3.0 (`LICENSE`):

* section 5(b): "The work must carry prominent notices stating that it is
  released under this License and any conditions added under section 7."
* section 5(d): "If the work has interactive user interfaces, each must
  display Appropriate Legal Notices". Section 0 defines when an interface
  does: "a convenient and prominently visible feature that (1) displays an
  appropriate copyright notice, and (2) tells the user that there is no
  warranty for the work (except to the extent that warranties are provided),
  that licensees may convey the work under this License, and how to view a
  copy of this License."
* section 6: a covered work in object code form may be conveyed "provided that
  you also convey the machine-readable Corresponding Source under the terms of
  this License, in one of these ways" (6(a) to 6(e)).
* section 7, last paragraphs: "All other non-permissive additional terms are
  considered "further restrictions" within the meaning of section 10. If the
  Program as you received it, or any part of it, contains a notice stating
  that it is governed by this License along with a term that is a further
  restriction, you may remove that term."
* section 10: "You may not impose any further restrictions on the exercise of
  the rights granted or affirmed under this License."
* section 13: "if you modify the Program, your modified version must
  prominently offer all users interacting with it remotely through a computer
  network (if your version supports such interaction) an opportunity to
  receive the Corresponding Source of your version".

Commons Clause v1.0 (`LICENSE-COMMONS-CLAUSE`):

* "the grant of rights under the License will not include, and the License
  does not grant to you, the right to Sell the Software."
* "Sell" means "practicing any or all of the rights granted to you under the
  License to provide to third parties, for a fee or other consideration
  (including without limitation fees for hosting or consulting/support
  services related to the Software), a product or service whose value derives,
  entirely or substantially, from the functionality of the Software."
* "Any license notice or attribution required by the License must also include
  this Commons Clause License Condition notice."

## 6. Open points

`[OWNER DECISION D-LIC-1]` Product licence wording: section 1, the copyright
line, the short form used in package metadata, and whether the installers show
the licence on an acceptance page (see D-LIC-10 in the register).

`[OWNER DECISION D-LIC-3]` Whether the Commons Clause term may stay in a
combined binary that also contains code under plain AGPL-3.0 or other
licences, in light of AGPL section 7 ("further restrictions") and section 10,
quoted above. The text of `LICENSE.md` ("What this means for anything
depending on `agentic-os`") describes the owner's position for the family;
whether it settles the question for third-party components is for counsel.

`[OWNER DECISION D-LIC-5]` What "users interacting with it remotely through a
computer network" (section 13) means for the sidecar, which serves the UI on a
loopback address of the same machine. This file does not conclude.

`[OWNER DECISION D-LIC-12]` Source availability for the object code in the
artifacts (section 6 above). The repository that holds the source of this
release is <https://github.com/redevops-io/agentic-os>; the release tag and
commit are recorded in the build evidence. Whether a pointer to that
repository, a written offer, or both is the chosen way under section 6, and
the wording of any offer, is for the owner. Until decided, the artifacts carry
this pointer and no offer.

## 7. Where the decisions are recorded, and the distribution gate

Every decision that blocks distribution is listed, with its question, options, recommended default, who decides and what ships
meanwhile, in [docs/legal/DECISION_PACKAGES.md](docs/legal/DECISION_PACKAGES.md); counsel's questions are numbered in
[docs/legal/COUNSEL_BRIEF.md](docs/legal/COUNSEL_BRIEF.md), the exact texts they refer to are in
[docs/legal/EXACT_TEXTS.md](docs/legal/EXACT_TEXTS.md), and the owner's written record is the form
[docs/legal/OWNER_DECISION_FORM.md](docs/legal/OWNER_DECISION_FORM.md). The list is machine-readable in
`packaging/distribution-gate.json` (status OPEN until the owner has decided); a build with `REDEVOPS_DISTRIBUTION_BUILD=1` is refused
by `packaging/check-distribution-gate.py` while any applicable decision is OPEN. A private candidate build is not a distribution.
None of these documents says that any component or distribution is cleared.

Two facts changed since section 6 was written, and are stated here rather than interpreted: the artifacts carry no source and no
source offer inside the application or the disk image (the `SOURCE-OFFER.md` template is a separate file), and the pinned
`redevops-connectors` tag still has no licence file (D-LIC-2).
