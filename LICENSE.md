# License

`agentic-os` is **source-available**, not open source. It is licensed under the
**GNU Affero General Public License v3.0 or later**, as modified by the
**Commons Clause License Condition v1.0**.

- Full AGPL text: [LICENSE](LICENSE)
- The added condition: [LICENSE-COMMONS-CLAUSE](LICENSE-COMMONS-CLAUSE)

Copyright (c) 2025-2026 RedevOps.

    SPDX-License-Identifier: LicenseRef-AGPL-3.0-or-later-with-Commons-Clause

There is no registered SPDX identifier for this combination — Commons Clause is
a licence *condition*, not an SPDX exception — so a `LicenseRef-` name is used.
Tooling expecting a standard identifier will report this project as
unrecognised, and GitHub will stop labelling it AGPL. That is accurate: it is
not AGPL any more.

## Effective point, and what stays as it was

Relicensed on **2026-08-08**, by the commit that added this file. `28c9825` is
the last commit under the old terms.

Everything up to and including `28c9825` — which is every release tagged at or
before `v0.1.0` — was distributed under **AGPL-3.0-or-later alone**, and remains
available on those terms. A licence change is not retroactive: it cannot revoke
a grant already made. Anyone who obtained an earlier version keeps the rights
that version came with, and may keep using, modifying and redistributing it as
plain AGPL, including selling it.

This is recorded rather than assumed, because "when did the terms change" is
the first question anyone auditing a dependency asks, and a repository that
only shows its current licence cannot answer it.

## What this permits, and what it does not

You may read, run, modify, self-host and redistribute this control plane,
including inside a company for its own business, commercially. Every AGPL
obligation applies, most importantly §13: if you modify it and let users
interact with the result over a network, those users are entitled to the source
of your modified version.

You may **not sell it**, in the Commons Clause's sense — you may not charge
third parties for the software itself, for hosting it, or for consulting or
support whose value derives entirely or substantially from it.

## Why this changed

Not for commercial reasons. It changed because the architecture made the old
licence unworkable.

`runtime-contracts` holds the canonical `VerifiedIntent` — the Discovery →
Mission boundary, owned by neither runtime precisely so that neither can
redefine it. `agentic-os` is the Mission Runtime. For cross-runtime replay to
prove anything, this repository has to *import* those contracts rather than
reimplement them: a comparator that translates between two private type sets is
measuring its own adapter, not the two runtimes.

But `runtime-contracts` is AGPL **with Commons Clause**, and AGPL §7 forbids
imposing further restrictions on AGPL-covered work. A pure-AGPL project cannot
take a Commons-Clause dependency and still be distributable as AGPL. The
options were not equivalent:

- **Drop Commons Clause from `runtime-contracts`.** Rejected. It would let an
  older licence on one repository set the commercial terms of the whole stack,
  which is an incidental fact deciding an architectural one.
- **Do not share the package** — each runtime keeps private types and proves
  equivalence against golden fixtures. Rejected, and it is the worst of the
  three: `IMPLEMENTATION_SPLIT.md` states that reproducing a golden hash proves
  translation only, never that the production path adopted the contract. It
  does not avoid the problem, it makes the gate unmeasurable.
- **Align this repository with the contracts it consumes.** Chosen.

The result says out loud what was already true. `agentic-os` is not an
independent utility that happens to speak a neutral protocol; it is where the
Mission Runtime lives, in the same family as the contracts and the product
above them. One licence across the family, or a seam at exactly the boundary
the contracts exist to hold together.

## The word "open source"

Commons Clause's own FAQ is explicit that adding it means the result is not
open source in the OSI sense, and describes it as source-available. So this
repository says source-available. Where the README and `modules.yaml` called
the project open-core or an open-source repo, they now name the licence
instead.

Descriptions of the *third-party* cores each module is built on — Lago,
Chatwoot, Umami, Listmonk, changedetection.io — are unchanged. Those really are
open source, and it is their licence being described, not this one.

## What this means for anything depending on `agentic-os`

The same inheritance that made this change necessary now applies downstream.
Anything importing these packages is a work based on them and takes on both the
AGPL's network obligation and the Commons Clause's restriction on selling. A
third party wanting the semantics without the terms has to write their own
implementation.

`runtime-contracts/LICENSE.md` records the one case still open: `mission-sdk`
is Apache-2.0 deliberately, and becomes a work based on a Commons-Clause
package the moment it adapts the canonical `MissionProgram`. That question is
unchanged by this file, and is still a design question — whether `mission-sdk`
*owns* runtime semantics or merely *translates* them — before it is a legal
one.

## Contribution history

All authoring commits in this repository are by RedevOps (Alex Mats, under two
git identities). The remaining commits are GitHub merge commits produced by the
`arybach` account operating the merge button on `redevops-io/*` branches, which
introduce no third-party authorship. On that basis RedevOps holds the copyright
needed to relicense. Recorded here because relicensing over outside
contributions without a CLA is the usual way this goes wrong, and the check
should be visible rather than assumed.

This file states the terms and the reasoning. It is not legal advice.
